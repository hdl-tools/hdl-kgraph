"""Tier 2: where a line-oriented search gets the answer *wrong*.

Tier 1 deliberately scores cost only, because the graph cannot be the judge of
its own correctness. This tier supplies the missing half using ground truth a
human wrote by reading the sources — never taken from the graph's output.

The question is always the same shape ("which design units instantiate X?"),
so both arms produce a set of names and the comparison is exact. The shipped
suite lives in :mod:`hdl_kgraph.bench.suite` and covers a false negative
(a macro-hidden instantiation), a false positive (a comment and a string
literal), a case-sensitivity miss (a VHDL entity), and one case grep answers
perfectly. ``--suite PATH`` runs the same machinery over a user's own cases.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hdl_kgraph.bench.baseline import grep
from hdl_kgraph.bench.corpus import load_corpus
from hdl_kgraph.pipeline import default_db_path, run_build
from hdl_kgraph.storage.query import GraphQuery

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

#: The suite that ships with the package.
BUNDLED_SUITE = Path(__file__).parent / "suite"

#: A `module foo` / `entity foo` declaration — used to attribute a grep hit to
#: the design unit that encloses it, which is the manual step an engineer
#: performs when reading grep output.
_DECLARATION = re.compile(r"^\s*(?:module|entity)\s+([A-Za-z_]\w*)", re.MULTILINE)


@dataclass
class Case:
    """One hand-authored case, once both arms have answered it."""

    name: str
    target: str
    question: str
    expected: list[str]
    forbidden: list[str]
    why: str
    graph_answer: list[str]
    grep_answer: list[str]

    @staticmethod
    def _ok(answer: list[str], expected: list[str], forbidden: list[str]) -> bool:
        found = set(answer)
        return found == set(expected) and not (found & set(forbidden))

    @property
    def graph_correct(self) -> bool:
        return self._ok(self.graph_answer, self.expected, self.forbidden)

    @property
    def grep_correct(self) -> bool:
        return self._ok(self.grep_answer, self.expected, self.forbidden)

    @property
    def verdict(self) -> str:
        """How the two arms compare on this case."""
        if self.graph_correct and self.grep_correct:
            return "both correct"
        if self.graph_correct:
            missing = set(self.expected) - set(self.grep_answer)
            spurious = set(self.grep_answer) - set(self.expected)
            if missing and spurious:
                return "grep wrong (missed and over-reported)"
            if missing:
                return "grep incomplete"
            return "grep false-positive"
        if self.grep_correct:
            return "graph wrong"
        return "both wrong"


@dataclass
class FidelityReport:
    suite: str
    cases: list[Case]

    def to_json(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "question_shape": "which design units instantiate the target?",
            "ground_truth": "hand-authored by reading the sources, not derived from the graph",
            "cases": [
                {
                    "name": case.name,
                    "target": case.target,
                    "question": case.question,
                    "expected": case.expected,
                    "forbidden": case.forbidden,
                    "graph_answer": case.graph_answer,
                    "grep_answer": case.grep_answer,
                    "graph_correct": case.graph_correct,
                    "grep_correct": case.grep_correct,
                    "verdict": case.verdict,
                    "why": case.why,
                }
                for case in self.cases
            ],
            "summary": {
                "cases": len(self.cases),
                "graph_correct": sum(1 for c in self.cases if c.graph_correct),
                "grep_correct": sum(1 for c in self.cases if c.grep_correct),
            },
        }


def _graph_answer(query: GraphQuery, target: str) -> list[str]:
    """Instantiating design units, per the graph.

    ``instances_of`` records identify the site by ``qualified_name``
    (``macro_top.u_fifo``); the enclosing unit is everything before the
    instance label. Duplicates are expected and collapse here — an ambiguous
    target yields one record per candidate definition, all naming the same
    single instantiation site.
    """
    result = query.who_instantiates(target, 500, 0)
    owners = set()
    for item in result.get("items", []):
        qualified = str(item.get("qualified_name", ""))
        if "." in qualified:
            owners.add(qualified.rsplit(".", 1)[0])
    return sorted(owners)


def _grep_answer(corpus: Any, target: str) -> list[str]:
    """Instantiating design units, as recovered from grep output by hand.

    This is a *careful* reading, not a naive one: every matching line is
    attributed to the design unit that encloses it, and a hit on the target's
    own declaration is discarded. Making the baseline as good as a diligent
    engineer is the point — the failures that survive are real ones.
    """
    lines, hit_files = grep(corpus, re.compile(rf"\b{re.escape(target)}\b"))
    hits: dict[str, set[int]] = {}
    for line in lines:
        hit_path, hit_line, _ = line.split(":", 2)
        hits.setdefault(hit_path, set()).add(int(hit_line))

    owners: set[str] = set()
    for relpath in hit_files:
        text = corpus.read(relpath)
        # (line number, unit name) for each declaration in the file, so a hit
        # can be attributed to the unit it falls inside.
        declarations = [
            (text.count("\n", 0, match.start()) + 1, match.group(1))
            for match in _DECLARATION.finditer(text)
        ]
        for number in hits.get(relpath, set()):
            enclosing = [name for start, name in declarations if start <= number]
            if not enclosing:
                continue
            owner = enclosing[-1]
            # A hit on the target's own declaration is not an instantiation.
            if owner.lower() != target.lower():
                owners.add(owner)
    return sorted(owners)


def load_suite(suite_dir: Path) -> list[dict[str, Any]]:
    """The ``suite.toml`` case list for *suite_dir*."""
    manifest = suite_dir / "suite.toml"
    if not manifest.is_file():
        raise FileNotFoundError(f"no suite.toml in {suite_dir}")
    with manifest.open("rb") as handle:
        return list(tomllib.load(handle).get("case", []))


def run(suite_dir: Path | None = None) -> FidelityReport:
    """Build a graph over the suite and answer every case both ways."""
    suite_dir = (suite_dir or BUNDLED_SUITE).resolve()
    cases = load_suite(suite_dir)

    # The suite is read-only (it ships inside the package), so build into a
    # temp copy rather than writing a .hdl-kgraph/ into site-packages.
    with tempfile.TemporaryDirectory(prefix="hdl-kgraph-fidelity-") as tmp:
        root = Path(tmp) / "suite"
        root.mkdir()
        for source in sorted(suite_dir.iterdir()):
            if source.is_file():
                (root / source.name).write_bytes(source.read_bytes())
        run_build(root)
        db_path = default_db_path(root)
        query = GraphQuery(db_path)
        corpus = load_corpus(db_path)
        answered = [
            Case(
                name=str(case["name"]),
                target=str(case["target"]),
                question=f"Which design units instantiate {case['target']}?",
                expected=sorted(case.get("expect", [])),
                forbidden=sorted(case.get("forbid", [])),
                why=str(case.get("why", "")).strip(),
                graph_answer=_graph_answer(query, str(case["target"])),
                grep_answer=_grep_answer(corpus, str(case["target"])),
            )
            for case in cases
        ]
    return FidelityReport(suite=str(suite_dir), cases=answered)
