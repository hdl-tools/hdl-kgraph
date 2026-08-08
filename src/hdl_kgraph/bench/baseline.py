"""The no-graph baseline: what an agent pays to answer by grepping the RTL.

This module is the one whose honesty decides whether the whole benchmark means
anything. A baseline that ``cat``s the design produces a meaningless 99.9%; a
baseline that magically greps the right regex produces a meaningless 0%. What
is modelled here is what Claude Code actually does without the graph:

1. one ``Grep`` over the source set, whose **matching lines** enter context;
2. one ``Read`` of each distinct hit file, whose **whole text** enters context
   (agents read files, not line windows — a port list or an always-block is
   unintelligible through a ±3-line keyhole);
3. repeat for a follow-up hop when the question needs one (hierarchy, impact).

Two deliberate choices:

**The scan is pure Python, never ``rg``, even where ripgrep is installed.**
A dual implementation would make the reported byte counts depend on which
machine ran the benchmark, and ripgrep is not universally present (it is
absent on the machine this was developed against). The equivalent ``rg``
command is still recorded in :attr:`Recipe.commands` so a reader can reproduce
the search by hand — it is documentation, not the measurement.

**The read is capped.** ``--baseline-max-files`` bounds how many hit files the
baseline ingests, modelling real agent patience and, more importantly, keeping
a pathological query (a signal named ``clk``) from inflating the result. When
the cap binds the row is flagged, and the flag is printed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from hdl_kgraph.bench.corpus import Corpus

#: Ordered so the recipe reads the way an agent would work.
DEFAULT_MAX_FILES = 10


@dataclass
class Recipe:
    """The evidence a baseline run ingested, and how it was obtained."""

    #: Human-readable, reproducible commands (the ``rg`` spelling).
    commands: list[str] = field(default_factory=list)
    #: Every chunk of text that would land in the agent's context.
    chunks: list[str] = field(default_factory=list)
    #: Distinct files the grep matched, before the cap.
    hit_files: list[str] = field(default_factory=list)
    #: Files actually read (``hit_files`` truncated to the cap).
    files_read: list[str] = field(default_factory=list)
    #: Whether the cap bound — i.e. the baseline was let off cheaply.
    capped: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.chunks)

    @property
    def bytes(self) -> int:
        return len(self.text)


def _word_pattern(name: str) -> re.Pattern[str]:
    r"""``\bname\b`` — the regex an agent reaches for first."""
    return re.compile(rf"\b{re.escape(name)}\b")


def grep(corpus: Corpus, pattern: re.Pattern[str]) -> tuple[list[str], list[str]]:
    """``(matching "path:line:text" lines, distinct hit files)``.

    Mirrors ``rg -n``: every matching line enters context, once per match.
    """
    lines: list[str] = []
    hits: list[str] = []
    for source in corpus.files:
        try:
            text = source.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matched = [
            f"{source.relpath}:{number}:{line}"
            for number, line in enumerate(text.splitlines(), start=1)
            if pattern.search(line)
        ]
        if matched:
            lines.extend(matched)
            hits.append(source.relpath)
    return lines, hits


def _search(
    recipe: Recipe, corpus: Corpus, pattern: re.Pattern[str], display: str, max_files: int
) -> list[str]:
    """One grep-then-read hop, appended to *recipe*. Returns the files read."""
    recipe.commands.append(f"rg -n {display!r} <indexed HDL files>")
    lines, hits = grep(corpus, pattern)
    recipe.chunks.extend(lines)
    fresh = [h for h in hits if h not in recipe.hit_files]
    recipe.hit_files.extend(fresh)

    budget = max_files - len(recipe.files_read)
    if budget <= 0:
        recipe.capped = recipe.capped or bool(fresh)
        return []
    to_read = fresh[:budget]
    if len(fresh) > len(to_read):
        recipe.capped = True
    for relpath in to_read:
        recipe.commands.append(f"read {relpath}")
        recipe.chunks.append(corpus.read(relpath))
        recipe.files_read.append(relpath)
    return to_read


def instances_of(corpus: Corpus, name: str, max_files: int = DEFAULT_MAX_FILES) -> Recipe:
    """ "Who instantiates X?" — grep the name, read every hit file."""
    recipe = Recipe()
    _search(recipe, corpus, _word_pattern(name), rf"\b{name}\b", max_files)
    return recipe


def port_map(corpus: Corpus, name: str, max_files: int = DEFAULT_MAX_FILES) -> Recipe:
    """ "What are X's ports and parameters?"

    The narrowest realistic grep — an agent can locate the declaration
    precisely — but it still has to read the file, because a port list cannot
    be extracted by a line-oriented match.
    """
    recipe = Recipe()
    pattern = re.compile(rf"^\s*(?:module|entity)\s+{re.escape(name)}\b", re.MULTILINE)
    _search(recipe, corpus, pattern, rf"^\s*(module|entity)\s+{name}\b", max_files)
    return recipe


def signal_drivers(corpus: Corpus, signal: str, max_files: int = DEFAULT_MAX_FILES) -> Recipe:
    """ "What drives signal S?" — grep the net, read every file that mentions it."""
    recipe = Recipe()
    _search(recipe, corpus, _word_pattern(signal), rf"\b{signal}\b", max_files)
    return recipe


def hierarchy(
    corpus: Corpus, top: str, depth: int = 3, max_files: int = DEFAULT_MAX_FILES
) -> Recipe:
    """ "What is the hierarchy under X?" — a bounded BFS of grep-then-read hops.

    Each level's instantiated module names are recovered from the files read at
    the previous level, which is exactly the loop an agent runs by hand.
    """
    recipe = Recipe()
    frontier = [top]
    seen = {top}
    instantiation = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:#\s*\(|[A-Za-z_]\w*\s*\()", re.MULTILINE)
    for _ in range(max(1, depth)):
        if not frontier or len(recipe.files_read) >= max_files:
            break
        next_level: list[str] = []
        for name in frontier:
            read = _search(recipe, corpus, _word_pattern(name), rf"\b{name}\b", max_files)
            for relpath in read:
                for child in instantiation.findall(corpus.read(relpath)):
                    if child not in seen:
                        seen.add(child)
                        next_level.append(child)
        frontier = next_level
    return recipe


def impact(corpus: Corpus, target: str, max_files: int = DEFAULT_MAX_FILES) -> Recipe:
    """ "What breaks if X changes?" — who-instantiates, transitively upward.

    Same shape as :func:`hierarchy` but climbing: each file that instantiates
    the target is itself a module whose own parents must then be found.
    """
    recipe = Recipe()
    declaration = re.compile(r"^\s*(?:module|entity)\s+([A-Za-z_]\w*)", re.MULTILINE)
    frontier = [target]
    seen = {target}
    while frontier and len(recipe.files_read) < max_files:
        parents: list[str] = []
        for name in frontier:
            read = _search(recipe, corpus, _word_pattern(name), rf"\b{name}\b", max_files)
            for relpath in read:
                for owner in declaration.findall(corpus.read(relpath)):
                    if owner not in seen:
                        seen.add(owner)
                        parents.append(owner)
        frontier = parents
    return recipe


def clock_domains(corpus: Corpus, max_files: int = DEFAULT_MAX_FILES) -> Recipe:
    """ "What are the clock domains?" — inherently whole-design.

    Nothing localizes this one: every sequential block in the design is
    evidence, so the baseline greps for edge-sensitivity across the corpus.
    This is the row where the cap is most likely to bind, and where the
    baseline is therefore most flattered.
    """
    recipe = Recipe()
    pattern = re.compile(r"\b(?:posedge|negedge)\b|\brising_edge\s*\(|\bfalling_edge\s*\(")
    _search(
        recipe, corpus, pattern, r"\b(posedge|negedge)\b|rising_edge\(|falling_edge\(", max_files
    )
    return recipe
