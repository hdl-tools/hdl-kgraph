"""Build pipeline: discover -> pass 0 (preprocess) -> pass 1 (parse) -> pass 2
(link) -> persist.

Keeps the CLI thin and gives later milestones (M6 MCP server) a reusable
entry point.

Pass 0 (M2) runs *serially in compile order* — filelist order, or sorted
discovery order — threading one :class:`MacroTable` through all files the
way simulators carry ``+define+`` and earlier-file defines forward. Pass 1
parses each already-expanded unit independently, so large builds fan parse
work out to a process pool (``--jobs``); results merge back in discovery
order, keeping the linked graph identical to a serial build.

A compilation unit whose content was already spliced into an earlier unit
via ``\\`include`` is skipped (``skipped_reason="included"``) instead of
being parsed a second time without its including context; a header that
appears *before* its includer in compile order still parses standalone,
exactly like a simulator compiling it as its own unit.

M4 — incremental updates: every standalone unit's pass-1 IR (plus its
macro-event log and spliced-header list) is persisted at build time, and
:func:`run_update` re-parses only changed/added/removed files and their
include/macro dependents (:mod:`hdl_kgraph.incremental`). Unchanged units
are re-linked from their stored IR, replaying their macro events into the
shared table at their position in compile order. Each unit gets its own
:class:`PreprocEmitter`, so stored IRs are self-contained — the linker
dedupes the resulting cross-IR repeats by first occurrence. A change to the
effective build inputs (defines, incdirs, filelist sets, library map — the
``options_hash``) falls back to a full rebuild, which also covers "a changed
``.f`` define or incdir dirties all files in that filelist".
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from hdl_kgraph.config import BuildOptions
from hdl_kgraph.discovery import (
    DEFAULT_MAX_FILE_SIZE_KB,
    DiscoveredFile,
    discover,
    discover_from_paths,
    glob_sources,
    source_dirs,
)
from hdl_kgraph.enrich import (
    EnrichmentInput,
    available_backends,
    run_enrichment,
    summarize_enrichment,
)
from hdl_kgraph.enrich.base import Discrepancy
from hdl_kgraph.graph.bounded_link import (
    changed_target_names_bounded,
    link_incremental_bounded,
)
from hdl_kgraph.graph.builder import RefRecord, link_graph, link_incremental
from hdl_kgraph.graph.summary import build_summaries
from hdl_kgraph.ids import file_node_id, library_node_id
from hdl_kgraph.incremental import (
    ChangeSet,
    affected_clean_refs,
    changed_target_names,
    diff_hashes,
    dirty_closure,
    incremental_link_safe,
    newly_resolvable_includes,
)
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.parser.c import CParser, CppParser
from hdl_kgraph.parser.filelist import (
    Filelist,
    filelist_irs,
    flattened_defines,
    flattened_files,
    flattened_incdirs,
    flattened_warnings,
    parse_filelist,
)
from hdl_kgraph.parser.perl import PerlParser
from hdl_kgraph.parser.preprocessor import (
    LineOrigin,
    MacroTable,
    PreprocEmitter,
    PreprocessedFile,
    Preprocessor,
)
from hdl_kgraph.parser.python import PythonParser
from hdl_kgraph.parser.sln import SlnParser
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.tcl import (
    SCRIPT_SUFFIXES,
    UPF_SUFFIXES,
    SdcParser,
    TclScriptParser,
    UpfParser,
)
from hdl_kgraph.parser.vhdl import DEFAULT_LIBRARY, VhdlParser
from hdl_kgraph.schema import Edge, EdgeKind, Language, Node, NodeKind
from hdl_kgraph.storage import ir_codec
from hdl_kgraph.storage.sqlite_store import (
    FileMeta,
    SchemaVersionError,
    SqliteStore,
    StoredUnit,
)

DB_DIRNAME = ".hdl-kgraph"
DB_FILENAME = "graph.db"

#: Auto job count: ``min(os.cpu_count(), this)`` — parsing saturates well
#: before the IPC overhead of more workers pays off.
DEFAULT_JOBS_CAP = 8
#: Auto mode stays serial below this many fresh-parse candidates: spawning
#: a pool costs more than it saves on small builds and incremental updates.
MIN_PARALLEL_FILES = 64
#: ... and below this much total candidate source text: parse time tracks
#: bytes, and for many tiny files per-task IPC outweighs the parallel parse.
MIN_PARALLEL_KB = 2048

#: Stage-progress callback: called with one human-readable line per
#: pipeline stage as it starts.
ProgressFn = Callable[[str], None]

#: Per-file progress callback for the pass 0+1 loop: ``tick(done, total)``.
TickFn = Callable[[int, int], None]


@dataclass
class BuildReport:
    """Summary of one ``build`` run."""

    root: Path
    db_path: Path
    parsed_files: int = 0  # units contributing an IR (freshly parsed + reused)
    reused_files: int = 0  # units re-linked from their stored IR (update only)
    vhdl_files: int = 0
    error_files: int = 0
    parse_error_count: int = 0
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count
    node_count: int = 0
    edge_count: int = 0
    unresolved_count: int = 0
    filelists_read: int = 0
    macros_defined: int = 0
    includes_resolved: int = 0
    includes_unresolved: int = 0
    both_branches: bool = False  # no defines given: ifdef alternatives at 0.6
    preproc_warning_count: int = 0
    warnings: list[str] = field(default_factory=list)  # config + filelist warnings
    # Diagnostics surfaced by `build -v` and persisted for `status --errors`:
    file_errors: dict[str, int] = field(default_factory=dict)  # relpath -> error count
    # relpath -> `file:line: message` details (capped per file in the parser)
    file_error_details: dict[str, list[str]] = field(default_factory=dict)
    preproc_warnings: list[str] = field(default_factory=list)  # full warning text
    incdirs: list[str] = field(default_factory=list)  # explicit `include search path
    # Count of source dirs auto-added to the `include search path (auto_incdirs).
    auto_incdir_count: int = 0
    # M7 semantic enrichment (only populated when `build --enrich` ran).
    enriched: bool = False
    enrich_backends: list[str] = field(default_factory=list)
    edges_upgraded: int = 0
    enrich_nodes_added: int = 0
    enrich_generates_unrolled: int = 0
    discrepancy_count: int = 0
    enrich_diagnostics: list[str] = field(default_factory=list)
    # M4/#64: pass-2 link was incremental (re-resolved only the dirty closure).
    incremental_link: bool = False
    # #119: the incremental link ran memory-bounded (no full prior-graph load).
    # When set, the linked graph is a *partial* delta, so report counts and the
    # whole-design summaries are taken from the DB after the scoped write, not
    # from the in-memory graph.
    bounded_link: bool = False
    # Clean-file ref source ids the incremental link re-resolved (#64). Together
    # with the dirty files this bounds the scoped delta write to the changed rows.
    affected_srcs: set[str] = field(default_factory=set)
    # Reason the incremental linker fell back to a full re-link (None if not).
    incremental_link_skipped: str | None = None
    # Parser-worker failures isolated to their unit (the build continued; #65).
    worker_failures: int = 0
    # Scoping telemetry for an incremental link: refs re-resolved this run vs
    # total pass-2 refs. ``refs_reresolved ≪ refs_total`` is the #64 invariant
    # (a silent re-resolve-everything regression shows up here). 0/0 on a full link.
    refs_reresolved: int = 0
    refs_total: int = 0
    # Per-phase wall-clock (seconds), for capacity planning — chiefly the
    # parallel-build/merge feasibility question (does the parallelizable
    # discover+parse work dominate the serial pass-2 link?). ``parse_s`` fuses
    # pass 0 (preprocess) and pass 1 (parse): the pipeline streams parse tasks
    # to the worker pool as it preprocesses each unit, so the two cannot be
    # timed apart without serializing them. Surfaced by ``build --timings``.
    discover_s: float = 0.0
    parse_s: float = 0.0  # pass 0 (preprocess) + pass 1 (parse), interleaved
    link_s: float = 0.0  # pass 2 (link)
    enrich_s: float = 0.0  # pass 3 (M7 enrichment); 0.0 unless --enrich
    # Phase breakdown *within* enrich_s (see enrich._profile); empty unless
    # --enrich ran. Top-level keys (``slang:enrich``, ``slang:apply``) tile the
    # pass; ``parent/child`` keys (``slang/elaborate_root`` …) detail one.
    enrich_phase_s: dict[str, float] = field(default_factory=dict)
    # Integer tallies from the enrich pass (e.g. ``walk_instances``), to
    # normalize a phase's cost per unit of work. Empty unless --enrich ran.
    enrich_phase_counts: dict[str, int] = field(default_factory=dict)
    persist_s: float = 0.0  # serialize + write the database


@dataclass
class UpdateReport:
    """Summary of one ``update`` run."""

    root: Path
    db_path: Path
    up_to_date: bool = False
    full_rebuild_reason: str | None = None  # incremental path not taken
    reparsed: dict[str, str] = field(default_factory=dict)  # relpath -> why
    removed: list[str] = field(default_factory=list)
    build: BuildReport | None = None  # None only when up_to_date
    elapsed_s: float = 0.0


def default_db_path(root: Path) -> Path:
    return root / DB_DIRNAME / DB_FILENAME


def find_db(start: Path) -> Path | None:
    """Locate the nearest database from *start* upward (git-style)."""
    for directory in [start.resolve(), *start.resolve().parents]:
        candidate = directory / DB_DIRNAME / DB_FILENAME
        if candidate.is_file():
            return candidate
    return None


@dataclass
class _Inputs:
    """Filelist/define/incdir inputs resolved once per run."""

    filelists: list[Filelist] = field(default_factory=list)
    defines: dict[str, str | None] = field(default_factory=dict)
    incdirs: list[Path] = field(default_factory=list)
    list_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    filelists_read: int = 0


def _resolve_inputs(options: BuildOptions, base: Path) -> _Inputs:
    inputs = _Inputs(incdirs=list(options.incdirs))
    confine = None if options.allow_outside_root else base
    inputs.filelists = [parse_filelist(path, root=confine) for path in options.filelists]
    for fl in inputs.filelists:
        inputs.defines.update(flattened_defines(fl))
        inputs.incdirs.extend(flattened_incdirs(fl))
        inputs.list_files.extend(flattened_files(fl))
        inputs.warnings.extend(flattened_warnings(fl))
    inputs.defines.update(options.defines)  # config/CLI defines override filelist ones
    seen: set[Path] = set()
    inputs.filelists_read = sum(len(_walk_filelists(fl, seen)) for fl in inputs.filelists)
    return inputs


def _discover(
    root: Path, base: Path, options: BuildOptions, inputs: _Inputs, max_kb: int
) -> list[DiscoveredFile]:
    if inputs.filelists or options.sources:
        paths = list(inputs.list_files)
        for pattern in options.sources:
            paths.extend(glob_sources(base, pattern))
        return discover_from_paths(paths, base, exclude=options.exclude, max_file_size_kb=max_kb)
    return discover(root, exclude=options.exclude, max_file_size_kb=max_kb)


def options_hash(base: Path, options: BuildOptions, inputs: _Inputs) -> str:
    """Fingerprint of the effective build inputs.

    A mismatch invalidates incremental updates: defines and incdirs feed the
    preprocessor globally (filelist ``+define+``/``+incdir+`` included), and
    sources/exclude/size/library settings change the file set or routing.
    Pure filelist *membership* changes are not part of the hash — they show
    up as added/removed files in the ordinary hash diff.
    """

    def rel(path: Path) -> str:
        return Path(os.path.relpath(Path(path).resolve(), base)).as_posix()

    payload = {
        "defines": sorted((k, v) for k, v in inputs.defines.items()),
        "incdirs": [rel(d) for d in inputs.incdirs],
        # Toggling auto-incdir changes which `include``s resolve, so an
        # incremental reuse of a build made with the other setting is invalid.
        # The derived dir list is a function of the (already-hashed) file set,
        # so only the flag itself needs fingerprinting.
        "auto_incdirs": options.auto_incdirs,
        # Lifting root containment changes which filelist sources/includes
        # resolve, so an incremental reuse across a flag flip is invalid.
        "allow_outside_root": options.allow_outside_root,
        "sources": sorted(options.sources),
        "exclude": sorted(options.exclude),
        "max_file_size_kb": options.max_file_size_kb,
        # cocotb DUT resolution keys off the configured tops, so a change to
        # them must invalidate an incremental reuse of the prior build.
        "top": sorted(options.top),
        "vhdl_libraries": sorted((name, rel(p)) for name, p in options.vhdl_libraries.items()),
        "filelists": [rel(p) for p in options.filelists],
        # Enrichment rewrites the graph, so toggling it (or its backend set)
        # must invalidate an incremental reuse of a non-enriched build.
        "enrich": options.enrich,
        "enrich_backends": sorted(options.enrich_backends),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def run_build(
    root: Path,
    db_path: Path | None = None,
    options: BuildOptions | None = None,
    progress: ProgressFn | None = None,
    tick: TickFn | None = None,
) -> BuildReport:
    return _execute(
        root,
        db_path,
        options if options is not None else BuildOptions(),
        progress=progress,
        tick=tick,
    )


# Pass-1 parse tasks run in pool workers. Tree-sitter parser objects are not
# picklable, so each worker process builds its own lazily; everything that
# crosses the process boundary (str, LineOrigin, FileIR) is a plain dataclass.
_WORKER_SV_PARSER: SystemVerilogParser | None = None
_WORKER_VHDL_PARSER: VhdlParser | None = None
_WORKER_C_PARSER: CParser | None = None
_WORKER_CPP_PARSER: CppParser | None = None
_WORKER_PYTHON_PARSER: PythonParser | None = None
_WORKER_SDC_PARSER: SdcParser | None = None
_WORKER_UPF_PARSER: UpfParser | None = None
_WORKER_TCL_SCRIPT_PARSER: TclScriptParser | None = None
_WORKER_PERL_PARSER: PerlParser | None = None
_WORKER_SLN_PARSER: SlnParser | None = None


def _parse_sv_task(relpath: str, text: str, line_map: list[LineOrigin]) -> FileIR:
    """Parse one already-expanded SV/Verilog unit (pool worker entry point)."""
    global _WORKER_SV_PARSER
    if _WORKER_SV_PARSER is None:
        _WORKER_SV_PARSER = SystemVerilogParser()
    return _WORKER_SV_PARSER.parse(Path(relpath), text, line_map=line_map)


def _parse_vhdl_task(relpath: str, text: str, library: str) -> FileIR:
    """Parse one VHDL unit (pool worker entry point)."""
    global _WORKER_VHDL_PARSER
    if _WORKER_VHDL_PARSER is None:
        _WORKER_VHDL_PARSER = VhdlParser()
    return _WORKER_VHDL_PARSER.parse(Path(relpath), text, library=library)


def _parse_c_task(relpath: str, text: str) -> FileIR:
    """Parse one C unit for DPI-C linking (pool worker entry point)."""
    global _WORKER_C_PARSER
    if _WORKER_C_PARSER is None:
        _WORKER_C_PARSER = CParser()
    return _WORKER_C_PARSER.parse(Path(relpath), text)


def _parse_cpp_task(relpath: str, text: str) -> FileIR:
    """Parse one C++ unit for DPI-C linking (pool worker entry point)."""
    global _WORKER_CPP_PARSER
    if _WORKER_CPP_PARSER is None:
        _WORKER_CPP_PARSER = CppParser()
    return _WORKER_CPP_PARSER.parse(Path(relpath), text)


def _parse_python_task(relpath: str, text: str, tops: list[str]) -> FileIR:
    """Parse one cocotb testbench (pool worker entry point). *tops* are the
    configured top modules used to resolve the DUT (else a filename heuristic)."""
    global _WORKER_PYTHON_PARSER
    if _WORKER_PYTHON_PARSER is None:
        _WORKER_PYTHON_PARSER = PythonParser()
    return _WORKER_PYTHON_PARSER.parse(Path(relpath), text, tops=tops)


def _parse_sdc_task(relpath: str, text: str) -> FileIR:
    """Parse one SDC/XDC constraint file (M10 — pool worker entry point)."""
    global _WORKER_SDC_PARSER
    if _WORKER_SDC_PARSER is None:
        _WORKER_SDC_PARSER = SdcParser()
    return _WORKER_SDC_PARSER.parse(Path(relpath), text)


def _parse_upf_task(relpath: str, text: str) -> FileIR:
    """Parse one UPF power-intent file (M10 — pool worker entry point)."""
    global _WORKER_UPF_PARSER
    if _WORKER_UPF_PARSER is None:
        _WORKER_UPF_PARSER = UpfParser()
    return _WORKER_UPF_PARSER.parse(Path(relpath), text)


def _parse_tcl_script_task(relpath: str, text: str) -> FileIR:
    """Parse one Tcl flow script (M10 — pool worker entry point)."""
    global _WORKER_TCL_SCRIPT_PARSER
    if _WORKER_TCL_SCRIPT_PARSER is None:
        _WORKER_TCL_SCRIPT_PARSER = TclScriptParser()
    return _WORKER_TCL_SCRIPT_PARSER.parse(Path(relpath), text)


def _parse_tcl_task(relpath: str, text: str) -> FileIR:
    """Route a TCL-family unit to its parser by suffix.

    ``.upf`` → UPF power intent, ``.tcl`` → flow script, else (``.sdc``/``.xdc``)
    → SDC/XDC timing constraints.
    """
    suffix = Path(relpath).suffix
    if suffix in UPF_SUFFIXES:
        return _parse_upf_task(relpath, text)
    if suffix in SCRIPT_SUFFIXES:
        return _parse_tcl_script_task(relpath, text)
    return _parse_sdc_task(relpath, text)


def _parse_perl_task(relpath: str, text: str) -> FileIR:
    """Parse one Perl codegen script (M10 — pool worker entry point)."""
    global _WORKER_PERL_PARSER
    if _WORKER_PERL_PARSER is None:
        _WORKER_PERL_PARSER = PerlParser()
    return _WORKER_PERL_PARSER.parse(Path(relpath), text)


def _parse_sln_task(relpath: str, text: str) -> FileIR:
    """Parse one Cadence Perspec SLN file (M10 — pool worker entry point)."""
    global _WORKER_SLN_PARSER
    if _WORKER_SLN_PARSER is None:
        _WORKER_SLN_PARSER = SlnParser()
    return _WORKER_SLN_PARSER.parse(Path(relpath), text)


def _effective_jobs(options: BuildOptions, candidates: int, candidate_bytes: int) -> int:
    """Parse workers for this run; explicit ``--jobs`` wins over the auto heuristic."""
    if options.jobs is not None:
        return max(1, options.jobs)
    if candidates < MIN_PARALLEL_FILES or candidate_bytes < MIN_PARALLEL_KB * 1024:
        return 1
    return min(os.cpu_count() or 1, DEFAULT_JOBS_CAP)


@dataclass
class _PendingUnit:
    """One non-skipped unit awaiting finalization, kept in discovery order."""

    found: DiscoveredFile
    ir: FileIR | None = None  # ready: reused unit's decoded IR
    thunk: Callable[[], FileIR] | None = None  # serial parse, deferred to result()
    future: Future[FileIR] | None = None  # in-flight pool parse
    pp: PreprocessedFile | None = None  # SV fresh parse: PreprocEmitter input
    reused: bool = False

    def ready(self) -> bool:
        return self.future is None or self.future.done()

    def result(self) -> FileIR:
        """Materialize the IR. Deferring the serial parse here routes both the
        serial and pool paths through ``finalize``'s worker-failure guard (#65)."""
        if self.ir is not None:
            return self.ir
        if self.thunk is not None:
            return self.thunk()
        return self.future.result()  # type: ignore[union-attr]


