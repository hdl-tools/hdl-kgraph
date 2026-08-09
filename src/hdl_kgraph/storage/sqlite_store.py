"""SQLite persistence (M1; schema v3 since M5).

Single-file, local-first storage of the knowledge graph:

* ``meta``  — schema_version, build root, built_at, tool_version,
  options_hash (fingerprint of the effective build inputs; an options change
  invalidates incremental updates)
* ``files`` — path, language, content_hash (drives M4 incremental rebuilds),
  size_bytes, parse_error_count, skipped_reason, warnings (JSON array of
  preprocessor diagnostics, so ``status --errors`` can report them after
  the fact)
* ``nodes`` — id, kind, name, qualified_name, file, line span, language,
  attrs (JSON)
* ``edges`` — src, dst, kind, confidence, attrs (JSON)
* ``file_irs`` — per-unit pass-1 IR, macro-event log, and spliced-header
  list (JSON via :mod:`hdl_kgraph.storage.ir_codec`); only units parsed
  standalone get rows. ``update`` re-links unchanged units from here.
* ``discrepancies`` — M7 enrichment findings: where native-frontend
  elaboration disagreed with the heuristic graph (only populated by
  ``build --enrich``). Surfaced by ``hdl-kgraph discrepancies``.

``save()`` is a full rewrite, written to a temp file (in WAL mode) and
atomically swapped into place with ``os.replace`` — concurrent readers (CLI
queries, the MCP server) never block on or observe a half-written database,
and a crashed build leaves the previous database intact. ``update`` reuses
stored IRs for unchanged files and persists the re-linked result through
``save_incremental()``, which diffs the new graph against the stored rows and
writes only the delta (UPSERT changed nodes/edges/file rows, delete vanished
ones) under a single WAL transaction — so a one-file edit pays a write cost
proportional to the change, not the whole design. Migration policy: the
database is a derived cache. ``update``/``watch`` first try
:meth:`SqliteStore.migrate`, which upgrades an older schema in place when a
registered, IR-compatible ladder step exists (e.g. adding the ``summaries``
table); when no such step exists — a gap in the ladder or a change to the
persisted IR encoding (``ir_codec.IR_CODEC_VERSION``) — they fall back to a
full rebuild. Read commands stay read-only: ``load()`` still raises
:class:`SchemaVersionError` on a version mismatch until a writer migrates the
database. See ``docs/schema-migrations.md``.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

from hdl_kgraph import __version__
from hdl_kgraph.enrich.base import Discrepancy
from hdl_kgraph.graph.builder import RefRecord
from hdl_kgraph.schema import Edge, EdgeKind, Language, NodeKind
from hdl_kgraph.storage.ir_codec import IR_CODEC_VERSION

SCHEMA_VERSION = "9"  # v9: clock summary carries cdc_analysis/alias_collapses (#176)

# How long a reader waits on a residual write lock before giving up.
_BUSY_TIMEOUT_MS = 5_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
  path              TEXT PRIMARY KEY,
  language          TEXT NOT NULL,
  content_hash      TEXT NOT NULL,
  size_bytes        INTEGER NOT NULL,
  parse_error_count INTEGER NOT NULL DEFAULT 0,
  skipped_reason    TEXT,
  warnings          TEXT NOT NULL DEFAULT '[]',
  parse_errors      TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS nodes (
  id             TEXT PRIMARY KEY,
  kind           TEXT NOT NULL,
  name           TEXT NOT NULL,
  qualified_name TEXT NOT NULL DEFAULT '',
  file           TEXT NOT NULL DEFAULT '',
  line_start     INTEGER NOT NULL DEFAULT 0,
  line_end       INTEGER NOT NULL DEFAULT 0,
  language       TEXT NOT NULL DEFAULT 'unknown',
  attrs          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind_name ON nodes(kind, name);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file);
CREATE TABLE IF NOT EXISTS edges (
  src        TEXT NOT NULL,
  dst        TEXT NOT NULL,
  kind       TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  attrs      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS file_irs (
  path         TEXT PRIMARY KEY,
  ir           TEXT NOT NULL,
  macro_events TEXT NOT NULL DEFAULT '[]',
  included     TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS discrepancies (
  kind       TEXT NOT NULL,
  backend    TEXT NOT NULL,
  detail     TEXT NOT NULL DEFAULT '',
  node_id    TEXT NOT NULL DEFAULT '',
  src        TEXT NOT NULL DEFAULT '',
  dst        TEXT NOT NULL DEFAULT '',
  heuristic  TEXT NOT NULL DEFAULT '',
  elaborated TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ref_index (
  file        TEXT NOT NULL,
  src_id      TEXT NOT NULL,
  edge_kind   TEXT NOT NULL,
  target_name TEXT NOT NULL,
  scoped      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ref_index_target ON ref_index(target_name);
CREATE INDEX IF NOT EXISTS idx_ref_index_file ON ref_index(file);
CREATE TABLE IF NOT EXISTS summaries (
  name    TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);
"""


class SchemaVersionError(RuntimeError):
    """The database was written by an incompatible hdl-kgraph version."""


# --- Schema migration ladder (issue #74) ----------------------------------
#
# A ``SCHEMA_VERSION`` bump used to force a full re-parse of the whole design
# (the database is a derived cache). For a large design the cache *is* the
# expensive artifact, so an in-place upgrade is worth having when the change is
# purely additive. Each registered step upgrades the database from one schema
# version to the next with additive DDL only (new tables / columns). A
# transition that changed the persisted pass-1 IR encoding is deliberately
# *not* registered — a stored IR blob can't be ``ALTER``ed, so it still routes
# to a full rebuild (see :meth:`SqliteStore.migrate` and ``IR_CODEC_VERSION``).
#
# Only register a step here if an older database can be brought forward without
# re-deriving data: a new table the reader already treats as optional (e.g. an
# empty ``summaries`` table falls back to on-the-fly computation) is safe; a
# table the linker depends on for correctness is not.

