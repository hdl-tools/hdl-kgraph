"""The source inventory both benchmark arms are allowed to see.

Fairness rule #1: the grep baseline searches **exactly the HDL files the graph
indexed** — read from the ``files`` table, minus anything the build skipped.
A real no-graph agent would grep the whole working tree (in the validation SoC
that means 1.2 GB of Verilator objects and P&R netlists alongside 807 KB of
RTL), so restricting the baseline to the indexed set makes it *stronger* than
reality. That is the conservative direction, and it is the only way the two
arms are answering from the same evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hdl_kgraph.schema import Language
from hdl_kgraph.storage.sqlite_store import SqliteStore

#: The languages a `grep the RTL` baseline would plausibly search. Tcl/SDC and
#: the M8+ C/Python boundary files are indexed too, but no question template
#: targets them, so including them would only pad the baseline.
HDL_LANGUAGES = frozenset({Language.SYSTEMVERILOG, Language.VERILOG, Language.VHDL})


@dataclass(frozen=True)
class SourceFile:
    """One indexed HDL file that is present on disk."""

    relpath: str
    path: Path
    language: Language
    size_bytes: int


@dataclass(frozen=True)
class Corpus:
    """The file set both arms share, plus what could not be honoured."""

    root: Path
    files: tuple[SourceFile, ...]
    #: Indexed but no longer on disk — the signature of a stale database.
    missing: tuple[str, ...]

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    def read(self, relpath: str) -> str:
        """File text as an agent's Read tool would ingest it."""
        return (self.root / relpath).read_text(encoding="utf-8", errors="replace")


def load_corpus(db_path: Path) -> Corpus:
    """The indexed, non-skipped, on-disk HDL files recorded in *db_path*.

    Uses :meth:`SqliteStore.load_file_metas`, which reads the ``files`` table
    alone — the whole-graph ``load()`` would defeat the point of benchmarking
    the bounded reader.
    """
    store = SqliteStore(db_path)
    root = Path(store.load_meta()["root"])
    files: list[SourceFile] = []
    missing: list[str] = []
    for meta in store.load_file_metas():
        if meta.skipped_reason is not None or meta.language not in HDL_LANGUAGES:
            continue
        path = root / meta.path
        if not path.is_file():
            missing.append(meta.path)
            continue
        files.append(
            SourceFile(
                relpath=meta.path,
                path=path,
                language=meta.language,
                # The on-disk size, not the recorded one: a stale database
                # would otherwise mis-price the baseline it is being compared
                # against. `bench context` reports the staleness separately.
                size_bytes=path.stat().st_size,
            )
        )
    # Sorted so a run is reproducible and two runs diff cleanly.
    files.sort(key=lambda f: f.relpath)
    return Corpus(root=root, files=tuple(files), missing=tuple(sorted(missing)))