def _link_pass2(
    irs: list[FileIR],
    db_path: Path,
    options: BuildOptions,
    discovered: list[DiscoveredFile] | None,
    incremental: bool,
    dirty_files: set[str] | None,
    report: BuildReport,
    root: Path | None = None,
) -> tuple[nx.MultiDiGraph, list[RefRecord]]:
    """Pass-2 link, incrementally when safe (#64).

    An incremental ``update`` re-resolves only the dirty closure plus its
    resolution neighborhood and reuses the prior graph's edges for the rest;
    anything the SV MVP doesn't model (VHDL, binds/config, enrichment) falls
    back to a full ``link_graph`` over the same (parse-incremental) IRs. *root*
    is threaded to the linker for in-tree absolute file-ref canonicalization (#164).
    """
    if not incremental or dirty_files is None:
        return link_graph(irs, warnings=report.warnings, root=root)
    has_vhdl = any(f.language is Language.VHDL for f in (discovered or []))
    has_binds = any(ref.edge_kind is EdgeKind.BINDS for ir in irs for ref in ir.unresolved_refs)
    # Only *parsed* cocotb units force a full re-link — a non-cocotb `.py` is
    # skipped (``not_cocotb``) and contributes no refs, so it must not count.
    has_cocotb = any(
        f.language is Language.PYTHON and f.skipped_reason is None for f in (discovered or [])
    )
    # SDC/UPF/Tcl-flow (TCL) and Perl scripts emit cross-file REFERENCES_FILE/
    # GENERATED_FROM refs (and SDC upgrades CLOCKED_BY design-wide), so any of
    # them forces a full re-link — like cocotb/VHDL.
    has_sdc = any(
        f.language in (Language.TCL, Language.PERL, Language.SLN) and f.skipped_reason is None
        for f in (discovered or [])
    )
    reason = incremental_link_safe(options.enrich, has_vhdl, has_binds, has_cocotb, has_sdc)
    if reason is None:
        store = SqliteStore(db_path)
        prior_ref_index = store.load_ref_index()
        if any(r.edge_kind is EdgeKind.BINDS for r in prior_ref_index):
            reason = "bind/configuration directives not supported yet"
        elif options.bounded_link:
            # #119: re-resolve the dirty closure straight from SQLite, never
            # loading the whole prior graph. changed_target_names is computed
            # from the DB (not a materialised graph) to keep the path bounded.
            changed = changed_target_names_bounded(db_path, irs, dirty_files)
            affected = {src for _, src, _ in affected_clean_refs(prior_ref_index, changed)}
            report.incremental_link = True
            report.bounded_link = True
            report.affected_srcs = affected
            graph, ref_records = link_incremental_bounded(
                db_path, irs, dirty_files, affected, warnings=report.warnings, root=root
            )
            report.refs_total = len(ref_records)
            report.refs_reresolved = sum(
                1 for r in ref_records if r.file in dirty_files or r.src_id in affected
            )
            return graph, ref_records
        else:
            prior_graph, _, _ = store.load()
            changed = changed_target_names(prior_graph, irs, dirty_files)
            affected = {src for _, src, _ in affected_clean_refs(prior_ref_index, changed)}
            report.incremental_link = True
            report.affected_srcs = affected
            graph, ref_records = link_incremental(
                irs, prior_graph, dirty_files, affected, warnings=report.warnings, root=root
            )
            # Telemetry: refs re-resolved this run vs total (the #64 scoping
            # invariant). Mirrors link_incremental's own live-ref condition.
            report.refs_total = len(ref_records)
            report.refs_reresolved = sum(
                1 for r in ref_records if r.file in dirty_files or r.src_id in affected
            )
            return graph, ref_records
    report.incremental_link_skipped = reason
    return link_graph(irs, warnings=report.warnings, root=root)


