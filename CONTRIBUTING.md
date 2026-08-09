# Contributing to hdl-kgraph

Thanks for your interest! The project is stable (v1.0) — see
[ROADMAP.md](ROADMAP.md) for what's planned and where help is most valuable.

## Dev setup

```bash
git clone https://github.com/hdl-tools/hdl-kgraph
cd hdl-kgraph
pip install -e .[dev]
```

Before pushing:

```bash
ruff check . && ruff format --check . && mypy && pytest
```

CI runs the same checks on Python 3.10–3.13.

## Changelog

If your PR changes user-visible behavior, add a bullet under the
`[Unreleased]` section of [CHANGELOG.md](CHANGELOG.md), in the appropriate
group (`Added` / `Changed` / `Fixed` / `Removed` / `Security`). CI enforces
this for any PR that touches `src/`; for docs-only or purely internal changes
that don't warrant an entry, apply the `skip-changelog` label to the PR.

## The most valuable contribution: fixtures

HDL parsing lives and dies by real-world edge cases. If hdl-kgraph
mis-extracts (or chokes on) your code, please open an issue with the
**smallest self-contained HDL file that reproduces it** — strip proprietary
content, keep the construct. Accepted reproductions land in `tests/fixtures/`
and become permanent regression tests.

## Code conventions

- The graph schema (`src/hdl_kgraph/schema.py`) is the project's contract.
  Extending it (new node/edge kinds) is fine; changing existing meanings needs
  discussion in an issue first.
- Parsers are pass-1 only: per-file extraction, no cross-file resolution.
  Cross-file logic belongs in the pass-2 linker (`graph/builder.py`).
- Every cross-file edge gets a confidence score per the convention in
  `schema.py`.
