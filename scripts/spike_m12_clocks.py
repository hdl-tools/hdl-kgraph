#!/usr/bin/env python3
"""M12 real-design validation — port the clock/CDC scan to SQL + kuzu (#128).

M12 (#142/#143/#144) showed off-the-shelf out-of-core layers answer a *structural*
whole-design scan in bounded RAM — but on the combinational `gen_corpus`, the real
clock/CDC scans were empty. This script closes that gap on a **real design**: it ports
the heaviest real whole-design scan — `clock_domains` (whose `net_aliases` step is
union-find / connected-components, the genuinely hard part) and `cdc_suspects` (the
one-step combinational bridge) — to SQL-native and kuzu, and asserts **byte-identical
parity** against `graph/clocks.py` (the oracle), measuring peak RSS per backend.

Key reformulation that makes the union-find expressible off-the-shelf: `clocks._UnionFind`
assigns every node the **lexicographically smallest id in its connected component**, which
equals *transitive closure + MIN(reachable id)* — a recursive CTE in SQLite, and a
variable-length path + MIN in kuzu Cypher.

Backends (each in an isolated child process for a clean peak RSS, parent kept lean):
- networkx — `summary.clock_summary` over the materialised graph (the oracle + the wall).
- sql — pure SQLite: recursive-CTE alias components + aggregation (domains *and* cdc).
- kuzu — projected attrs + a materialised Alias rel + variable-length component traversal
  (domains only; the cdc bridge port is left to productionisation — see the report).

Usage::

    pip install -e '.[spike]'
    python scripts/spike_m12_clocks.py --db /path/to/.hdl-kgraph/graph.db
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from profile_v2 import _cpu_seconds, _peak_rss_bytes, _RSSSampler  # noqa: E402

from hdl_kgraph.graph import summary  # noqa: E402
from hdl_kgraph.storage.sqlite_store import SqliteStore  # noqa: E402

_RESULT_MARKER = "__SPIKE_M12_CLOCKS_RESULT__"
_IDENT = re.compile(r"\s*\w+\s*\Z")  # a single identifier (the only expr that can alias)


# --------------------------------------------------------------------------- #
# Shared: alias components (union-find ≡ transitive closure + MIN reachable id)
# --------------------------------------------------------------------------- #
def _alias_pairs_sql(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(formal_port_id, actual_id) pairs, mirroring clocks.net_aliases exactly."""
    conn.create_function("strip", 1, lambda s: s.strip() if s else None)
    return conn.execute(
        """
        WITH a AS (
          SELECT e.src inst, e.dst actual,
                 json_extract(e.attrs,'$.expr_text') expr,
                 json_extract(e.attrs,'$.via_port')  via
          FROM edges e
          WHERE e.kind IN ('reads','drives') AND json_extract(e.attrs,'$.derived')='connects'
        ),
        matched AS (
          SELECT a.inst, a.actual, a.via, na.name actual_name
          FROM a JOIN nodes na ON na.id=a.actual
          WHERE a.expr IS NOT NULL AND (strip(a.expr)=na.name OR lower(strip(a.expr))=na.name)
        )
        SELECT p.id formal, m.actual
        FROM matched m
        JOIN edges i ON i.src=m.inst AND i.kind='instantiates'
        JOIN edges d ON d.src=i.dst AND d.kind='declares'
        JOIN nodes p ON p.id=d.dst AND p.kind='port' AND p.name=m.via
        """
    ).fetchall()