class _SelectiveLinkUnavailable(Exception):
    """The bounded selective-decode path cannot link this update (bind/config
    directives force a full re-link, which needs every unit's IR). ``run_update``
    catches it and retries with the legacy full-decode path."""


def _replay_macros_only(
    relpath: str,
    lite: tuple[str, str],
    preprocessor: Preprocessor,
    processed: set[str],
    consumed: set[str],
) -> None:
    """Replay a clean SV unit's macro events into the shared table — the only
    thing a clean, non-affected unit contributes to a bounded re-link (#119), so
    its large IR blob is never decoded. Mirrors the macro half of ``_reuse_unit``.

    A corrupt stored row raises :class:`_SelectiveLinkUnavailable` so ``run_update``
    retries on the full-decode path, where ``_reuse_unit`` re-parses it fresh (the
    selective path cannot re-parse a clean unit, having decoded only its macros)."""
    try:
        events = ir_codec.macro_events_from_json(lite[0])
        included = set(json.loads(lite[1]))
    except (KeyError, TypeError, ValueError) as exc:
        raise _SelectiveLinkUnavailable from exc
    for event in events:
        preprocessor.macros.apply(event)
    processed.add(relpath)
    consumed |= included - processed


def _selective_link(
    db_path: Path,
    irs: list[FileIR],
    dirty_files: set[str],
    discovered: list[DiscoveredFile],
    clean_set: set[str],
    report: BuildReport,
    root: Path | None = None,
) -> tuple[nx.MultiDiGraph, list[RefRecord]]:
    """Bounded re-link for the selective-decode path: decode only the *affected*
    clean units' IRs (the rest were macro-replayed only), then re-resolve the
    dirty closure straight from SQLite. Raises :class:`_SelectiveLinkUnavailable`
    when bind/configuration directives force a full re-link (which would need
    every unit's IR)."""
    store = SqliteStore(db_path)
    prior_ref_index = store.load_ref_index()
    if any(r.edge_kind is EdgeKind.BINDS for r in prior_ref_index) or any(
        ref.edge_kind is EdgeKind.BINDS for ir in irs for ref in ir.unresolved_refs
    ):
        raise _SelectiveLinkUnavailable

    changed = changed_target_names_bounded(db_path, irs, dirty_files)
    affected_keys = affected_clean_refs(prior_ref_index, changed)
    affected = {src for _, src, _ in affected_keys}
    affected_files = {file for file, _, _ in affected_keys} & clean_set

    # Decode just the affected clean units (the rest stay undecoded), then merge
    # them back with the dirty IRs in *discovery order*. The bounded linker uses
    # first occurrence across `file_irs` for node ownership / definition dedup, so
    # a clean affected unit that precedes a dirty one must keep that relative order
    # to stay byte-identical to the full-decode path; filelist/synthetic IRs (added
    # to `irs` before this call) stay at the tail as they do there.
    if affected_files:
        units = store.load_units_for(affected_files)
        order = {d.relpath: i for i, d in enumerate(discovered)}
        affected_irs: list[FileIR] = []
        for relpath in affected_files:
            stored = units.get(relpath)
            if stored is None:
                # An affected clean row vanished/missed the chunked fetch: bail to
                # the full-decode path, which re-parses it rather than dropping a
                # unit that still needs re-resolution.
                raise _SelectiveLinkUnavailable
            try:
                affected_irs.append(ir_codec.ir_from_json(stored.ir))
            except (KeyError, TypeError, ValueError) as exc:
                raise _SelectiveLinkUnavailable from exc
        unit_irs = [ir for ir in irs if ir.path in dirty_files] + affected_irs
        tail_irs = [ir for ir in irs if ir.path not in dirty_files]
        unit_irs.sort(key=lambda ir: order.get(ir.path, len(discovered)))
        irs = unit_irs + tail_irs

    report.incremental_link = True
    report.bounded_link = True
    report.affected_srcs = affected
    graph, ref_records = link_incremental_bounded(
        db_path, irs, dirty_files, affected, warnings=report.warnings, root=root
    )
    # Telemetry (the #64 scoping invariant). `ref_records` only covers the decoded
    # subset (dirty + affected), so the whole-design total comes from the prior
    # ref index (clean refs survive the scoped write) plus the fresh dirty refs.
    clean_total = sum(1 for r in prior_ref_index if r.file not in dirty_files)
    dirty_total = sum(len(ir.unresolved_refs) for ir in irs if ir.path in dirty_files)
    report.refs_total = clean_total + dirty_total
    report.refs_reresolved = sum(
        1 for r in ref_records if r.file in dirty_files or r.src_id in affected
    )
    return graph, ref_records


