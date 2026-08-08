"""Source-file discovery with the M1 real-world-input guards.

Walks the build root (following directory symlinks, with loop protection)
for files matching the registered parser suffixes and
skips — while still recording, so ``status`` can report them — files that
match an exclude glob, exceed the size guard (huge generated netlists), or
contain ``\\`pragma protect`` encrypted IP (ROADMAP Risk #7).

M2 adds :func:`discover_from_paths` for explicit file sets (filelists,
config source globs): input order is preserved because compile order governs
``\\`define`` visibility, and two extra skip reasons appear — ``missing``
(a filelist entry that does not exist) and ``unsupported`` (a suffix no
parser handles yet). M3 adds the VHDL suffixes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from hdl_kgraph.parser.base import within_root
from hdl_kgraph.parser.c import C_SUFFIXES, CPP_SUFFIXES
from hdl_kgraph.parser.perl import SUFFIXES as PERL_SUFFIXES
from hdl_kgraph.parser.python import COCOTB_MARKER
from hdl_kgraph.parser.python import SUFFIXES as PYTHON_SUFFIXES
from hdl_kgraph.parser.sln import SUFFIXES as SLN_SUFFIXES
from hdl_kgraph.parser.sln import VS_SOLUTION_MARKER
from hdl_kgraph.parser.systemverilog import SUFFIXES as SV_SUFFIXES
from hdl_kgraph.parser.systemverilog import SYSTEMVERILOG_SUFFIXES
from hdl_kgraph.parser.tcl import SCRIPT_SUFFIXES, SDC_SUFFIXES, UPF_SUFFIXES
from hdl_kgraph.parser.vhdl import SUFFIXES as VHDL_SUFFIXES
from hdl_kgraph.schema import Language

SUFFIXES = (
    SV_SUFFIXES
    | VHDL_SUFFIXES
    | C_SUFFIXES
    | CPP_SUFFIXES
    | PYTHON_SUFFIXES
    | SDC_SUFFIXES
    | UPF_SUFFIXES
    | SCRIPT_SUFFIXES
    | PERL_SUFFIXES
    | SLN_SUFFIXES
)
_VS_SOLUTION_MARKER_BYTES = VS_SOLUTION_MARKER.encode()
_COCOTB_MARKER_BYTES = COCOTB_MARKER.encode()

DEFAULT_MAX_FILE_SIZE_KB = 1024
_PRAGMA_PROTECT_PROBE_BYTES = 4096


@dataclass
class DiscoveredFile:
    """One candidate source file, possibly skipped by a guard."""

    path: Path  # absolute
    relpath: str  # POSIX, relative to the build root (may contain ``..``)
    language: Language
    size_bytes: int
    content_hash: str = ""
    # None | 'exclude' | 'size' | 'pragma_protect' | 'missing' | 'unsupported' | 'not_cocotb'
    # | 'visual_studio_solution'
    skipped_reason: str | None = None


def _language_for(path: Path) -> Language:
    if path.suffix in VHDL_SUFFIXES:
        return Language.VHDL
    if path.suffix in C_SUFFIXES:
        return Language.C
    if path.suffix in CPP_SUFFIXES:
        return Language.CPP
    if path.suffix in PYTHON_SUFFIXES:
        return Language.PYTHON
    if path.suffix in SDC_SUFFIXES or path.suffix in UPF_SUFFIXES or path.suffix in SCRIPT_SUFFIXES:
        return Language.TCL  # SDC/XDC constraints, UPF power intent, Tcl flow scripts
    if path.suffix in PERL_SUFFIXES:
        return Language.PERL  # Perl codegen-lineage scripts
    if path.suffix in SLN_SUFFIXES:
        return Language.SLN  # Cadence Perspec System Level Notation
    if path.suffix not in SV_SUFFIXES:
        return Language.UNKNOWN
    return Language.SYSTEMVERILOG if path.suffix in SYSTEMVERILOG_SUFFIXES else Language.VERILOG


def check_file(
    path: Path,
    base: Path,
    exclude: tuple[str, ...] = (),
    max_file_size_kb: int = DEFAULT_MAX_FILE_SIZE_KB,
) -> DiscoveredFile:
    """Apply the skip guards to one file (already resolved to absolute)."""
    relpath = Path(os.path.relpath(path, base)).as_posix()
    found = DiscoveredFile(path=path, relpath=relpath, language=_language_for(path), size_bytes=0)
    if not path.is_file():
        found.skipped_reason = "missing"
        return found
    found.size_bytes = path.stat().st_size
    if path.suffix not in SUFFIXES:
        found.skipped_reason = "unsupported"
    elif any(fnmatch.fnmatch(relpath, pattern) for pattern in exclude):
        found.skipped_reason = "exclude"
    elif found.size_bytes > max_file_size_kb * 1024:
        found.skipped_reason = "size"
    else:
        # One read serves both the pragma-protect probe and the hash; the
        # size guard above already bounds how much this loads.
        data = path.read_bytes()
        if b"`pragma protect" in data[:_PRAGMA_PROTECT_PROBE_BYTES]:
            found.skipped_reason = "pragma_protect"
        elif found.language is Language.PYTHON and _COCOTB_MARKER_BYTES not in data:
            # A `.py` is only a source when it mentions cocotb — keeps ordinary
            # Python scripts (and hdl-kgraph's own sources) out of the graph.
            found.skipped_reason = "not_cocotb"
        elif (
            found.language is Language.SLN
            and _VS_SOLUTION_MARKER_BYTES in data[:_PRAGMA_PROTECT_PROBE_BYTES]
        ):
            # `.sln` collides with Visual Studio solution files — skip those by
            # their header so only Cadence Perspec SLN reaches the parser.
            found.skipped_reason = "visual_studio_solution"
        else:
            found.content_hash = hashlib.sha256(data).hexdigest()
    return found


def _walk_files(root: Path) -> Iterator[Path]:
    """Yield every file under *root*, following directory symlinks.

    ``Path.rglob``/``Path.glob`` do not descend into symlinked directories
    (until the Python 3.13 ``recurse_symlinks`` flag), so vendor/IP trees
    linked into the build root were silently missed. Visited real paths are
    tracked to break symlink loops.
    """
    visited = {root.resolve()}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        kept = []
        for name in sorted(dirnames):
            real = (Path(dirpath) / name).resolve()
            if real not in visited:
                visited.add(real)
                kept.append(name)
        dirnames[:] = kept  # prune already-visited (loop) dirs in place
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_file():
                yield path


def _walk_sources(root: Path) -> Iterator[Path]:
    """Yield files with parser suffixes under *root*, following dir symlinks."""
    return (path for path in _walk_files(root) if path.suffix in SUFFIXES)


def _match_glob(rel_parts: tuple[str, ...], pat_parts: tuple[str, ...]) -> bool:
    """``Path.glob``-style match: ``**`` spans segments, ``*``/``?`` do not."""
    if not pat_parts:
        return not rel_parts
    head, rest = pat_parts[0], pat_parts[1:]
    if head == "**":
        return any(_match_glob(rel_parts[i:], rest) for i in range(len(rel_parts) + 1))
    return (
        bool(rel_parts)
        and fnmatch.fnmatchcase(rel_parts[0], head)
        and _match_glob(rel_parts[1:], rest)
    )


def glob_sources(base: Path, pattern: str) -> list[Path]:
    """Files under *base* matching the relative glob *pattern*, sorted.

    Replacement for ``base.glob(pattern)`` that follows directory symlinks
    (with loop protection): the config ``sources`` globs are matched against
    the symlink-following walk instead of pathlib's traversal.
    """
    pat_parts = tuple(part for part in pattern.split("/") if part)
    matches = (
        path for path in _walk_files(base) if _match_glob(path.relative_to(base).parts, pat_parts)
    )
    return sorted(matches)


def discover(
    root: Path,
    exclude: tuple[str, ...] = (),
    max_file_size_kb: int = DEFAULT_MAX_FILE_SIZE_KB,
) -> list[DiscoveredFile]:
    """Find SV/Verilog sources under *root* (or just *root* if it is a file).

    Directory symlinks are followed; a file reachable through more than one
    path (symlink alias or loop) is reported once, under its first path in
    sorted order.
    """
    root = root.resolve()
    if root.is_file():
        paths = [root] if root.suffix in SUFFIXES else []
        base = root.parent
    else:
        paths = []
        seen: set[Path] = set()
        for path in sorted(_walk_sources(root)):
            real = path.resolve()
            if real not in seen:
                seen.add(real)
                paths.append(path)
        base = root
    return [check_file(path, base, exclude, max_file_size_kb) for path in paths]


def discover_from_paths(
    paths: Iterable[Path],
    base: Path,
    exclude: tuple[str, ...] = (),
    max_file_size_kb: int = DEFAULT_MAX_FILE_SIZE_KB,
) -> list[DiscoveredFile]:
    """Apply the guards to an explicit file set, preserving input order.

    Duplicates (same resolved path) keep their first position only, matching
    how simulators compile a file once.
    """
    seen: set[Path] = set()
    results: list[DiscoveredFile] = []
    for path in paths:
        path = path.resolve()
        if path in seen:
            continue
        seen.add(path)
        results.append(check_file(path, base, exclude, max_file_size_kb))
    return results


def source_dirs(discovered: list[DiscoveredFile], base: Path) -> list[Path]:
    """Distinct parent directories of non-skipped discovered files, within *base*.

    Used as automatic ``\\`include`` search directories so a header/define file
    that lives anywhere in the scanned tree resolves without an explicit ``-I``.
    Directories are confined to *base* (#68) and returned deduped and sorted, so
    a bare ``\\`include "abc.svh"`` resolves to a deterministic first match.
    """
    base = base.resolve()
    dirs: set[Path] = set()
    for found in discovered:
        if found.skipped_reason is not None:
            continue
        directory = found.path.parent.resolve()
        if within_root(directory, base):
            dirs.add(directory)
    return sorted(dirs)
