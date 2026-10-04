# 2. Data model

## The atom

An atom is a single typed claim about the codebase: one row in the `atoms`
table (`schema.sql`), identified by a content hash over
`(type, topic_key, summary_50w)`. Every atom carries provenance,
bi-temporal validity, an optional liveness predicate, multi-resolution
summaries (10w/50w/250w), and an embedding. `topic_key` groups every
version of "the same fact" — asserting a new claim under an existing topic
doesn't insert a competing row, it supersedes the old one (see below).

## Atom taxonomy

The taxonomy is closed at nine types, enforced by a `CHECK` on `atoms.type`:

- **invariant** — a rule the code enforces (DB constraint, assertion).
- **schema_fact** — a fact about the DDL: a table, column, or constraint.
- **decision** — see below; carries `decision_status`
  (`OPEN`/`CLOSED`/`DEFERRED`).
- **convention** — a project style rule; the one type with no automatic
  producer, only ever asserted by hand.
- **dependency** — a package/library fact, from manifests.
- **owner** — who is responsible for what, from git history.
- **runtime** — an operational fact (env var, service, port), from manifests.
- **build_recipe** — how something is built or run, also from manifests.
- **glossary** — a term definition, from docs and symbol names.

`decision` needed a second discriminator: `decision_class`
(`constraint` | `change`, its own `CHECK`). Every commit produces a
`decision` atom, and those swamped the load-bearing ones — in one real
workspace, "what decisions constrain this goal?" resolved to commit subject
lines 76 times out of 76. `constraint` forbids or shapes future work and
must surface before related work starts; `change` just records that
something happened, one per SHA, `CLOSED` by definition. The git ingester
always mints `change`; anything asserted deliberately — human, agent, or
`meristem decide` — defaults to `constraint`. Retrieval and the
SessionStart digest default to constraints; changes stay queryable as
history via the `constraints_live` view. This was chosen over a tenth type
to keep the taxonomy closed.

## Tiers

Atoms also carry a `tier`: `module`, `file`, or `symbol` (`CHECK`, default
`symbol`). This exists for retrieval cost at scale: a 1M-LOC repo and a
10K-LOC repo should both return a handful of atoms in comparable time, so
the candidate set must stay bounded regardless of repo size. A
`module`-tier atom summarizes a subsystem and holds `ROLLS_UP` edges down
to its `file`/`symbol` children. Retrieval runs a coarse pass first, over
whichever tier's atoms match the seed set, then drills into finer-grained
children only where the query's PageRank mass concentrates. Ingestion
emits module atoms up front from directory/package structure; symbol
atoms stay lazy and materialize on first drill-down.

## Bi-temporal validity

Every atom has `valid_from` (when the fact became true), `valid_to` (when
it was superseded — `NULL` means still live), `asserted_at` (when Meristem
recorded it), and `superseded_by` (the replacing atom, if any). A live
atom is one with `valid_to IS NULL` and `archived = 0` — the `live_atoms`
view is exactly that filter.

Reasserting a fact under the same `(type, topic_key, workspace_id)` doesn't
overwrite the row: `assert_fact` inserts the new atom, then closes the
prior live one by setting its `valid_to` and pointing `superseded_by` at
the new id. The old row never disappears — it stays queryable through
`history()`, ordered by `valid_from`. That's the point over a delete: a
delete destroys the fact that something was once believed true, which is
exactly what's needed to answer "didn't we already decide this?" or catch
an agent re-litigating a closed constraint. If the new claim diverges from
the old one — rather than being routine re-ingestion of a commit, schema
snapshot, or manifest — the supersede also raises a `CONTRADICTS` edge
between the two, so the conflict is surfaced instead of silently
overwritten.

Bi-temporal validity is not the staleness mechanism, though the two are
easy to conflate. Staleness is the **liveness predicate**'s job
(`liveness_kind`: `regex` | `ast` | `sql` | `none`, plus `liveness_target`
and `liveness_pattern`) — a runnable check that re-verifies an atom's claim
against the current repo. A passing check bumps `liveness_last_ok`; a
failing one writes nothing, so staleness is inferred from absence — checked
today but `liveness_last_ok` didn't move — rather than any write to
`valid_to`. A stale atom is not superseded or closed; it stays live and
keeps surfacing, just flagged (the Memory Pulse's `⚠ N stale` count).
Facts with no predicate — a stated policy, not something a grep or AST
query can check — instead get a `confirm_by` timestamp: past that horizon
the atom still surfaces but marked unconfirmed.

## Confidence and pinning

`confidence` is a `REAL` in `[0, 1]`, validated in `assert_fact` rather
than by a table `CHECK`. It feeds retrieval scoring: each candidate's rank
is multiplied by `confidence × 0.5^(age / half_life)`, age measured from
the most recent of `liveness_last_ok`, `valid_from`, or `asserted_at`. An
atom that keeps passing its liveness check stays fresh regardless of age;
one with no predicate sinks as it goes unconfirmed. Only `confidence = 0`
removes an atom outright; otherwise decay only down-ranks.

`pinned` protects an atom from consolidation — the periodic housekeeping
pass that can archive or down-rank low-value atoms — and sorts pinned
constraints first in the SessionStart digest. `archived` is the
soft-delete flag: excluded from `live_atoms` and default queries, but the
row and its history stay on disk.