def _refresh_test_covers_from_db(store: SqliteStore) -> None:
    """After a bounded-link scoped write, re-derive the whole TEST_COVERS edge set
    out-of-core (the partial graph can't run the whole-graph derivation) and
    reconcile it into the DB — byte-identically to a full ``build``."""
    from hdl_kgraph.storage.summaries import test_covers_sql

    with store._connect() as conn:
        store._check_version(conn)
        edges = test_covers_sql(conn)
    store.replace_test_covers(edges)


def _refresh_summaries_and_counts_from_db(store: SqliteStore, report: BuildReport) -> None:
    """After a bounded-link scoped write, recompute the whole-design summaries
    out-of-core (M12.5 SQL scans) from the now-current DB and read back the graph
    counts — the bounded path never had the whole graph in memory to do either."""
    from hdl_kgraph.storage.summaries import (
        clock_summary_sql,
        power_summary_sql,
        uvm_summary_sql,
    )

    with store._connect() as conn:
        store._check_version(conn)
        summaries = {
            "clock_domains": json.dumps(clock_summary_sql(conn)),
            "power_domains": json.dumps(power_summary_sql(conn)),
            "uvm_topology": json.dumps(uvm_summary_sql(conn)),
        }
    store.save_summaries(summaries)
    report.node_count, report.edge_count, report.unresolved_count = store.graph_counts()


