"""Change detection and dirty-set closure for incremental rebuilds (M4).

``update`` re-parses the files whose content hash changed plus their
preprocessor-dependent files, derived from the *stored* graph:

* reverse ``INCLUDES`` — editing (or removing) a header dirties every unit
  that spliced it;
* ``DEFINES_MACRO`` → reverse ``USES_MACRO`` — editing a file that defines a
  macro dirties every file that expanded it;
* unresolved ``INCLUDE_FILE`` stubs — an *added* file whose path now
  satisfies a previously failing ``\\`include`` dirties the includers.

Known limit (documented): the closure reads the stored graph, so an edit
that *newly* defines a macro some other unchanged file already uses by name
is not detected — ``update --full`` covers that case. Changed filelist
defines/incdirs change the build-options fingerprint instead and trigger a
full rebuild upstream.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx

from hdl_kgraph.config import CONFIG_FILENAME
from hdl_kgraph.graph.builder import DEFINITION_KINDS, RefRecord, ref_target_kinds
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.schema import EdgeKind, NodeKind

_FILE_ID_PREFIX = "file:"

#: A resolution-target name key: (target node kind, name).
DefKey = tuple[NodeKind, str]


def definition_profiles(nodes: Iterable[tuple[str, Mapping[str, Any]]]) -> dict[DefKey, tuple]:
    """Map ``(kind, name)`` -> a hashable profile of its defining nodes.

    The profile captures every node field pass-2 resolution reads
    (``file``/``conditional``/``library``/``qualified_name``/``language``), so
    two builds whose resolution of a name would differ have different profiles
    for that name. Only :data:`DEFINITION_KINDS` participate — the kinds a
    global-name ref can resolve to.
    """
    buckets: dict[DefKey, list[tuple[str, ...]]] = defaultdict(list)
    for node_id, data in nodes:
        kind = data["kind"]
        if kind not in DEFINITION_KINDS:
            continue
        attrs = data.get("attrs") or {}
        buckets[(kind, str(data["name"]))].append(
            (
                node_id,
                str(data.get("file", "")),
                str(bool(attrs.get("conditional"))),
                str(attrs.get("library")),
                str(data.get("qualified_name", "")),
                getattr(data.get("language"), "value", str(data.get("language"))),
            )
        )
    return {key: tuple(sorted(rows)) for key, rows in buckets.items()}


def changed_definition_names(
    prior: Mapping[DefKey, tuple], new: Mapping[DefKey, tuple]
) -> set[DefKey]:
    """Definition names whose global profile changed between two builds."""
    return {key for key in prior.keys() | new.keys() if prior.get(key) != new.get(key)}


def changed_target_names(
    prior_graph: nx.MultiDiGraph, file_irs: list[FileIR], dirty_files: set[str]
) -> set[DefKey]:
    """``(kind, name)`` definitions touched by a dirty/removed file.

    A conservative superset of the names whose resolution can change: any
    definition declared in a reparsed/removed file (prior side from the stored
    graph, new side from the fresh IRs). This intentionally over-includes (it
    flags a module whose *children* changed, e.g. a port added, even though its
    own node is unchanged) so a clean unit connecting to it re-resolves; the
    precise profile diff is a later refinement (#64-D).
    """
    changed: set[DefKey] = set()
    for _, data in prior_graph.nodes(data=True):
        if data["kind"] in DEFINITION_KINDS and data.get("file", "") in dirty_files:
            changed.add((data["kind"], data["name"]))
    for ir in file_irs:
        for node in ir.nodes:
            if node.kind in DEFINITION_KINDS and node.file in dirty_files:
                changed.add((node.kind, node.name))
    return changed


def incremental_link_safe(
    enrich: bool,
    has_vhdl: bool,
    has_binds: bool,
    has_cocotb: bool = False,
    has_sdc: bool = False,
) -> str | None:
    """Reason the incremental linker must defer to a full re-link, or None.

    The SV MVP (#64-B) does not yet model VHDL library/architecture/config
    scoping, SV bind / VHDL configuration binding state, or enrichment, so
    those fall back to a full (still parse-incremental) re-link. cocotb
    ``dut.<signal>`` resolution is cross-file (the ref lives in the .py, the
    signal in the DUT module), which the dirty-closure ref index does not model,
    so it falls back too — correctness over a marginal incremental win. SDC
    (M10) is the same shape: CONSTRAINS refs are cross-file and the
    ``create_clock`` evidence upgrade rewrites CLOCKED_BY edges design-wide.
    """
    if enrich:
        return "enrichment is not incremental"
    if has_vhdl:
        return "VHDL incremental link not supported yet"
    if has_binds:
        return "bind/configuration directives not supported yet"
    if has_cocotb:
        return "cocotb cross-file DUT resolution not incremental"
    if has_sdc:
        return "SDC constraint resolution not incremental"
    return None


def affected_clean_refs(
    ref_records: Iterable[RefRecord], changed_names: set[DefKey]
) -> set[tuple[str, str, EdgeKind]]:
    """Refs that may resolve differently because a name they target changed.

    Returns ``(file, src_id, edge_kind)`` keys for every ref whose
    ``target_name`` has a changed definition of a kind the ref could resolve
    to. This is the resolution neighborhood the incremental linker must
    re-resolve in addition to the refs in reparsed files.
    """
    kinds_by_name: dict[str, set[NodeKind]] = defaultdict(set)
    for kind, name in changed_names:
        kinds_by_name[name].add(kind)
    affected: set[tuple[str, str, EdgeKind]] = set()
    for rec in ref_records:
        kinds = kinds_by_name.get(rec.target_name)
        if kinds and (ref_target_kinds(rec.edge_kind) & kinds):
            affected.add((rec.file, rec.src_id, rec.edge_kind))
    return affected


#: Non-HDL inputs that still invalidate a build (filelists, config).
EXTRA_SUFFIXES = frozenset({".f", ".vc"})


@dataclass
class ChangeSet:
    """Hash-diff of the stored file table against the current tree."""

    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.changed or self.added or self.removed)


def diff_hashes(stored: Mapping[str, str], current: Mapping[str, str]) -> ChangeSet:
    """Compare content hashes by relpath (skipped files hash equal-empty)."""
    return ChangeSet(
        changed=sorted(p for p, h in current.items() if p in stored and stored[p] != h),
        added=sorted(p for p in current if p not in stored),
        removed=sorted(p for p in stored if p not in current),
    )


def _file_relpath(node_id: str) -> str | None:
    if node_id.startswith(_FILE_ID_PREFIX):
        return node_id[len(_FILE_ID_PREFIX) :]
    return None


def dirty_closure(graph: nx.MultiDiGraph, seeds: Mapping[str, str]) -> dict[str, str]:
    """Expand *seeds* (relpath -> reason) through include/macro dependents.

    Returns seeds plus every transitively dependent file, each mapped to the
    reason it must be re-parsed.
    """
    dirty = dict(seeds)
    pending = list(seeds)
    while pending:
        relpath = pending.pop()
        file_id = _FILE_ID_PREFIX + relpath
        if file_id not in graph:
            continue
        dependents: list[tuple[str, str]] = []
        for src, _, data in graph.in_edges(file_id, data=True):
            if data["kind"] is EdgeKind.INCLUDES:
                includer = _file_relpath(src)
                if includer is not None:
                    dependents.append((includer, f"includes {relpath}"))
        for _, macro_id, data in graph.out_edges(file_id, data=True):
            if data["kind"] is not EdgeKind.DEFINES_MACRO:
                continue
            macro_name = graph.nodes[macro_id]["name"]
            for user, _, use in graph.in_edges(macro_id, data=True):
                if use["kind"] is EdgeKind.USES_MACRO:
                    user_rel = _file_relpath(user)
                    if user_rel is not None:
                        dependents.append((user_rel, f"uses `{macro_name}"))
        for dep, reason in dependents:
            if dep not in dirty:
                dirty[dep] = reason
                pending.append(dep)
    return dirty


def newly_resolvable_includes(graph: nx.MultiDiGraph, added: Iterable[str]) -> dict[str, str]:
    """Files whose unresolved ``\\`include`` may now resolve to an added file."""
    added = list(added)
    if not added:
        return {}
    dirty: dict[str, str] = {}
    for node_id, data in graph.nodes(data=True):
        if data["kind"] is not NodeKind.INCLUDE_FILE or not data["attrs"].get("unresolved"):
            continue
        path_text = data["name"]
        if not any(a == path_text or a.endswith("/" + path_text) for a in added):
            continue
        for src, _, edge in graph.in_edges(node_id, data=True):
            if edge["kind"] is EdgeKind.INCLUDES:
                includer = _file_relpath(src)
                if includer is not None:
                    dirty.setdefault(includer, f"include {path_text!r} now resolvable")
    return dirty


def is_build_input(relpath: str, suffixes: frozenset[str]) -> bool:
    """True for paths whose change can invalidate a build (HDL/.f/config)."""
    path = Path(relpath)
    return path.suffix in suffixes or path.suffix in EXTRA_SUFFIXES or path.name == CONFIG_FILENAME


def reject_option_like_ref(ref: str, tool: str) -> None:
    """Guard against argument injection via a VCS ref/revision.

    The ref is passed to ``git``/``svn`` as a positional argument, but a value
    that begins with ``-`` (e.g. ``--output=…``, ``-G<regex>``) would be parsed
    as an *option* instead of a revision. No legal git refname or svn revision
    begins with ``-``, so reject such values rather than hand them to the tool.
    Raises ``RuntimeError`` (the same error type the backends already normalize
    failures to) so the CLI maps it to its "error" exit code.
    """
    if not ref or ref.startswith("-"):
        raise RuntimeError(f"{tool}: refusing ref {ref!r}: looks like an option, not a revision")


def detect_git_changes(base: Path, ref: str, suffixes: frozenset[str]) -> ChangeSet:
    """Diff the working tree against *ref*, filtered to build inputs.

    Raises ``RuntimeError`` when *ref* looks like an option, when git is
    unavailable, or when *base* is not inside a work tree.
    """
    reject_option_like_ref(ref, "git")
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-status", ref, "--"],
            cwd=base,
            capture_output=True,
            text=True,
            check=True,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=base,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git executable not found") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.strip() or "git diff failed") from exc

    changes = ChangeSet()
    for line in diff.stdout.splitlines():
        status, _, rest = line.partition("\t")
        if not rest:
            continue
        # Renames/copies (R100\told\tnew) list old then new path.
        paths = rest.split("\t")
        if status.startswith(("R", "C")) and len(paths) == 2:
            old, new = paths
            if is_build_input(old, suffixes):
                changes.removed.append(old)
            if is_build_input(new, suffixes):
                changes.added.append(new)
            continue
        path = paths[0]
        if not is_build_input(path, suffixes):
            continue
        if status.startswith("A"):
            changes.added.append(path)
        elif status.startswith("D"):
            changes.removed.append(path)
        else:
            changes.changed.append(path)
    changes.added.extend(p for p in untracked.stdout.splitlines() if is_build_input(p, suffixes))
    for bucket in (changes.changed, changes.added, changes.removed):
        bucket.sort()
    return changes