MigrationFn = Callable[[sqlite3.Connection], None]


def _migrate_7_to_8(conn: sqlite3.Connection) -> None:
    """v7 -> v8: add the precomputed whole-design ``summaries`` table.

    Created empty; until the next build/update repopulates it, readers fall
    back to computing each summary from the graph (the same path a pre-v8
    database already takes), so the migrated database stays correct.

    Uses a single ``execute`` (not ``executescript``, which would implicitly
    commit the caller's migration transaction).
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS summaries (  name    TEXT PRIMARY KEY,  payload TEXT NOT NULL)"
    )


def _migrate_8_to_9(conn: sqlite3.Connection) -> None:
    """v8 -> v9: drop the stale ``clock_domains`` summary (#176).

    A v8 payload predates ``cdc_analysis``/``alias_collapses``, and a reader
    that defaults a missing status would report a *collapsed* design as
    ``complete`` — the exact false clean bill this release exists to remove.
    Deleting the row makes ``load_summary`` return ``None``, so the reader falls
    through to the out-of-core ``clock_summary_sql``, which computes the new
    fields. No rebuild needed; the next build/update repopulates the row.

    Uses a single ``execute`` (not ``executescript``, which would implicitly
    commit the caller's migration transaction).
    """
    conn.execute("DELETE FROM summaries WHERE name = 'clock_domains'")


#: from_version -> (to_version, upgrade function). A contiguous chain up to
#: ``SCHEMA_VERSION`` is run in order; a gap (or an IR-codec change) routes to a
#: full rebuild instead.
_MIGRATIONS: dict[str, tuple[str, MigrationFn]] = {
    "7": ("8", _migrate_7_to_8),
    "8": ("9", _migrate_8_to_9),
}


@dataclass
class StoredUnit:
    """One compilation unit's persisted pass-1 results (the ``file_irs`` row).

    All three fields are JSON text — kept opaque here so unchanged units
    round-trip through ``update`` byte-for-byte without re-serialization.
    """

    ir: str  # FileIR (ir_codec.ir_to_json)
    macro_events: str  # `define/`undef log (ir_codec.macro_events_to_json)
    included: str  # JSON array of header relpaths spliced into this unit


@dataclass
class FileMeta:
    """Per-file record persisted in the ``files`` table."""

    path: str
    language: Language
    content_hash: str
    size_bytes: int
    parse_error_count: int = 0
    skipped_reason: str | None = None  # None | 'size' | 'pragma_protect' | 'exclude'
    warnings: list[str] = field(default_factory=list)  # preprocessor diagnostics
    # ``file:line: message`` details (capped at parser.base.MAX_PARSE_ERRORS;
    # parse_error_count stays exact beyond the cap).
    parse_errors: list[str] = field(default_factory=list)


# -- row serialization (single source of truth) -------------------------------
# Both the full ``save()`` and the incremental ``save_incremental()`` build
# their rows here, so the two write paths can never drift on column order or
# JSON encoding (a drift would make the incremental row-diff see spurious or,
# worse, missed changes).


def _node_row(node_id: str, data: dict[str, object]) -> tuple[object, ...]:
    line_span = data["line_span"]
    return (
        node_id,
        data["kind"].value,  # type: ignore[attr-defined]
        data["name"],
        data["qualified_name"],
        data["file"],
        line_span[0],  # type: ignore[index]
        line_span[1],  # type: ignore[index]
        data["language"].value,  # type: ignore[attr-defined]
        json.dumps(data["attrs"], sort_keys=True),
    )


def _edge_row(src: str, dst: str, data: dict[str, object]) -> tuple[object, ...]:
    return (
        src,
        dst,
        data["kind"].value,  # type: ignore[attr-defined]
        data["confidence"],
        json.dumps(data["attrs"], sort_keys=True, default=list),
    )


def _file_row(f: FileMeta) -> tuple[object, ...]:
    return (
        f.path,
        f.language.value,
        f.content_hash,
        f.size_bytes,
        f.parse_error_count,
        f.skipped_reason,
        json.dumps(f.warnings),
        json.dumps(f.parse_errors),
    )


def _file_ir_row(path: str, unit: StoredUnit) -> tuple[object, ...]:
    return (path, unit.ir, unit.macro_events, unit.included)


def _file_meta(row: tuple[Any, ...]) -> FileMeta:
    """Decode a ``SELECT * FROM files`` row — the inverse of :func:`_file_row`."""
    return FileMeta(
        path=row[0],
        language=Language(row[1]),
        content_hash=row[2],
        size_bytes=row[3],
        parse_error_count=row[4],
        skipped_reason=row[5],
        warnings=json.loads(row[6]),
        parse_errors=json.loads(row[7]),
    )


def _discrepancy_row(d: Discrepancy) -> tuple[object, ...]:
    return (d.kind, d.backend, d.detail, d.node_id, d.src, d.dst, d.heuristic, d.elaborated)


def _ref_index_row(rec: RefRecord) -> tuple[object, ...]:
    return (rec.file, rec.src_id, rec.edge_kind.value, rec.target_name, int(rec.scoped))


# -- row deserialization (single source of truth) -----------------------------
# Both the full-graph ``load()`` and the bounded ``storage.query`` reader build
# their in-memory nodes/edges here, so the read paths can never drift on how a
# stored row maps back to a graph node/edge.

#: The ``nodes`` columns in table order — every node read does ``SELECT`` of
#: exactly these so :func:`add_node_row` can unpack positionally.
NODE_COLUMNS = "id, kind, name, qualified_name, file, line_start, line_end, language, attrs"
#: The ``edges`` columns in table order, for :func:`add_edge_row`.
EDGE_COLUMNS = "src, dst, kind, confidence, attrs"


def add_node_row(graph: nx.MultiDiGraph, row: tuple[Any, ...]) -> str:
    """Add one ``nodes`` row (in :data:`NODE_COLUMNS` order) to *graph*."""
    node_id, kind, name, qualified_name, file, line_start, line_end, language, attrs = row
    graph.add_node(
        node_id,
        kind=NodeKind(kind),
        name=name,
        qualified_name=qualified_name,
        file=file,
        line_span=(line_start, line_end),
        language=Language(language),
        attrs=json.loads(attrs),
    )
    return str(node_id)


def add_edge_row(graph: nx.MultiDiGraph, row: tuple[Any, ...]) -> None:
    """Add one ``edges`` row (in :data:`EDGE_COLUMNS` order) to *graph*."""
    src, dst, kind, confidence, attrs = row
    graph.add_edge(src, dst, kind=EdgeKind(kind), confidence=confidence, attrs=json.loads(attrs))


def _meta_rows(root: Path, options_hash: str) -> list[tuple[str, str]]:
    return [
        ("schema_version", SCHEMA_VERSION),
        ("ir_codec_version", IR_CODEC_VERSION),
        ("root", str(root.resolve())),
        ("built_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ("tool_version", __version__),
        ("options_hash", options_hash),
    ]


class SqliteStore:
    """Saves and loads the knowledge graph at a fixed database path."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        # Set by save_incremental(): {nodes_upserted, nodes_deleted,
        # edge_srcs_rewritten} for the last incremental write (None after a
        # full save or before any write). Used by scripts/bench_incremental.py
        # to assert write cost scales with the change, not the design size.
        self.last_write_stats: dict[str, int] | None = None

    def save(
        self,
        graph: nx.MultiDiGraph,
        files: list[FileMeta],
        root: Path,
        units: dict[str, StoredUnit] | None = None,
        options_hash: str = "",
        discrepancies: list[Discrepancy] | None = None,
        ref_records: list[RefRecord] | None = None,
        summaries: dict[str, str] | None = None,
    ) -> None:
        """Write the full graph to a sibling temp file, then swap it into place.

        The live database is never written directly: readers see either the
        old or the new complete database, and a crashed build leaves the
        previous one intact (plus a harmless ``.tmp`` leftover).
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.db_path.with_name(self.db_path.name + ".tmp")
        _unlink_db(tmp_path)  # leftover (db + sidecars) from a crashed build
        conn = sqlite3.connect(tmp_path)
        try:
            # Build in WAL mode so the persisted database is already WAL: a
            # later incremental write never has to switch the journal mode
            # (which is impossible while a reader holds the database open), so
            # it can never block on a concurrent reader.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            conn.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                _meta_rows(root, options_hash),
            )
            conn.executemany(
                "INSERT INTO file_irs VALUES (?, ?, ?, ?)",
                [_file_ir_row(path, unit) for path, unit in (units or {}).items()],
            )
            conn.executemany(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [_file_row(f) for f in files],
            )
            conn.executemany(
                "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [_node_row(node_id, data) for node_id, data in graph.nodes(data=True)],
            )
            conn.executemany(
                "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
                [_edge_row(src, dst, data) for src, dst, data in graph.edges(data=True)],
            )
            conn.executemany(
                "INSERT INTO discrepancies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [_discrepancy_row(d) for d in (discrepancies or [])],
            )
            conn.executemany(
                "INSERT INTO ref_index VALUES (?, ?, ?, ?, ?)",
                [_ref_index_row(r) for r in (ref_records or [])],
            )
            conn.executemany(
                "INSERT INTO summaries (name, payload) VALUES (?, ?)",
                list((summaries or {}).items()),
            )
            conn.commit()
            # Fold the WAL back into the main file so the temp database is a
            # self-contained single file ready for the atomic swap.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()  # last connection closing also drops the WAL sidecars
        _replace_into_place(tmp_path, self.db_path)
        # A prior incremental update may have left WAL sidecars beside the live
        # database; they belong to the now-replaced inode, so a reader must not
        # apply them to the fresh file.
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(OSError):
                self.db_path.with_name(self.db_path.name + suffix).unlink(missing_ok=True)
        self.last_write_stats = None

    def save_incremental(
        self,
        graph: nx.MultiDiGraph,
        files: list[FileMeta],
        root: Path,
        units: dict[str, StoredUnit] | None = None,
        options_hash: str = "",
        discrepancies: list[Discrepancy] | None = None,
        ref_records: list[RefRecord] | None = None,
        summaries: dict[str, str] | None = None,
        touched_files: set[str] | None = None,
        affected_srcs: set[str] | None = None,
    ) -> None:
        """Write only the rows that changed since the stored build, in place.

        ``update`` re-links the full graph but usually touches few files. This
        diffs the new graph against the stored rows and writes just the delta,
        so the write cost scales with the change rather than the design size
        (the full ``save()`` rewrites every row every time). The write runs in
        WAL mode under a single ``BEGIN IMMEDIATE`` transaction: a concurrent
        reader never blocks the writer, and a crash mid-write rolls back to the
        previous database. Falls back to a full :meth:`save` when the database
        is missing, foreign, or on an incompatible schema.

        The loaded result is identical to a full ``save()`` of the same graph;
        this is the contract ``test_update_graph_matches_full_rebuild`` pins.
        """
        if not self.db_path.is_file():
            self.save(
                graph, files, root, units, options_hash, discrepancies, ref_records, summaries
            )
            return
        conn = sqlite3.connect(self.db_path)
        conn.isolation_level = None  # we drive BEGIN/COMMIT explicitly
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            try:
                self._check_version(conn)
            except SchemaVersionError:
                conn.close()
                self.save(
                    graph, files, root, units, options_hash, discrepancies, ref_records, summaries
                )
                return
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                # WAL could not be enabled (e.g. a filesystem without shared-
                # memory support). An in-place BEGIN IMMEDIATE here could block
                # a concurrent reader, so fall back to the full save(), whose
                # os.replace swap is non-blocking regardless of journal mode.
                conn.close()
                self.save(
                    graph, files, root, units, options_hash, discrepancies, ref_records, summaries
                )
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                stats = _apply_delta(
                    conn,
                    graph,
                    files,
                    root,
                    units,
                    options_hash,
                    discrepancies,
                    ref_records,
                    summaries,
                    touched_files,
                    affected_srcs,
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            # Keep the WAL sidecar near-empty so a later full save() never
            # risks applying stale frames to the swapped-in file.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.last_write_stats = stats
        finally:
            conn.close()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A read-only connection that waits out a concurrent writer and is
        closed deterministically (an open handle blocks the ``save()`` swap on
        Windows).

        Opened ``mode=ro`` via a file: URI so a read can never create or write
        the database — without it, a plain ``connect`` to a path that a
        concurrent rebuild has momentarily removed would silently materialize an
        empty database. All writers (``save``/``save_incremental``) use their own
        connections, so this only constrains the read paths.
        """
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            yield conn
        finally:
            conn.close()

    def migrate(self) -> str:
        """Upgrade an older-schema database to ``SCHEMA_VERSION`` in place when a
        registered, IR-compatible migration path exists.

        Returns one of:

        * ``"absent"``   — no database file;
        * ``"current"``  — already at ``SCHEMA_VERSION``;
        * ``"migrated"`` — brought forward in place;
        * ``"rebuild"``  — no registered path, a changed IR codec, or a
          foreign/garbage file; the caller should fall back to a full rebuild.

        Never raises on a version/format problem — it reports ``"rebuild"`` and
        leaves the database untouched. Read paths are unaffected: they stay
        read-only and still raise :class:`SchemaVersionError` until a writer
        (``update``/``watch``) migrates the database.
        """
        if not self.db_path.is_file():
            return "absent"
        conn = sqlite3.connect(self.db_path)
        conn.isolation_level = None  # drive BEGIN/COMMIT explicitly
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            try:
                meta = dict(conn.execute("SELECT key, value FROM meta"))
            except sqlite3.DatabaseError:
                return "rebuild"  # foreign / garbage / missing meta table
            version = meta.get("schema_version")
            if version == SCHEMA_VERSION:
                return "current"
            if version is None:
                return "rebuild"
            # Build a contiguous chain version -> ... -> SCHEMA_VERSION.
            chain: list[MigrationFn] = []
            cur = version
            seen: set[str] = set()
            while cur != SCHEMA_VERSION:
                step = _MIGRATIONS.get(cur)
                if step is None or cur in seen:
                    return "rebuild"  # no registered path (or a cycle)
                seen.add(cur)
                chain.append(step[1])
                cur = step[0]
            # A change to the persisted IR encoding can't be migrated in place.
            # Databases written before this feature carry no ir_codec_version
            # key; trust the registered chain for them (only IR-compatible steps
            # are ever registered).
            stored_codec = meta.get("ir_codec_version")
            if stored_codec is not None and stored_codec != IR_CODEC_VERSION:
                return "rebuild"
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                return "rebuild"  # can't safely write in place; rebuild instead
            conn.execute("BEGIN IMMEDIATE")
            try:
                for fn in chain:
                    fn(conn)
                conn.executemany(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    [
                        ("schema_version", SCHEMA_VERSION),
                        ("ir_codec_version", IR_CODEC_VERSION),
                    ],
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return "migrated"
        finally:
            conn.close()

    def _check_version(self, conn: sqlite3.Connection) -> dict[str, str]:
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
        except sqlite3.DatabaseError as exc:
            # OperationalError is a DatabaseError subclass: re-raise transient
            # problems (e.g. 'database is locked'), but remap a missing meta
            # table ('no such table') or a non-SQLite/foreign file ('file is
            # not a database') to the schema error so callers fall back to a
            # full rebuild.
            message = str(exc).lower()
            if "no such table" not in message and "not a database" not in message:
                raise
            raise SchemaVersionError(f"{self.db_path} is not an hdl-kgraph database") from exc
        version = meta.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"{self.db_path} has graph schema version {version!r}; this "
                f"hdl-kgraph expects {SCHEMA_VERSION!r}. Re-run `hdl-kgraph build`."
            )
        return meta

    def load_meta(self) -> dict[str, str]:
        """Load the meta key/values (checking the schema version)."""
        with self._connect() as conn:
            return self._check_version(conn)

    def load_file_hashes(self) -> dict[str, str]:
        """path -> content_hash for every stored file record."""
        with self._connect() as conn:
            self._check_version(conn)
            return dict(conn.execute("SELECT path, content_hash FROM files"))

    def load_file_metas(self) -> list[FileMeta]:
        """Every stored :class:`FileMeta`, without hydrating the graph.

        The same list :meth:`load` returns as its second element — but reading
        the ``files`` table alone, so a caller that only needs the source
        inventory (``bench``) does not pay for a whole-graph load.
        """
        with self._connect() as conn:
            self._check_version(conn)
            return [_file_meta(row) for row in conn.execute("SELECT * FROM files")]

    def load_file_warnings(self) -> dict[str, list[str]]:
        """path -> preprocessor warnings, for files that have any.

        ``update`` carries these forward for units re-linked from stored IRs
        without re-running the preprocessor.
        """
        with self._connect() as conn:
            self._check_version(conn)
            return {
                path: json.loads(warnings)
                for path, warnings in conn.execute(
                    "SELECT path, warnings FROM files WHERE warnings != '[]'"
                )
            }

    def load_file_errors(self) -> dict[str, tuple[int, list[str]]]:
        """path -> (parse_error_count, parse_error details), for files that have any.

        ``update`` carries these forward for clean units it does not re-decode on
        the selective bounded path (the parse-error telemetry otherwise lives only
        in the stored IR).
        """
        with self._connect() as conn:
            self._check_version(conn)
            return {
                path: (count, json.loads(errors))
                for path, count, errors in conn.execute(
                    "SELECT path, parse_error_count, parse_errors FROM files "
                    "WHERE parse_error_count != 0"
                )
            }

    def load_dependency_graph(self) -> nx.MultiDiGraph:
        """The preprocessor-dependency subgraph (M4 dirty closure).

        Only INCLUDES / DEFINES_MACRO / USES_MACRO edges and FILE / MACRO /
        INCLUDE_FILE nodes — a fraction of the full graph, so ``update``
        skips rehydrating everything else.
        """
        with self._connect() as conn:
            self._check_version(conn)
            graph = nx.MultiDiGraph()
            for node_id, kind, name, attrs in conn.execute(
                "SELECT id, kind, name, attrs FROM nodes WHERE kind IN (?, ?, ?)",
                (NodeKind.FILE.value, NodeKind.MACRO.value, NodeKind.INCLUDE_FILE.value),
            ):
                graph.add_node(node_id, kind=NodeKind(kind), name=name, attrs=json.loads(attrs))
            for src, dst, kind in conn.execute(
                "SELECT src, dst, kind FROM edges WHERE kind IN (?, ?, ?)",
                (
                    EdgeKind.INCLUDES.value,
                    EdgeKind.DEFINES_MACRO.value,
                    EdgeKind.USES_MACRO.value,
                ),
            ):
                if src in graph and dst in graph:
                    graph.add_edge(src, dst, kind=EdgeKind(kind))
            return graph

    def load_units(self) -> dict[str, StoredUnit]:
        """Load the per-unit pass-1 IRs persisted for incremental updates."""
        with self._connect() as conn:
            self._check_version(conn)
            return {
                path: StoredUnit(ir=ir, macro_events=events, included=included)
                for path, ir, events, included in conn.execute("SELECT * FROM file_irs")
            }

    def load_macro_events(self) -> dict[str, tuple[str, str]]:
        """``path -> (macro_events, included)`` for every stored unit, **without**
        the large ``ir`` blob (#119 selective decode).

        The bounded `update` path must still replay each clean unit's macro events
        in compile order (the shared preprocessor table feeds dirty re-parses), but
        only needs the small columns to do so — the full IR is decoded later, and
        only for the dirty/affected units (:meth:`load_units_for`)."""
        with self._connect() as conn:
            self._check_version(conn)
            return {
                path: (events, included)
                for path, events, included in conn.execute(
                    "SELECT path, macro_events, included FROM file_irs"
                )
            }

    def load_units_for(self, paths: set[str]) -> dict[str, StoredUnit]:
        """Full stored units (incl. the ``ir`` blob) for just *paths* — the
        dirty/affected units the bounded re-link actually decodes (#119)."""
        out: dict[str, StoredUnit] = {}
        with self._connect() as conn:
            self._check_version(conn)
            for chunk in _chunked(set(paths)):
                placeholders = ", ".join("?" for _ in chunk)
                for path, ir, events, included in conn.execute(
                    f"SELECT path, ir, macro_events, included FROM file_irs "
                    f"WHERE path IN ({placeholders})",
                    tuple(chunk),
                ):
                    out[path] = StoredUnit(ir=ir, macro_events=events, included=included)
        return out

    def load_discrepancies(self) -> list[Discrepancy]:
        """The M7 enrichment findings (empty for a non-enriched build)."""
        with self._connect() as conn:
            self._check_version(conn)
            return [
                Discrepancy(
                    kind=row[0],
                    backend=row[1],
                    detail=row[2],
                    node_id=row[3],
                    src=row[4],
                    dst=row[5],
                    heuristic=row[6],
                    elaborated=row[7],
                )
                for row in conn.execute(
                    "SELECT kind, backend, detail, node_id, src, dst, heuristic, elaborated "
                    "FROM discrepancies"
                )
            ]

    def load_ref_index(self) -> list[RefRecord]:
        """All persisted pass-2 reference records (incremental-link reverse index)."""
        with self._connect() as conn:
            self._check_version(conn)
            return [
                RefRecord(
                    file=row[0],
                    src_id=row[1],
                    edge_kind=EdgeKind(row[2]),
                    target_name=row[3],
                    scoped=bool(row[4]),
                )
                for row in conn.execute(
                    "SELECT file, src_id, edge_kind, target_name, scoped FROM ref_index"
                )
            ]

    def load_summary(self, name: str) -> str | None:
        """The precomputed JSON payload for whole-design summary *name*, or None.

        ``None`` means the build predates summaries (or did not compute this
        one), so the reader falls back to computing it from the full graph.
        """
        with self._connect() as conn:
            self._check_version(conn)
            row = conn.execute("SELECT payload FROM summaries WHERE name = ?", (name,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a single ``meta`` key/value — e.g. post-build telemetry written
        after persist (``build_stats``). A small in-place write to the live (WAL)
        database; readers tolerate the key's absence on older DBs."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
        finally:
            conn.close()

    def save_summaries(self, summaries: dict[str, str]) -> None:
        """Replace the whole-design ``summaries`` table in one small transaction.

        Used by the bounded-link path (#119), which writes the node/edge/ref
        delta first (leaving summaries untouched) and then recomputes the
        summaries from the updated database via the SQL-native scans."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM summaries")
            conn.executemany(
                "INSERT INTO summaries (name, payload) VALUES (?, ?)", list(summaries.items())
            )
            conn.commit()
        finally:
            conn.close()

    def replace_test_covers(self, edges: list[Edge]) -> None:
        """Replace every TEST_COVERS edge with *edges* in one transaction.

        The bounded re-link's partial graph can't run ``derive_test_covers``
        (which is whole-graph), so the bounded ``update`` path re-derives the full
        set out-of-core (``summaries.test_covers_sql``) and reconciles it here.
        A full replace is byte-identical — the equivalence gate compares the
        *set* of loaded edges — and bounded by the small TEST_COVERS count."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM edges WHERE kind = ?", (EdgeKind.TEST_COVERS.value,))
            conn.executemany(
                "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
                [
                    _edge_row(
                        e.src, e.dst, {"kind": e.kind, "confidence": e.confidence, "attrs": e.attrs}
                    )
                    for e in edges
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def graph_counts(self) -> tuple[int, int, int]:
        """``(nodes, edges, unresolved_nodes)`` from the live DB without loading
        the graph — for build-report counts on the bounded-link path."""
        with self._connect() as conn:
            self._check_version(conn)
            nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            unresolved = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE json_extract(attrs, '$.unresolved') = 1"
            ).fetchone()[0]
            return int(nodes), int(edges), int(unresolved)

    def load(self) -> tuple[nx.MultiDiGraph, list[FileMeta], dict[str, str]]:
        """Load (graph, file metadata, meta key/values) from the database."""
        with self._connect() as conn:
            meta = self._check_version(conn)
            files = [_file_meta(row) for row in conn.execute("SELECT * FROM files")]
            graph = nx.MultiDiGraph()
            for row in conn.execute(f"SELECT {NODE_COLUMNS} FROM nodes"):
                add_node_row(graph, row)
            for row in conn.execute(f"SELECT {EDGE_COLUMNS} FROM edges"):
                add_edge_row(graph, row)
            return graph, files, meta


def _apply_delta(
    conn: sqlite3.Connection,
    graph: nx.MultiDiGraph,
    files: list[FileMeta],
    root: Path,
    units: dict[str, StoredUnit] | None,
    options_hash: str,
    discrepancies: list[Discrepancy] | None,
    ref_records: list[RefRecord] | None = None,
    summaries: dict[str, str] | None = None,
    touched_files: set[str] | None = None,
    affected_srcs: set[str] | None = None,
) -> dict[str, int]:
    """UPSERT/delete only the rows that differ from what is stored.

    Runs inside the caller's open transaction. Returns write-volume stats.

    When *touched_files* and *affected_srcs* are given (the incremental-link
    path), the diff is *scoped* to the dirty closure — only those files' rows
    and the re-resolved clean refs are read and reconciled, never the whole
    nodes/edges tables. Otherwise the full tables are diffed (correct for any
    graph, including a full re-link fallback).
    """
    units = units or {}
    discrepancies = discrepancies or []
    ref_records = ref_records or []
    # NOTE: summaries is left as-is here — ``None`` means "do not touch the
    # summaries table" (the bounded-link path refreshes them via SQL after the
    # delta write), while ``{}`` means "clear it". _refresh_small_tables honors
    # the distinction.

    if touched_files is not None and affected_srcs is not None:
        return _apply_delta_scoped(
            conn,
            graph,
            files,
            root,
            units,
            options_hash,
            discrepancies,
            ref_records,
            summaries,
            touched_files,
            affected_srcs,
        )

    # -- nodes (PK id): UPSERT changed rows, delete vanished ids ---------------
    stored_nodes = {row[0]: row for row in conn.execute("SELECT * FROM nodes")}
    new_nodes = {node_id: _node_row(node_id, data) for node_id, data in graph.nodes(data=True)}
    node_upserts = [row for node_id, row in new_nodes.items() if stored_nodes.get(node_id) != row]
    node_deletes = [(node_id,) for node_id in stored_nodes if node_id not in new_nodes]
    conn.executemany(
        "INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", node_upserts
    )
    conn.executemany("DELETE FROM nodes WHERE id = ?", node_deletes)

    # -- edges (no PK; a multiset grouped by src) -----------------------------
    # Edges carry no key, so the unit of replacement is "all edges from one
    # src". For each src whose edge multiset differs (including a src whose
    # edges only changed because a *target* in an unchanged file was removed/
    # re-resolved) we delete and re-insert just that src's rows. Counter, not
    # set: a MultiDiGraph may hold identical parallel edges.
    stored_edges: defaultdict[str, Counter[tuple[object, ...]]] = defaultdict(Counter)
    for row in conn.execute("SELECT src, dst, kind, confidence, attrs FROM edges"):
        stored_edges[row[0]][row] += 1
    new_edges: defaultdict[str, Counter[tuple[object, ...]]] = defaultdict(Counter)
    for src, dst, data in graph.edges(data=True):
        new_edges[src][_edge_row(src, dst, data)] += 1
    edge_srcs_rewritten = 0
    for src in stored_edges.keys() | new_edges.keys():
        if stored_edges.get(src) == new_edges.get(src):
            continue
        edge_srcs_rewritten += 1
        conn.execute("DELETE FROM edges WHERE src = ?", (src,))
        rows = [row for row, count in new_edges.get(src, Counter()).items() for _ in range(count)]
        conn.executemany("INSERT INTO edges VALUES (?, ?, ?, ?, ?)", rows)

    # -- files / file_irs (PK path): UPSERT changed, delete removed -----------
    # A removed source file leaves no FileMeta and no unit, so its rows in both
    # tables are deleted here; a stale files row would carry a stale
    # content_hash that the next scan_changes would miss.
    _upsert_by_path(
        conn, "files", "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", {f.path: _file_row(f) for f in files}
    )
    _upsert_by_path(
        conn,
        "file_irs",
        "VALUES (?, ?, ?, ?)",
        {path: _file_ir_row(path, unit) for path, unit in units.items()},
    )

    # -- ref_index (no PK; a multiset grouped by file) ------------------------
    # Like edges, ref rows carry no key; replace per owning unit so a one-file
    # edit rewrites only that unit's ref rows. Counter handles duplicate refs
    # (a header spliced into several units).
    stored_refs: defaultdict[str, Counter[tuple[object, ...]]] = defaultdict(Counter)
    for row in conn.execute("SELECT file, src_id, edge_kind, target_name, scoped FROM ref_index"):
        stored_refs[row[0]][row] += 1
    new_refs: defaultdict[str, Counter[tuple[object, ...]]] = defaultdict(Counter)
    for rec in ref_records:
        new_refs[rec.file][_ref_index_row(rec)] += 1
    for file in stored_refs.keys() | new_refs.keys():
        if stored_refs.get(file) == new_refs.get(file):
            continue
        conn.execute("DELETE FROM ref_index WHERE file = ?", (file,))
        rows = [row for row, count in new_refs.get(file, Counter()).items() for _ in range(count)]
        conn.executemany("INSERT INTO ref_index VALUES (?, ?, ?, ?, ?)", rows)

    _refresh_small_tables(conn, root, options_hash, discrepancies, summaries)

    return {
        "nodes_upserted": len(node_upserts),
        "nodes_deleted": len(node_deletes),
        "edge_srcs_rewritten": edge_srcs_rewritten,
    }


#: SQLite caps host parameters per statement; chunk ``IN (...)`` lists under it.
_SQL_VAR_LIMIT = 900


def _chunked(items: set[str]) -> Iterator[list[str]]:
    seq = list(items)
    for start in range(0, len(seq), _SQL_VAR_LIMIT):
        yield seq[start : start + _SQL_VAR_LIMIT]


def _refresh_small_tables(
    conn: sqlite3.Connection,
    root: Path,
    options_hash: str,
    discrepancies: list[Discrepancy],
    summaries: dict[str, str] | None,
) -> None:
    """Wholesale-refresh the small tables both delta paths share.

    ``discrepancies`` and ``summaries`` are a handful of rows (enrich findings;
    the two whole-design JSON blobs), and ``meta`` is five keys — cheaper to
    rewrite than to diff. ``summaries=None`` leaves the summaries table
    untouched (the bounded-link path rewrites it via SQL after the delta write).
    """
    conn.execute("DELETE FROM discrepancies")
    conn.executemany(
        "INSERT INTO discrepancies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [_discrepancy_row(d) for d in discrepancies],
    )
    if summaries is not None:
        conn.execute("DELETE FROM summaries")
        conn.executemany(
            "INSERT INTO summaries (name, payload) VALUES (?, ?)", list(summaries.items())
        )
    conn.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", _meta_rows(root, options_hash)
    )


def _apply_delta_scoped(
    conn: sqlite3.Connection,
    graph: nx.MultiDiGraph,
    files: list[FileMeta],
    root: Path,
    units: dict[str, StoredUnit],
    options_hash: str,
    discrepancies: list[Discrepancy],
    ref_records: list[RefRecord],
    summaries: dict[str, str] | None,
    touched_files: set[str],
    affected_srcs: set[str],
) -> dict[str, int]:
    """Delta write scoped to the dirty closure (incremental-link path only).

    ``link_incremental`` (#64) guarantees that the only rows which can differ
    from the stored build are: nodes owned by a touched/removed file, *fileless*
    nodes (unresolved stubs, filelist/library) which it may add or drop, and the
    ``affected_srcs`` clean references it re-resolved. Every other row is
    byte-identical to the prior build, so this reads and reconciles only those —
    the diff cost scales with the change, not the design size. The loaded result
    is identical to a full ``save()``; the byte-identical-rebuild fuzz suite
    (``tests/test_incremental_equivalence.py``) pins that.
    """
    # -- nodes: scope = a touched/removed file, or fileless (stubs etc.) -------
    stored_nodes = {row[0]: row for row in _select_scoped_nodes(conn, touched_files)}
    new_nodes = {
        node_id: _node_row(node_id, data)
        for node_id, data in graph.nodes(data=True)
        if data["file"] in touched_files or data["file"] == ""
    }
    node_upserts = [row for node_id, row in new_nodes.items() if stored_nodes.get(node_id) != row]
    node_deletes = [(node_id,) for node_id in stored_nodes if node_id not in new_nodes]
    conn.executemany(
        "INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", node_upserts
    )
    conn.executemany("DELETE FROM nodes WHERE id = ?", node_deletes)

    # -- edges: scope = srcs in those nodes plus the re-resolved clean srcs ----
    # An edge's src multiset can change only if the src is in a touched/fileless
    # node (reparsed/added/removed) or is an affected clean ref (re-resolved);
    # every other src's edges are reused verbatim by link_incremental.
    candidate_srcs = set(stored_nodes) | set(new_nodes) | affected_srcs
    stored_edges: defaultdict[str, Counter[tuple[object, ...]]] = defaultdict(Counter)
    for row in _select_edges_by_src(conn, candidate_srcs):
        stored_edges[row[0]][row] += 1
    new_edges: defaultdict[str, Counter[tuple[object, ...]]] = defaultdict(Counter)
    for src, dst, data in graph.edges(data=True):
        if src in candidate_srcs:
            new_edges[src][_edge_row(src, dst, data)] += 1
    edge_srcs_rewritten = 0
    for src in candidate_srcs:
        if stored_edges.get(src) == new_edges.get(src):
            continue
        edge_srcs_rewritten += 1
        conn.execute("DELETE FROM edges WHERE src = ?", (src,))
        rows = [row for row, count in new_edges.get(src, Counter()).items() for _ in range(count)]
        conn.executemany("INSERT INTO edges VALUES (?, ?, ?, ?, ?)", rows)

    # -- files / file_irs (PK path): scope = the touched/removed paths ---------
    _upsert_by_path_scoped(
        conn,
        "files",
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        {f.path: _file_row(f) for f in files if f.path in touched_files},
        touched_files,
    )
    _upsert_by_path_scoped(
        conn,
        "file_irs",
        "VALUES (?, ?, ?, ?)",
        {path: _file_ir_row(path, unit) for path, unit in units.items() if path in touched_files},
        touched_files,
    )

    # -- ref_index (grouped by file): scope = the touched/removed files --------
    # Clean files' ref rows are regenerated identically, so only touched files
    # can differ (a removed file's rows are deleted: no new refs for it).
    stored_refs: defaultdict[str, Counter[tuple[object, ...]]] = defaultdict(Counter)
    for row in _select_refs_by_file(conn, touched_files):
        stored_refs[row[0]][row] += 1
    new_refs: defaultdict[str, Counter[tuple[object, ...]]] = defaultdict(Counter)
    for rec in ref_records:
        if rec.file in touched_files:
            new_refs[rec.file][_ref_index_row(rec)] += 1
    for file in touched_files:
        if stored_refs.get(file) == new_refs.get(file):
            continue
        conn.execute("DELETE FROM ref_index WHERE file = ?", (file,))
        rows = [row for row, count in new_refs.get(file, Counter()).items() for _ in range(count)]
        conn.executemany("INSERT INTO ref_index VALUES (?, ?, ?, ?, ?)", rows)

    _refresh_small_tables(conn, root, options_hash, discrepancies, summaries)

    return {
        "nodes_upserted": len(node_upserts),
        "nodes_deleted": len(node_deletes),
        "edge_srcs_rewritten": edge_srcs_rewritten,
        # Read volume: stored rows the scoped diff had to read. Bounded by the
        # dirty closure, not the design — scripts/bench_incremental.py asserts it.
        "nodes_scanned": len(stored_nodes),
        "edge_srcs_scanned": len(candidate_srcs),
    }


def _select_scoped_nodes(conn: sqlite3.Connection, files: set[str]) -> Iterator[tuple[Any, ...]]:
    """Stored ``nodes`` rows owned by a file in *files*, plus all fileless nodes.

    Fileless nodes (``file = ''``) — unresolved stubs and filelist/library nodes
    — are the ones the linker creates/drops without a source file, so they must
    be reconciled on every incremental write."""
    for chunk in _chunked(files):
        placeholders = ", ".join("?" for _ in chunk)
        yield from conn.execute(f"SELECT * FROM nodes WHERE file IN ({placeholders})", tuple(chunk))
    yield from conn.execute("SELECT * FROM nodes WHERE file = ''")


def _select_edges_by_src(conn: sqlite3.Connection, srcs: set[str]) -> Iterator[tuple[Any, ...]]:
    """Stored ``edges`` rows whose ``src`` is in *srcs* (uses ``idx_edges_src``)."""
    for chunk in _chunked(srcs):
        placeholders = ", ".join("?" for _ in chunk)
        yield from conn.execute(
            f"SELECT src, dst, kind, confidence, attrs FROM edges WHERE src IN ({placeholders})",
            tuple(chunk),
        )


def _select_refs_by_file(conn: sqlite3.Connection, files: set[str]) -> Iterator[tuple[Any, ...]]:
    """Stored ``ref_index`` rows owned by a file in *files* (``idx_ref_index_file``)."""
    for chunk in _chunked(files):
        placeholders = ", ".join("?" for _ in chunk)
        yield from conn.execute(
            f"SELECT file, src_id, edge_kind, target_name, scoped FROM ref_index "
            f"WHERE file IN ({placeholders})",
            tuple(chunk),
        )


def _upsert_by_path_scoped(
    conn: sqlite3.Connection,
    table: str,
    values_clause: str,
    new_rows: dict[str, tuple[object, ...]],
    scope_paths: set[str],
) -> None:
    """:func:`_upsert_by_path` restricted to *scope_paths*.

    Stored rows are read only for the scoped paths, and deletes are confined to
    them too — a path outside the scope was not touched, so its row is unchanged.
    """
    stored: dict[str, tuple[object, ...]] = {}
    for chunk in _chunked(scope_paths):
        placeholders = ", ".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT * FROM {table} WHERE path IN ({placeholders})", tuple(chunk)
        ):
            stored[row[0]] = row
    conn.executemany(
        f"INSERT OR REPLACE INTO {table} {values_clause}",
        [row for path, row in new_rows.items() if stored.get(path) != row],
    )
    conn.executemany(
        f"DELETE FROM {table} WHERE path = ?",
        [(path,) for path in stored if path not in new_rows],
    )


def _upsert_by_path(
    conn: sqlite3.Connection,
    table: str,
    values_clause: str,
    new_rows: dict[str, tuple[object, ...]],
) -> None:
    """UPSERT path-keyed rows that changed and delete paths no longer present."""
    stored = {row[0]: row for row in conn.execute(f"SELECT * FROM {table}")}
    conn.executemany(
        f"INSERT OR REPLACE INTO {table} {values_clause}",
        [row for path, row in new_rows.items() if stored.get(path) != row],
    )
    conn.executemany(
        f"DELETE FROM {table} WHERE path = ?",
        [(path,) for path in stored if path not in new_rows],
    )


def _unlink_db(db_path: Path) -> None:
    """Remove a database file and its WAL sidecars, ignoring absence."""
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def _replace_into_place(tmp_path: Path, db_path: Path) -> None:
    """``os.replace`` with retries.

    Atomic and lock-free on POSIX; on Windows the swap fails with
    ``PermissionError`` while a reader briefly holds the destination open,
    so back off and retry (~6 s total) before giving up.
    """
    delay = 0.05
    for _ in range(7):
        try:
            os.replace(tmp_path, db_path)
            return
        except PermissionError:
            time.sleep(delay)
            delay *= 2
    os.replace(tmp_path, db_path)