def _execute(
    root: Path,
    db_path: Path | None,
    options: BuildOptions,
    inputs: _Inputs | None = None,
    reuse: dict[str, StoredUnit] | None = None,
    discovered: list[DiscoveredFile] | None = None,
    prior_warnings: dict[str, list[str]] | None = None,
    progress: ProgressFn | None = None,
    tick: TickFn | None = None,
    incremental: bool = False,
    dirty_files: set[str] | None = None,
    macro_lite: dict[str, tuple[str, str]] | None = None,
    prior_errors: dict[str, tuple[int, list[str]]] | None = None,
) -> BuildReport:
    """One pipeline run; units named in *reuse* re-link from their stored IR.

    *prior_warnings* carries the previous build's per-file preprocessor
    warnings for reused units (their preprocessor never re-runs).

    When *incremental* is set (an ``update`` over an existing, schema-matched
    database), persistence writes only the changed rows via
    :meth:`SqliteStore.save_incremental`; otherwise the whole graph is
    rewritten via :meth:`SqliteStore.save`.
    """
    progress = progress if progress is not None else lambda _line: None
    tick = tick if tick is not None else lambda _done, _total: None
    root = root.resolve()
    base = root.parent if root.is_file() else root
    db_path = db_path if db_path is not None else default_db_path(base)
    report = BuildReport(root=root, db_path=db_path)
    report.warnings.extend(options.warnings)
    max_kb = (
        options.max_file_size_kb
        if options.max_file_size_kb is not None
        else DEFAULT_MAX_FILE_SIZE_KB
    )
    reuse = reuse or {}
    # Selective decode (#119): clean units are macro-replayed only (no IR decode);
    # the bounded re-link decodes just the affected ones. `clean_set` is the set of
    # unchanged units either way.
    selective = macro_lite is not None
    clean_set = set(macro_lite) if macro_lite is not None else set(reuse)

    # -- inputs: filelists, defines, include dirs -----------------------------
    if inputs is None:
        progress("resolving build inputs (filelists, defines, incdirs)")
        inputs = _resolve_inputs(options, base)
    report.warnings.extend(inputs.warnings)
    report.filelists_read = inputs.filelists_read
    report.incdirs = [str(d) for d in inputs.incdirs]

    # -- file set --------------------------------------------------------------
    if discovered is None:
        progress(f"discovering source files under {root}")
        _t0 = time.perf_counter()
        discovered = _discover(root, base, options, inputs, max_kb)
        report.discover_s = time.perf_counter() - _t0

    # Auto-discovered `include search dirs: every source directory in the tree,
    # so a header/define file resolves without an explicit ``-I``. Explicit
    # incdirs still win (searched first); see Preprocessor._resolve_include.
    auto_incdirs = source_dirs(discovered, base) if options.auto_incdirs else []
    report.auto_incdir_count = len(auto_incdirs)

    # -- pass 0 + pass 1, in compile order --------------------------------------
    report.both_branches = not inputs.defines
    preprocessor = Preprocessor(
        base=base,
        incdirs=inputs.incdirs,
        auto_incdirs=auto_incdirs,
        macros=MacroTable(inputs.defines),
        branch_mode="both" if report.both_branches else "select",
    )
    irs: list[FileIR] = []
    units: dict[str, StoredUnit] = {}
    files_meta: list[FileMeta] = []
    macro_keys: set[tuple[str, str, int]] = set()
    processed: set[str] = set()  # units preprocessed standalone so far
    consumed: set[str] = set()  # spliced into an earlier unit -> skip standalone
    vhdl_file_libs: dict[str, str] = {}  # relpath -> library name

    def finalize(entry: _PendingUnit) -> None:
        """Per-unit accounting, called strictly in discovery order."""
        found = entry.found
        try:
            ir = entry.result()
        except Exception as exc:  # noqa: BLE001 — isolate any worker failure
            # A parser-worker crash (e.g. a BrokenProcessPool from a segfaulting
            # or OOM-killed child) or an unexpected task error is isolated to its
            # unit: record it and continue so the rest of the build still produces
            # a partial graph rather than aborting wholesale (#65).
            detail = f"{type(exc).__name__}: {exc}"
            report.worker_failures += 1
            report.error_files += 1
            report.parse_error_count += 1
            report.file_errors[found.relpath] = 1
            report.file_error_details[found.relpath] = [f"parser worker failed: {detail}"]
            report.warnings.append(
                f"parser worker failed on {found.relpath} ({detail}); unit skipped"
            )
            files_meta.append(
                FileMeta(
                    path=found.relpath,
                    language=found.language,
                    content_hash=found.content_hash,
                    size_bytes=found.size_bytes,
                    parse_error_count=1,
                    parse_errors=[f"parser worker failed: {detail}"],
                )
            )
            return
        file_warnings: list[str] = []
        if entry.reused:
            report.reused_files += 1
            # The preprocessor does not re-run for reused units; carry their
            # previous build's warnings forward.
            file_warnings = list((prior_warnings or {}).get(found.relpath, []))
            if found.language is Language.VHDL:
                report.vhdl_files += 1
            units[found.relpath] = reuse[found.relpath]
        elif found.language is Language.VHDL:
            report.vhdl_files += 1
            units[found.relpath] = StoredUnit(
                ir=ir_codec.ir_to_json(ir), macro_events="[]", included="[]"
            )
        elif found.language in (
            Language.C,
            Language.CPP,
            Language.PYTHON,
            Language.TCL,
            Language.PERL,
            Language.SLN,
        ):
            # C/C++ (DPI-C), Python (cocotb), and SDC/XDC (M10) have no
            # preprocessor pass; store the IR directly, like VHDL.
            units[found.relpath] = StoredUnit(
                ir=ir_codec.ir_to_json(ir), macro_events="[]", included="[]"
            )
        else:
            pp = entry.pp
            assert pp is not None
            # One emitter per unit keeps each stored IR self-contained; the
            # linker dedupes repeats across units by first occurrence.
            PreprocEmitter().emit(pp, ir)
            report.includes_resolved += sum(1 for ev in pp.includes if ev.resolved is not None)
            report.includes_unresolved += sum(1 for ev in pp.includes if ev.resolved is None)
            file_warnings = list(pp.warnings)
            macro_keys.update((d.file, d.name, d.line) for d in pp.macro_defs)
            units[found.relpath] = StoredUnit(
                ir=ir_codec.ir_to_json(ir),
                macro_events=ir_codec.macro_events_to_json(pp.macro_events),
                included=json.dumps(sorted(pp.included_relpaths)),
            )
        irs.append(ir)
        report.parsed_files += 1
        if ir.parse_error_count:
            report.error_files += 1
            report.parse_error_count += ir.parse_error_count
            report.file_errors[found.relpath] = ir.parse_error_count
            report.file_error_details[found.relpath] = list(ir.parse_errors)
        report.preproc_warnings.extend(file_warnings)
        report.preproc_warning_count += len(file_warnings)
        files_meta.append(
            FileMeta(
                path=found.relpath,
                language=found.language,
                content_hash=found.content_hash,
                size_bytes=found.size_bytes,
                parse_error_count=ir.parse_error_count,
                warnings=file_warnings,
                parse_errors=list(ir.parse_errors),
            )
        )

    fresh = [f for f in discovered if f.skipped_reason is None and f.relpath not in clean_set]
    jobs = _effective_jobs(options, len(fresh), sum(f.size_bytes for f in fresh))
    progress(
        f"pass 0+1: preprocessing and parsing {len(discovered)} file(s)"
        + (f" ({jobs} parse workers)" if jobs > 1 else "")
    )
    # Pass 0 streams parse tasks to the pool as it expands each unit; results
    # are drained in discovery order (the linker dedupes by first occurrence,
    # so `irs` order must match a serial build) which also bounds how many
    # expanded texts stay in memory at once.
    pending: deque[_PendingUnit] = deque()
    _t_parse = time.perf_counter()
    with contextlib.ExitStack() as stack:
        executor = stack.enter_context(ProcessPoolExecutor(max_workers=jobs)) if jobs > 1 else None
        for index, found in enumerate(discovered, start=1):
            # Skipped/reused files advance the counter too: it always reaches total.
            tick(index, len(discovered))
            skipped_reason = found.skipped_reason
            if skipped_reason is None and found.relpath in consumed:
                skipped_reason = "included"
            if skipped_reason is not None:
                report.skipped[skipped_reason] = report.skipped.get(skipped_reason, 0) + 1
                files_meta.append(
                    FileMeta(
                        path=found.relpath,
                        language=found.language,
                        content_hash=found.content_hash,
                        size_bytes=found.size_bytes,
                        skipped_reason=skipped_reason,
                    )
                )
                continue
            if macro_lite is not None and found.relpath in macro_lite:
                # Clean unit on the bounded path: replay its macros into the shared
                # table (compile-order prerequisite for dirty re-parses) but do NOT
                # decode its IR. It is counted as reused; its files/file_irs rows are
                # preserved by the scoped write. The affected ones are decoded later.
                _replay_macros_only(
                    found.relpath, macro_lite[found.relpath], preprocessor, processed, consumed
                )
                report.reused_files += 1
                report.parsed_files += 1
                # The preprocessor does not re-run for clean units; carry their
                # previous build's warnings forward (their files rows are preserved
                # by the scoped write, so this keeps the report consistent with it).
                clean_warnings = list((prior_warnings or {}).get(found.relpath, []))
                report.preproc_warnings.extend(clean_warnings)
                report.preproc_warning_count += len(clean_warnings)
                # Likewise carry forward the stored parse-error telemetry (it lives
                # in the un-decoded IR / preserved files row, not re-derived here).
                err_count, err_details = (prior_errors or {}).get(found.relpath, (0, []))
                if err_count:
                    report.error_files += 1
                    report.parse_error_count += err_count
                    report.file_errors[found.relpath] = err_count
                    report.file_error_details[found.relpath] = list(err_details)
                continue
            ir = None if selective else _reuse_unit(found, reuse, preprocessor, processed, consumed)
            if ir is not None:
                if found.language is Language.VHDL:
                    vhdl_file_libs[found.relpath] = _library_for(found.path, options.vhdl_libraries)
                pending.append(_PendingUnit(found=found, ir=ir, reused=True))
            elif found.language is Language.VHDL:
                # VHDL has no SV preprocessor pass; route by configured library.
                library = _library_for(found.path, options.vhdl_libraries)
                vhdl_file_libs[found.relpath] = library
                text = found.path.read_text(errors="replace")
                if executor is None:
                    pending.append(
                        _PendingUnit(
                            found=found,
                            thunk=functools.partial(_parse_vhdl_task, found.relpath, text, library),
                        )
                    )
                else:
                    pending.append(
                        _PendingUnit(
                            found=found,
                            future=executor.submit(_parse_vhdl_task, found.relpath, text, library),
                        )
                    )
            elif found.language in (Language.C, Language.CPP):
                # C/C++ have no SV preprocessor pass (M8 DPI-C boundary): route
                # the raw text to the C-family parser, like VHDL.
                task = _parse_cpp_task if found.language is Language.CPP else _parse_c_task
                text = found.path.read_text(errors="replace")
                if executor is None:
                    pending.append(
                        _PendingUnit(
                            found=found, thunk=functools.partial(task, found.relpath, text)
                        )
                    )
                else:
                    pending.append(
                        _PendingUnit(found=found, future=executor.submit(task, found.relpath, text))
                    )
            elif found.language is Language.PYTHON:
                # cocotb testbenches (M8): no preprocessor; the configured top
                # modules resolve the DUT (else the parser's filename heuristic).
                tops = list(options.top)
                text = found.path.read_text(errors="replace")
                if executor is None:
                    pending.append(
                        _PendingUnit(
                            found=found,
                            thunk=functools.partial(_parse_python_task, found.relpath, text, tops),
                        )
                    )
                else:
                    pending.append(
                        _PendingUnit(
                            found=found,
                            future=executor.submit(_parse_python_task, found.relpath, text, tops),
                        )
                    )
            elif found.language is Language.TCL:
                # SDC/XDC constraints, UPF power intent, and Tcl flow scripts
                # (M10): no preprocessor; the raw text is routed by suffix
                # (``.upf`` → UPF, ``.tcl`` → flow script, else SDC), like C/VHDL.
                text = found.path.read_text(errors="replace")
                if executor is None:
                    pending.append(
                        _PendingUnit(
                            found=found,
                            thunk=functools.partial(_parse_tcl_task, found.relpath, text),
                        )
                    )
                else:
                    pending.append(
                        _PendingUnit(
                            found=found,
                            future=executor.submit(_parse_tcl_task, found.relpath, text),
                        )
                    )
            elif found.language is Language.PERL:
                # Perl codegen scripts (M10): no preprocessor; the raw text goes
                # to the regex-scan PerlParser, like C/VHDL/TCL.
                text = found.path.read_text(errors="replace")
                if executor is None:
                    pending.append(
                        _PendingUnit(
                            found=found,
                            thunk=functools.partial(_parse_perl_task, found.relpath, text),
                        )
                    )
                else:
                    pending.append(
                        _PendingUnit(
                            found=found,
                            future=executor.submit(_parse_perl_task, found.relpath, text),
                        )
                    )
            elif found.language is Language.SLN:
                # Cadence Perspec SLN (M10): no preprocessor; the raw text goes
                # to the regex-scan SlnParser, like Perl/TCL.
                text = found.path.read_text(errors="replace")
                if executor is None:
                    pending.append(
                        _PendingUnit(
                            found=found,
                            thunk=functools.partial(_parse_sln_task, found.relpath, text),
                        )
                    )
                else:
                    pending.append(
                        _PendingUnit(
                            found=found,
                            future=executor.submit(_parse_sln_task, found.relpath, text),
                        )
                    )
            elif found.language not in (Language.SYSTEMVERILOG, Language.VERILOG):
                # A suffix that reached discovery but has no pass-1 dispatch
                # branch (a not-yet-implemented backend wired into SUFFIXES
                # without a handler): skip-with-warning rather than silently
                # mis-parsing it as SystemVerilog. See issue #77.
                report.skipped["unsupported"] = report.skipped.get("unsupported", 0) + 1
                report.warnings.append(
                    f"no pass-1 parser for {found.relpath} ({found.language.value}); skipped"
                )
                files_meta.append(
                    FileMeta(
                        path=found.relpath,
                        language=found.language,
                        content_hash=found.content_hash,
                        size_bytes=found.size_bytes,
                        skipped_reason="unsupported",
                    )
                )
                continue
            else:
                pp = preprocessor.preprocess(found.path)
                processed.add(found.relpath)
                consumed |= pp.included_relpaths - processed
                if executor is None:
                    pending.append(
                        _PendingUnit(
                            found=found,
                            thunk=functools.partial(
                                _parse_sv_task, found.relpath, pp.text, pp.line_map
                            ),
                            pp=pp,
                        )
                    )
                else:
                    pending.append(
                        _PendingUnit(
                            found=found,
                            future=executor.submit(
                                _parse_sv_task, found.relpath, pp.text, pp.line_map
                            ),
                            pp=pp,
                        )
                    )
            while pending and pending[0].ready():
                finalize(pending.popleft())
        if pending:
            progress(f"pass 1: waiting for {len(pending)} parse result(s)")
            while pending:
                finalize(pending.popleft())
    report.parse_s = time.perf_counter() - _t_parse
    report.macros_defined = len(macro_keys)

    # Nothing parseable: the CLI treats this as an error, so leave any
    # previously built database untouched instead of overwriting it with an
    # empty graph.
    if report.parsed_files == 0:
        return report

    # FILELIST nodes/edges last, so parser-emitted FILE nodes win the
    # linker's first-occurrence dedupe over the filelist's minimal stubs.
    seen_meta: set[Path] = set()
    for fl in inputs.filelists:
        irs.extend(filelist_irs(fl, base))
        files_meta.extend(_filelist_meta(fl, base, seen_meta))
    if vhdl_file_libs:
        irs.append(_library_ir(vhdl_file_libs, options.vhdl_libraries))

    progress(f"pass 2: linking {len(irs)} unit(s) into the graph")
    _t_link = time.perf_counter()
    if selective:
        assert db_path is not None and dirty_files is not None  # update path only
        graph, ref_records = _selective_link(
            db_path, irs, dirty_files, discovered, clean_set, report, root=base
        )
    else:
        graph, ref_records = _link_pass2(
            irs, db_path, options, discovered, incremental, dirty_files, report, root=base
        )
    report.link_s = time.perf_counter() - _t_link
    if not report.bounded_link:
        # The bounded link returns a *partial* graph (delta only), so its
        # number_of_nodes/edges would be wrong — those counts are read from the
        # DB after the scoped write below instead.
        report.node_count = graph.number_of_nodes()
        report.edge_count = graph.number_of_edges()
        report.unresolved_count = sum(
            1 for _, data in graph.nodes(data=True) if data["attrs"].get("unresolved")
        )

    # -- pass 3 (M7): opt-in native-frontend enrichment ------------------------
    discrepancies: list[Discrepancy] = []
    if options.enrich:
        # The compilation-unit set, not the raw discovery: a header spliced
        # into an earlier unit (``consumed``) must not also be handed to the
        # native frontend as a standalone top, or it double-defines its
        # contents and breaks elaboration.
        enrich_files = [
            f.path
            for f in discovered
            if f.skipped_reason is None
            and f.relpath not in consumed
            and f.language in (Language.VERILOG, Language.SYSTEMVERILOG, Language.VHDL)
        ]
        _t_enrich = time.perf_counter()
        discrepancies = _enrich(
            graph, base, options, inputs, enrich_files, vhdl_file_libs, report, progress
        )
        report.enrich_s = time.perf_counter() - _t_enrich
        report.node_count = graph.number_of_nodes()
        report.edge_count = graph.number_of_edges()

    progress(f"writing {db_path}")
    _t_persist = time.perf_counter()
    store = SqliteStore(db_path)
    # Whole-design summaries (clock domains, UVM topology) cannot be answered
    # from a bounded subgraph, so compute them once here — the graph is already
    # in memory — and persist them for the MCP server to read without a load.
    # The bounded-link path has only a partial graph in memory, so it defers the
    # summaries: they are recomputed from the written DB via the SQL-native scans
    # below (summaries=None tells save_incremental to leave the table untouched).
    summaries: dict[str, str] | None
    if report.bounded_link:
        summaries = None
    else:
        summaries = {name: json.dumps(p) for name, p in build_summaries(graph).items()}
    opts_hash = options_hash(base, options, inputs)
    if incremental:
        # Scope the delta write to the dirty closure only when the link was
        # incremental — a full re-link fallback can touch any clean edge, so
        # leaving the scope sets None makes save_incremental diff the whole graph.
        scoped = report.incremental_link and dirty_files is not None
        store.save_incremental(
            graph,
            files_meta,
            root=base,
            units=units,
            options_hash=opts_hash,
            discrepancies=discrepancies,
            ref_records=ref_records,
            summaries=summaries,
            touched_files=dirty_files if scoped else None,
            affected_srcs=report.affected_srcs if scoped else None,
        )
        if scoped:
            # TEST_COVERS edges are cross-file — a clean tb-top / uvm_test src
            # covers DUTs anywhere in the design — so the src-scoped delta write
            # cannot keep them consistent (the bounded path never derives them; the
            # in-memory path derives them in-graph but the scoped write drops the
            # ones whose src is outside the dirty closure). Re-derive the whole set
            # out-of-core and reconcile, for BOTH incremental paths. Runs before the
            # summary/count refresh below so they read the corrected edges.
            _refresh_test_covers_from_db(store)
        if report.bounded_link:
            # Now the DB reflects the delta: recompute the whole-design summaries
            # out-of-core (M12.5 SQL scans) and read the graph counts back.
            _refresh_summaries_and_counts_from_db(store, report)
    else:
        store.save(
            graph,
            files_meta,
            root=base,
            units=units,
            options_hash=opts_hash,
            discrepancies=discrepancies,
            ref_records=ref_records,
            summaries=summaries,
        )
    report.persist_s = time.perf_counter() - _t_persist
    # Persist content-free build telemetry so `hdl-kgraph review` can report it
    # from a static DB (timings live only in the report otherwise). Best-effort:
    # the build already succeeded and the DB is written, so a telemetry-write
    # failure degrades to a warning rather than failing the build.
    try:
        store.set_meta(
            "build_stats",
            json.dumps(
                {
                    "discover_s": report.discover_s,
                    "parse_s": report.parse_s,
                    "link_s": report.link_s,
                    "enrich_s": report.enrich_s,
                    "persist_s": report.persist_s,
                    "enriched": options.enrich,
                }
            ),
        )
    except Exception as exc:  # noqa: BLE001 — telemetry is non-critical
        report.warnings.append(f"failed to persist build_stats telemetry: {exc}")
    return report


