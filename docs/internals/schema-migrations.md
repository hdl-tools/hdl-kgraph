# SQLite schema migrations

The on-disk SQLite database (`.hdl-kgraph/graph.db`) is a **derived cache** of
the knowledge graph: everything in it can be regenerated from the HDL sources by
`hdl-kgraph build`. Its layout is versioned by `SCHEMA_VERSION`
(`storage/sqlite_store.py`), bumped whenever the tables, columns, or indices
change.

For a small design, regenerating the cache on a version bump is cheap. For a
large design the cache *is* the expensive artifact (a full re-parse + re-link can
take minutes), so a tool upgrade that bumped the schema used to be a multi-minute
wall. The **migration ladder** removes that wall for the common case where the
schema change is purely additive.

## How it works

`SqliteStore.migrate()` runs at the start of `update`/`watch`, before the
database is read:

1. It reads the stored `schema_version`.
2. If it already matches `SCHEMA_VERSION`, it is a no-op (`"current"`).
3. Otherwise it looks for a **contiguous chain** of registered steps
   (`_MIGRATIONS`, keyed `from_version -> (to_version, fn)`) from the stored
   version up to `SCHEMA_VERSION`.
4. If a full chain exists *and* the persisted IR encoding is compatible (see
   below), it runs the steps in one transaction, stamps the new version, and
   reports `"migrated"`. The subsequent `update` then proceeds incrementally —
   re-parsing only the edited files.
5. If there is **no** registered path (a gap in the ladder), or the IR encoding
   changed, it leaves the database untouched and reports `"rebuild"`; `update`
   falls back to a full `build`, exactly as before.

Read-only commands (`query`, `status`, `tree`, …) are **not** migrated in place —
they stay read-only and still refuse a mismatched database with a clear message
until the next `update`/`watch` migrates it.

## What may be registered as a migration

A step may be registered **only if an older database can be brought forward
without re-deriving data**. Concretely, additive DDL whose new state the reader
already treats as optional:

- ✅ `v7 → v8`: create the `summaries` table. It is created empty; until the next
  build/update repopulates it, readers fall back to computing each summary from
  the graph — the same path a genuinely pre-v8 database already takes — so the
  result stays correct.
- ❌ A table the linker depends on for **correctness** (e.g. `ref_index`, which
  the incremental linker reads to find affected references) cannot be created
  empty — an empty index would silently produce a wrong graph. Such a transition
  is deliberately left unregistered, so it routes to a full rebuild.

## IR-codec compatibility

The per-file pass-1 IR blobs (`file_irs.ir`) cannot be `ALTER`ed — if their
encoding changes, an in-place migration would leave undecodable rows behind.
`ir_codec.IR_CODEC_VERSION` makes this explicit and is stamped into `meta`. When
`migrate()` finds a stored `ir_codec_version` that differs from the current one,
it routes to a full rebuild regardless of the schema-DDL chain. (Databases
written before this feature carry no `ir_codec_version`; the registered chain is
trusted for them, since only IR-compatible steps are ever registered.)

## Adding a new migration

When you bump `SCHEMA_VERSION`:

1. If the change is purely additive **and** safe to leave empty/defaulted, add an
   `_migrate_<n>_to_<n+1>` function (use single `execute()` statements, not
   `executescript`, which would implicitly commit the migration transaction) and
   register it in `_MIGRATIONS`.
2. If the change alters the persisted IR encoding, bump `IR_CODEC_VERSION` and do
   **not** register an in-place step — let it rebuild.
3. Add a round-trip test mirroring `tests/test_store.py::test_migrate_*`.