def _alias_root_sql(conn: sqlite3.Connection, pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Component root (lex-min id) for every aliased node, via a recursive CTE."""
    conn.execute("CREATE TEMP TABLE ae(a TEXT, b TEXT)")
    conn.executemany("INSERT INTO ae VALUES(?,?)", pairs)
    conn.executemany("INSERT INTO ae VALUES(?,?)", [(b, a) for a, b in pairs])
    return dict(
        conn.execute(
            """
            WITH RECURSIVE ni(n) AS (SELECT DISTINCT a FROM ae),
            reach(n, r) AS (
              SELECT n, n FROM ni
              UNION
              SELECT x.n, ae.b FROM reach x JOIN ae ON ae.a=x.r
            )
            SELECT n, MIN(r) FROM reach GROUP BY n
            """
        )
    )


# --------------------------------------------------------------------------- #
# SQL backend: clock_domains + cdc_suspects (parity with summary.clock_summary)
# --------------------------------------------------------------------------- #
def _scan_sql(db: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    root = _alias_root_sql(conn, _alias_pairs_sql(conn))
    find = lambda x: root.get(x, x)  # noqa: E731
    name = dict(conn.execute("SELECT id, name FROM nodes"))
    kind = dict(conn.execute("SELECT id, kind FROM nodes"))
    nfile = dict(conn.execute("SELECT id, file FROM nodes"))
    nline = dict(conn.execute("SELECT id, line_start FROM nodes"))

    # --- clock_domains ---
    dn: dict[str, set[str]] = defaultdict(set)
    dp: dict[str, list[str]] = defaultdict(list)
    dc: dict[str, float] = defaultdict(lambda: 1.0)
    for src, clk, conf in conn.execute(
        "SELECT src, dst, confidence FROM edges WHERE kind='clocked_by'"
    ):
        r = find(clk)
        dn[r].add(name[clk])
        if src not in dp[r]:
            dp[r].append(src)
        dc[r] = min(dc[r], conf)
    domains = []
    for r in dn:
        driven = set()
        for proc in dp[r]:
            if kind.get(proc) != "process":
                continue
            for (sig,) in conn.execute(
                "SELECT dst FROM edges WHERE kind='drives' AND src=?", (proc,)
            ):
                driven.add(find(sig))
        cn = sorted(dn[r])
        domains.append(
            {
                "clock": cn[0],
                "aliases": cn,
                "process_count": len(dp[r]),
                "signal_count": len(driven),
                "min_confidence": dc[r],
            }
        )
    domains.sort(key=lambda d: d["aliases"][0])

    # --- cdc_suspects (the combinational-bridge logic, mirroring clocks.cdc_suspects) ---
    proc_domain: dict[str, tuple[str, float]] = {}
    clock_nets: set[str] = set()
    for proc, clk, conf in conn.execute(
        "SELECT src, dst, confidence FROM edges WHERE kind='clocked_by'"
    ):
        if kind.get(proc) != "process":
            continue
        r = find(clk)
        clock_nets.add(r)
        held = proc_domain.get(proc)
        if held is None:
            proc_domain[proc] = (r, conf)
        elif held[0] != r:
            proc_domain[proc] = ("", 0.0)
    proc_domain = {p: d for p, d in proc_domain.items() if d[0]}
    sig_domain: dict[str, dict[str, tuple[float, str]]] = defaultdict(dict)
    reads: dict[str, list[tuple[str, float]]] = defaultdict(list)
    drives: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for proc, sig, conf in conn.execute(
        "SELECT src, dst, confidence FROM edges WHERE kind='reads'"
    ):
        if kind.get(proc) == "process":
            reads[proc].append((find(sig), conf))
    for proc, sig, conf in conn.execute(
        "SELECT src, dst, confidence FROM edges WHERE kind='drives'"
    ):
        if kind.get(proc) != "process":
            continue
        drives[proc].append((find(sig), conf))
        dom = proc_domain.get(proc)
        if dom:
            rt, dconf = dom
            sd = sig_domain[find(sig)]
            c = min(dconf, conf)
            if rt not in sd or sd[rt][0] < c:
                sd[rt] = (c, proc)
    for proc, driven_list in drives.items():
        if proc in proc_domain:
            continue
        inherited: dict[str, tuple[float, str]] = {}
        for rs, rc in reads.get(proc, []):
            for rt, (c, drv) in sig_domain.get(rs, {}).items():
                m = min(c, rc)
                if rt not in inherited or inherited[rt][0] < m:
                    inherited[rt] = (m, drv)
        for sig, dconf in driven_list:
            sd = sig_domain[sig]
            for rt, (c, drv) in inherited.items():
                m = min(c, dconf)
                if rt not in sd or sd[rt][0] < m:
                    sd[rt] = (m, drv)
    suspects = []
    for proc, (rr, rc) in sorted(proc_domain.items()):
        for sig, rcf in reads.get(proc, []):
            if sig in clock_nets:
                continue
            for rt, (c, drv) in sorted(sig_domain.get(sig, {}).items()):
                if rt == rr:
                    continue
                suspects.append(
                    {
                        "signal_id": sig,
                        "signal_name": name[sig],
                        "file": nfile[sig],
                        "line": nline[sig],
                        "driver_id": drv,
                        "driver_domain": name[rt],
                        "reader_id": proc,
                        "reader_domain": name[rr],
                        "confidence": min(c, rcf, rc),
                    }
                )
    suspects.sort(key=lambda s: (s["signal_name"], s["reader_id"], s["driver_domain"]))
    conn.close()
    return {"domains": domains, "cdc_suspect_count": len(suspects), "cdc_suspects": suspects[:50]}


# --------------------------------------------------------------------------- #
# kuzu backend: clock_domains (domains only)
# --------------------------------------------------------------------------- #
def _kuzu_dir(db: Path) -> Path:
    return db.parent / "graph_kuzu_clocks"


def _build_kuzu(db: Path) -> None:
    import csv
    import shutil

    import kuzu

    kuzu_dir = _kuzu_dir(db)
    for stale in (kuzu_dir, kuzu_dir.with_name(kuzu_dir.name + ".wal")):
        if stale.is_dir():
            shutil.rmtree(stale)
        elif stale.exists():
            stale.unlink()
    ncsv, ecsv = db.parent / "kc_nodes.csv", db.parent / "kc_edges.csv"
    sq = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with open(ncsv, "w", newline="") as fh:
        w = csv.writer(fh)
        for nid, kind, nm in sq.execute("SELECT id, kind, name FROM nodes"):
            w.writerow([nid, kind, nm or ""])
    with open(ecsv, "w", newline="") as fh:
        w = csv.writer(fh)
        for src, dst, kind, conf, attrs in sq.execute(
            "SELECT src, dst, kind, confidence, attrs FROM edges"
        ):
            a = json.loads(attrs)
            expr = str(a.get("expr_text") or "")
            # only single-identifier exprs can alias; emitting "" otherwise keeps the CSV
            # clean (real RTL concatenation exprs carry commas/newlines that break COPY)
            expr = expr if _IDENT.match(expr) else ""
            w.writerow(
                [
                    src,
                    dst,
                    kind,
                    str(a.get("derived") or ""),
                    expr,
                    str(a.get("via_port") or ""),
                    conf,
                ]
            )
    sq.close()
    conn = kuzu.Connection(kuzu.Database(str(kuzu_dir)))
    conn.execute("CREATE NODE TABLE Node(id STRING, kind STRING, name STRING, PRIMARY KEY(id))")
    conn.execute(
        "CREATE REL TABLE Edge(FROM Node TO Node, kind STRING, derived STRING, "
        "expr STRING, via STRING, confidence DOUBLE)"
    )
    conn.execute(f"COPY Node FROM '{ncsv}'")
    conn.execute(f"COPY Edge FROM '{ecsv}'")
    # materialise the alias relation (both directions) for component traversal.
    # Bulk-COPY rather than per-pair CREATE — the latter is O(pairs) round-trips and
    # dominates build time on a real design (≈900 pairs).
    conn.execute("CREATE REL TABLE Alias(FROM Node TO Node)")
    pairs = conn.execute(
        """
        MATCH (inst:Node)-[e:Edge]->(actual:Node)
        WHERE e.kind IN ['reads','drives'] AND e.derived='connects'
          AND (trim(e.expr)=actual.name OR lower(trim(e.expr))=actual.name)
        MATCH (inst)-[i:Edge]->(m:Node)-[d:Edge]->(p:Node)
        WHERE i.kind='instantiates' AND d.kind='declares' AND p.kind='port' AND p.name=e.via
        RETURN DISTINCT p.id, actual.id
        """
    )
    acsv = db.parent / "kc_alias.csv"
    with open(acsv, "w", newline="") as fh:
        w = csv.writer(fh)
        while pairs.has_next():
            a, b = pairs.get_next()
            w.writerow([a, b])
            w.writerow([b, a])
    conn.execute(f"COPY Alias FROM '{acsv}'")


def _scan_kuzu(db: Path) -> dict[str, Any]:
    import kuzu

    conn = kuzu.Connection(kuzu.Database(str(_kuzu_dir(db))))

    def rows(q: str, p: dict[str, Any] | None = None) -> list[Any]:
        res = conn.execute(q, p) if p else conn.execute(q)
        out = []
        while res.has_next():
            out.append(res.get_next())
        return out

    # NOTE: kuzu variable-length matches *walks*, not a reachable set (unlike SQL's
    # UNION recursive CTE), so cost grows combinatorially with the depth bound on the
    # cyclic/symmetric alias graph — *1..30 hangs. A small cap covers real alias-chain
    # depth (hierarchy hops) here; a production kuzu port would use a WCC algorithm
    # extension instead. See docs/v2/m12_real_design.md.
    root = {a: b for a, b in rows("MATCH (a:Node)-[:Alias*1..10]->(b:Node) RETURN a.id, MIN(b.id)")}
    find = lambda x: root.get(x, x)  # noqa: E731
    kind = {r[0]: r[1] for r in rows("MATCH (n:Node) RETURN n.id, n.kind")}
    dn: dict[str, set[str]] = defaultdict(set)
    dp: dict[str, list[str]] = defaultdict(list)
    dc: dict[str, float] = defaultdict(lambda: 1.0)
    for s, c, cname, conf in rows(
        "MATCH (s:Node)-[e:Edge]->(c:Node) WHERE e.kind='clocked_by' "
        "RETURN s.id, c.id, c.name, e.confidence"
    ):
        r = find(c)
        dn[r].add(cname)
        if s not in dp[r]:
            dp[r].append(s)
        dc[r] = min(dc[r], conf)
    domains = []
    for r in dn:
        driven = set()
        for proc in dp[r]:
            if kind.get(proc) != "process":
                continue
            for (sig,) in rows(
                "MATCH (p:Node {id:$p})-[e:Edge]->(s:Node) WHERE e.kind='drives' RETURN s.id",
                {"p": proc},
            ):
                driven.add(find(sig))
        cn = sorted(dn[r])
        domains.append(
            {
                "clock": cn[0],
                "aliases": cn,
                "process_count": len(dp[r]),
                "signal_count": len(driven),
                "min_confidence": dc[r],
            }
        )
    domains.sort(key=lambda d: d["aliases"][0])
    return {"domains": domains}


# --------------------------------------------------------------------------- #
# networkx oracle
# --------------------------------------------------------------------------- #
def _scan_networkx(db: Path) -> dict[str, Any]:
    graph, _f, _m = SqliteStore(db).load()
    return summary.clock_summary(graph)


_STAGES = {"networkx": _scan_networkx, "sql": _scan_sql, "kuzu": _scan_kuzu}


# --------------------------------------------------------------------------- #
# Orchestration (subprocess-isolated peak RSS; parent stays lean)
# --------------------------------------------------------------------------- #
def _run_child(stage: str, db: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [sys.executable, __file__, "--_child", stage, "--db", str(db)],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"stage {stage!r} timed out after 900s") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"stage {stage!r} failed:\n{proc.stderr}")
    for line in proc.stdout.splitlines():
        if line.startswith(_RESULT_MARKER):
            return json.loads(line[len(_RESULT_MARKER) :])
    raise RuntimeError(f"stage {stage!r}: no result\n{proc.stdout}\n{proc.stderr}")


def _measure(stage: str, db: Path) -> dict[str, Any]:
    with _RSSSampler() as sampler:
        cpu0 = _cpu_seconds()
        started = time.perf_counter()
        result = _STAGES[stage](db)
        wall_s = time.perf_counter() - started
        cpu_s = _cpu_seconds() - cpu0
    return {
        "result": result,
        "wall_s": wall_s,
        "cpu_s": cpu_s,
        "peak_rss": _peak_rss_bytes(),
        "sampled_peak_rss": sampler.peak,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, required=True, help="path to an existing graph.db")
    ap.add_argument("--_child", dest="child", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.child:
        if args.child == "kuzu_build":
            _build_kuzu(args.db)
            print(_RESULT_MARKER + json.dumps({"ok": True}))
        else:
            print(_RESULT_MARKER + json.dumps(_measure(args.child, args.db)))
        return 0

    backends = ["networkx", "sql"]
    try:
        import kuzu  # noqa: F401

        backends.append("kuzu")
    except ImportError:
        print("kuzu not installed — skipping that backend")

    mib = 1024 * 1024
    if "kuzu" in backends:
        _run_child("kuzu_build", args.db)  # one-time conversion, not measured
    measured = {b: _run_child(b, args.db) for b in backends}
    oracle = measured["networkx"]["result"]

    print(f"\n=== clock/CDC scan on {args.db} ===")
    ndom, ncdc = len(oracle["domains"]), oracle["cdc_suspect_count"]
    print(f"oracle: {ndom} clock domains, {ncdc} CDC suspects")
    ok = True
    for b in backends:
        res = measured[b]["result"]
        dom_ok = res["domains"] == oracle["domains"]
        # kuzu computes domains only; sql also computes cdc (check both list + count)
        if "cdc_suspects" in res:
            cdc_ok = (
                res["cdc_suspects"] == oracle["cdc_suspects"]
                and res.get("cdc_suspect_count") == oracle["cdc_suspect_count"]
            )
        else:
            cdc_ok = True
        scope = "domains+cdc" if "cdc_suspects" in res else "domains"
        verdict = "ok" if (dom_ok and cdc_ok) else "PARITY-FAIL"
        ok = ok and dom_ok and cdc_ok
        ms, rss = measured[b]["wall_s"] * 1000, measured[b]["peak_rss"] / mib
        print(f"  {b:9s} {scope:11s} parity={verdict:11s} scan {ms:8.1f} ms  peak {rss:7.0f} MiB")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