def _enrich(
    graph: nx.MultiDiGraph,
    base: Path,
    options: BuildOptions,
    inputs: _Inputs,
    enrich_files: list[Path],
    vhdl_file_libs: dict[str, str],
    report: BuildReport,
    progress: ProgressFn,
) -> list[Discrepancy]:
    """Run the M7 enrichment pass over the linked graph (mutated in place).

    *enrich_files* is the standalone compilation-unit set (consumed headers
    already excluded by the caller); *vhdl_file_libs* maps each VHDL relpath to
    the library it analyses into, which the GHDL backend needs.
    """
    backends = available_backends(options.enrich_backends or None)
    if not backends:
        report.warnings.append(
            "enrichment requested but no backend is available. Install the SystemVerilog "
            "frontend with: pip install 'hdl-kgraph[enrich]' (VHDL also needs the `ghdl` "
            "system binary; see docs/enrichment.md)."
        )
        return []
    progress(f"pass 3: enriching via {', '.join(b.name for b in backends)}")
    enrich_input = EnrichmentInput(
        files=enrich_files,
        defines=inputs.defines,
        incdirs=inputs.incdirs,
        tops=list(options.top),
        base=base,
        vhdl_libraries=vhdl_file_libs,
    )
    enrich_report = run_enrichment(graph, enrich_input, backends)
    # Reconstruct the delta from the (now mutated) graph's elaboration stamps so
    # the build report and the standalone `enriched` command share one source.
    summary = summarize_enrichment(graph)
    report.enriched = True
    report.enrich_backends = summary.backends or enrich_report.backends
    report.edges_upgraded = summary.edges_upgraded
    report.enrich_nodes_added = summary.nodes_added
    report.enrich_generates_unrolled = summary.generates_unrolled
    report.discrepancy_count = len(enrich_report.discrepancies)
    report.enrich_diagnostics = enrich_report.diagnostics
    report.enrich_phase_s = enrich_report.phase_timings
    report.enrich_phase_counts = enrich_report.phase_counts
    return enrich_report.discrepancies


