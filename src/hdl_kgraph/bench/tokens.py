"""Token estimators for the context benchmark.

The default is deliberately dependency-free: a ``ceil(len/4)`` heuristic. It is
*not* an accurate token count for dense code (real tokenizers land nearer 3
chars/token on Verilog), but the benchmark's headline is a **ratio**, and the
same estimator prices both arms — so the savings percentage is robust even
where the absolute number is off by a third. Users who want exact counts can
install the ``bench`` extra and pass ``--tokenizer tiktoken``.

Every report prints :attr:`Estimator.name`, so a number is never separated
from how it was produced.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Estimator(Protocol):
    """Something that turns text into a token count."""

    #: Shown in the report so the number is interpretable.
    name: str

    def count(self, text: str) -> int: ...


class HeuristicEstimator:
    """``ceil(len(text) / 4)`` — the zero-dependency default."""

    name = "heuristic(chars/4)"

    def count(self, text: str) -> int:
        return (len(text) + 3) // 4


class TiktokenEstimator:
    """Exact counts via ``tiktoken``'s ``o200k_base`` (the ``bench`` extra).

    Not Anthropic's tokenizer — no local one is published — but a real BPE
    over the same text, which is a far better estimate than chars/4 and, being
    applied to both arms, does not bias the ratio either way.
    """

    name = "tiktoken(o200k_base)"

    def __init__(self) -> None:
        import tiktoken  # imported lazily: optional dependency

        self._encoding = tiktoken.get_encoding("o200k_base")

    def count(self, text: str) -> int:
        # disallowed_special=() — HDL source can legitimately contain the
        # literal text of a special token, and that must not raise.
        return len(self._encoding.encode(text, disallowed_special=()))


#: Accepted ``--tokenizer`` values.
TOKENIZERS = ("heuristic", "tiktoken")


def make_estimator(name: str) -> Estimator:
    """Build the estimator named *name*, raising ``ValueError`` if unavailable."""
    if name == "heuristic":
        return HeuristicEstimator()
    if name == "tiktoken":
        try:
            return TiktokenEstimator()
        except ImportError as exc:  # pragma: no cover — exercised via CLI test
            raise ValueError(
                "the tiktoken tokenizer needs the optional extra: pip install 'hdl-kgraph[bench]'"
            ) from exc
    raise ValueError(f"unknown tokenizer {name!r}; choose from {', '.join(TOKENIZERS)}")
