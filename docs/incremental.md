# Incremental updates, watch mode, and change analysis

The database is a derived cache that stays fresh as you edit.

## `update`

`update` re-parses only changed/added/removed files plus their dependents —
files that `` `include `` an edited header or expand a macro it defines —
and re-links everything else from stored parse results. One file edited in
a 2000-file design updates in about 1.5 s (budget < 1.8 s); see
[benchmarks.md](benchmarks.md).

The database write is scoped to the change too: when the pass-2 link is
incremental, `save_incremental` reads and rewrites only the dirty closure's
rows (the touched files' nodes/edges, fileless stubs, and the re-resolved
clean references) rather than diffing the whole `nodes`/`edges` tables — so a
one-file edit touches ~0.04 % of the rows on the 2000-file corpus. The result
is byte-identical to a full rebuild. As of v2.0 the incremental link is itself
memory-bounded by default: it re-resolves the dirty closure straight from SQLite
(selective IR decode, out-of-core `TEST_COVERS`) instead of loading the prior
graph, so neither the read nor the write scales with the design — see
[scalability.md](scalability.md).

A change to the effective build inputs (defines, incdirs, filelists,
library map) falls back to a full rebuild automatically, as does a database
written by an older schema version — there is no in-place migration,
because rebuild *is* the migration.

```bash
hdl-kgraph update                  # re-parse only what changed, re-link, save
hdl-kgraph update --full           # force a full rebuild
hdl-kgraph watch ./rtl             # debounced update on every save burst
```

`watch` needs the `watchdog` extra (`pip install 'hdl-kgraph[watch]'`) and
survives failing updates (a file deleted mid-update is reported, watching
continues).

## `detect-changes`

```bash
hdl-kgraph detect-changes          # M/A/D lines vs the last build
hdl-kgraph detect-changes --git    # ...or vs git HEAD (any ref works)
hdl-kgraph detect-changes --svn    # ...or vs the svn base (any revision works)
hdl-kgraph detect-changes --p4     # ...or the Perforce workspace's local changes
hdl-kgraph detect-changes --vcs    # ...or auto-detect which VCS the tree uses
hdl-kgraph detect-changes --closure  # include files dirtied via include/macro deps
```

`--git`, `--svn`, and `--p4` each take an optional ref/revision
(`--git main`, `--svn r42`); `--vcs` picks git/svn/p4 automatically from the
tree (`.git`/`.svn`, or a configured Perforce connection) and diffs against
that VCS's default ref. Perforce support reports the *local* workspace changes
(opened files plus reconciled on-disk edits) and needs a configured p4 client.

Exit codes follow the `git diff --exit-code` convention so scripts can tell
the cases apart: **0** nothing changed, **1** changes detected, **2** error
(missing database, bad config, VCS unavailable, ...). `--json` emits the change
set as structured data.

## `impact`

```bash
hdl-kgraph impact rtl/uart_tx.sv   # what does my change affect?
hdl-kgraph impact fifo --files     # affected files instead of design units
```

`impact` walks reverse `INSTANTIATES`/`IMPORTS`/`INCLUDES`/`EXTENDS` (plus
VHDL `USES_PACKAGE`/`IMPLEMENTS`/`BINDS` and macro-use) edges transitively:
the instantiating parents, importers, includers, and subclasses a change
can break. `--max-depth` limits the radius; `--json` emits records for
scripting.