def _reuse_unit(
    found: DiscoveredFile,
    reuse: dict[str, StoredUnit],
    preprocessor: Preprocessor,
    processed: set[str],
    consumed: set[str],
) -> FileIR | None:
    """Decode *found*'s stored IR (replaying its macro events), or None.

    A corrupt stored row falls back to a fresh parse rather than failing the
    update.
    """
    stored = reuse.get(found.relpath)
    if stored is None:
        return None
    try:
        ir = ir_codec.ir_from_json(stored.ir)
        events = ir_codec.macro_events_from_json(stored.macro_events)
        included: set[str] = set(json.loads(stored.included))
    except (KeyError, TypeError, ValueError):
        return None
    if found.language is not Language.VHDL:
        for event in events:
            preprocessor.macros.apply(event)
        processed.add(found.relpath)
        consumed |= included - processed
    return ir


def run_update(
    root: Path,
    db_path: Path | None = None,
    options: BuildOptions | None = None,
    full: bool = False,
    progress: ProgressFn | None = None,
    tick: TickFn | None = None,
) -> UpdateReport:
    """Incrementally refresh the database after source edits.

    Re-parses changed/added files plus their include/macro dependents
    (removed files seed the closure too), re-links everything from stored
    pass-1 IRs, and rewrites the database. Falls back to a full rebuild when
    the database is missing/incompatible or the effective build inputs
    changed.
    """
    started = time.perf_counter()
    options = options if options is not None else BuildOptions()
    root = root.resolve()
    base = root.parent if root.is_file() else root
    db_path = db_path if db_path is not None else default_db_path(base)
    report = UpdateReport(root=root, db_path=db_path)
    max_kb = (
        options.max_file_size_kb
        if options.max_file_size_kb is not None
        else DEFAULT_MAX_FILE_SIZE_KB
    )

    def full_rebuild(reason: str) -> UpdateReport:
        report.full_rebuild_reason = reason
        report.build = run_build(root, db_path, options, progress=progress, tick=tick)
        report.elapsed_s = time.perf_counter() - started
        return report

    if full:
        return full_rebuild("forced with --full")
    if not db_path.is_file():
        return full_rebuild("no existing database")
    store = SqliteStore(db_path)
    # Bring an older but in-place-upgradable schema forward (#74) before reading
    # it; an un-migratable database still raises below and falls back to rebuild.
    store.migrate()
    try:
        meta = store.load_meta()
        stored_hashes = store.load_file_hashes()
    except SchemaVersionError as exc:
        return full_rebuild(str(exc))
    if meta.get("root") != str(base):
        return full_rebuild(f"build root changed (was {meta.get('root')})")
    if meta.get("options_hash", "").startswith("merged:"):
        # A merged database has no single set of build inputs to diff against;
        # re-link from sources rather than attempt an incremental update.
        return full_rebuild(
            "database was produced by `merge` and does not support incremental update"
        )

    inputs = _resolve_inputs(options, base)
    if meta.get("options_hash") != options_hash(base, options, inputs):
        return full_rebuild("build options changed (defines/incdirs/sources/libraries)")

    if progress is not None:
        progress("scanning for changed files")
    discovered = _discover(root, base, options, inputs, max_kb)
    current = _current_hashes(base, inputs, discovered)
    changes = diff_hashes(stored_hashes, current)
    if not changes:
        report.up_to_date = True
        report.elapsed_s = time.perf_counter() - started
        return report
    dependencies = store.load_dependency_graph()
    seeds = {path: "changed" for path in changes.changed}
    seeds.update({path: "removed" for path in changes.removed})
    dirty = dirty_closure(dependencies, seeds)
    dirty.update(newly_resolvable_includes(dependencies, changes.added))
    dirty.update({path: "added" for path in changes.added})
    discovered_relpaths = {found.relpath for found in discovered}
    report.reparsed = {path: why for path, why in dirty.items() if path in discovered_relpaths}
    report.removed = changes.removed
    # Files reparsed this run (dirty closure ∩ discovered) plus removed ones —
    # the set whose pass-2 refs the incremental linker re-resolves.
    dirty_files = set(report.reparsed) | set(report.removed)

    prior_file_warnings = store.load_file_warnings()
    prior_file_errors = store.load_file_errors()
    # The database exists and its schema/root/options matched above, so the delta
    # write applies; save_incremental still self-checks and falls back to a full
    # rewrite if the database changed underneath us.

    # Selective IR decode (#119): on the bounded path (the default, SV-only, no
    # enrich) clean units are macro-replayed from the small `macro_events` column
    # and only the dirty/affected units' full IRs are decoded — the prior build's
    # entire IR set never needs to be resident. Bind/config directives still need a
    # full re-link, so the path raises and we retry with the legacy full decode.
    has_vhdl = any(f.language is Language.VHDL for f in discovered)
    # cocotb forces a full re-link (cross-file DUT resolution; see
    # incremental_link_safe), so it must not take the selective-decode path that
    # leaves clean units' IRs undecoded. Only *parsed* cocotb units count — a
    # non-cocotb `.py` is skipped (``not_cocotb``).
    has_cocotb = any(f.language is Language.PYTHON and f.skipped_reason is None for f in discovered)
    # SDC (M10) forces a full re-link for the same reason as cocotb (cross-file
    # CONSTRAINS resolution + design-wide CLOCKED_BY upgrade).
    has_sdc = any(
        f.language in (Language.TCL, Language.PERL, Language.SLN) and f.skipped_reason is None
        for f in discovered
    )
    if (
        options.bounded_link
        and not options.enrich
        and not has_vhdl
        and not has_cocotb
        and not has_sdc
    ):
        macro_all = store.load_macro_events()
        if not macro_all:
            return full_rebuild("no stored parse results")
        macro_lite = {p: ev for p, ev in macro_all.items() if p not in dirty}
        try:
            report.build = _execute(
                root,
                db_path,
                options,
                inputs=inputs,
                reuse={},
                discovered=discovered,
                prior_warnings=prior_file_warnings,
                progress=progress,
                tick=tick,
                incremental=True,
                dirty_files=dirty_files,
                macro_lite=macro_lite,
                prior_errors=prior_file_errors,
            )
            report.elapsed_s = time.perf_counter() - started
            return report
        except _SelectiveLinkUnavailable:
            pass  # fall through to the full-decode path below

    stored_units = store.load_units()
    if not stored_units:
        return full_rebuild("no stored parse results")
    reuse = {path: unit for path, unit in stored_units.items() if path not in dirty}
    report.build = _execute(
        root,
        db_path,
        options,
        inputs=inputs,
        reuse=reuse,
        discovered=discovered,
        prior_warnings=prior_file_warnings,
        progress=progress,
        tick=tick,
        incremental=True,
        dirty_files=dirty_files,
    )
    report.elapsed_s = time.perf_counter() - started
    return report


def scan_changes(root: Path, db_path: Path, options: BuildOptions | None = None) -> ChangeSet:
    """Hash-diff the working tree against the stored build (``detect-changes``).

    Raises :class:`SchemaVersionError` for an incompatible database.
    """
    options = options if options is not None else BuildOptions()
    root = root.resolve()
    base = root.parent if root.is_file() else root
    max_kb = (
        options.max_file_size_kb
        if options.max_file_size_kb is not None
        else DEFAULT_MAX_FILE_SIZE_KB
    )
    stored_hashes = SqliteStore(db_path).load_file_hashes()
    inputs = _resolve_inputs(options, base)
    discovered = _discover(root, base, options, inputs, max_kb)
    current = _current_hashes(base, inputs, discovered)
    return diff_hashes(stored_hashes, current)


def _current_hashes(
    base: Path, inputs: _Inputs, discovered: list[DiscoveredFile]
) -> dict[str, str]:
    """Content hashes of the present tree: sources plus filelists."""
    current = {found.relpath: found.content_hash for found in discovered}
    seen: set[Path] = set()
    for fl in inputs.filelists:
        for filelist_meta in _filelist_meta(fl, base, seen):
            current.setdefault(filelist_meta.path, filelist_meta.content_hash)
    return current


def _library_for(path: Path, libraries: dict[str, Path]) -> str:
    """The VHDL library *path* compiles into: longest matching mapped prefix."""
    best = DEFAULT_LIBRARY
    best_len = -1
    for name, lib_path in libraries.items():
        lib_path = lib_path.resolve()
        if path == lib_path or lib_path in path.parents:
            depth = len(lib_path.parts)
            if depth > best_len:
                best, best_len = name, depth
    return best


def _library_ir(file_libs: dict[str, str], libraries: dict[str, Path]) -> FileIR:
    """LIBRARY nodes + LIBRARY->FILE DECLARES edges for the VHDL files seen.

    Emitted as an adapter IR (the FILELIST pattern); parser-emitted FILE
    nodes win the linker's first-occurrence dedupe, so the minimal FILE
    stubs here only matter if a file somehow failed to parse.
    """
    ir = FileIR(path="<vhdl-libraries>")
    seen: set[str] = set()
    for relpath, library in file_libs.items():
        lib_id = library_node_id(library)
        if library not in seen:
            seen.add(library)
            attrs: dict[str, object] = {}
            mapped = libraries.get(library)
            if mapped is not None:
                attrs["path"] = str(mapped)
            ir.nodes.append(
                Node(
                    id=lib_id,
                    kind=NodeKind.LIBRARY,
                    name=library,
                    qualified_name=library,
                    language=Language.VHDL,
                    attrs=attrs,
                )
            )
        ir.nodes.append(
            Node(
                id=file_node_id(relpath),
                kind=NodeKind.FILE,
                name=Path(relpath).name,
                qualified_name=relpath,
                file=relpath,
                language=Language.VHDL,
            )
        )
        ir.local_edges.append(Edge(src=lib_id, dst=file_node_id(relpath), kind=EdgeKind.DECLARES))
    return ir


def _walk_filelists(fl: Filelist, seen: set[Path]) -> list[Filelist]:
    if fl.path in seen:
        return []
    seen.add(fl.path)
    result = [fl]
    for nested in fl.nested:
        result.extend(_walk_filelists(nested, seen))
    return result


def _filelist_meta(fl: Filelist, base: Path, seen: set[Path]) -> list[FileMeta]:
    """files-table records for filelists (feeds the M4 incremental rebuild)."""
    metas: list[FileMeta] = []
    for each in _walk_filelists(fl, seen):
        relpath = Path(os.path.relpath(each.path, base)).as_posix()
        try:
            data = each.path.read_bytes()
        except OSError:
            continue
        metas.append(
            FileMeta(
                path=relpath,
                language=Language.UNKNOWN,
                content_hash=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        )
    return metas
