# 4. Liveness — the self-invalidating part

Section 2 introduced the shape: `liveness_kind` (`regex` | `ast` | `sql` |
`none`), `liveness_target`, `liveness_pattern`, `liveness_last_ok`, and the
rule that staleness is inferred from absence — a check ran and
`liveness_last_ok` didn't move — never a write to `valid_to`. This section
covers the mechanism: what each predicate kind checks, what triggers a
check, and the bounded sweep that keeps it cheap at scale, all grounded in
`src/meristem/liveness.py` and its callers.

## The three predicate kinds

**`regex`** — `_check_regex` reads `liveness_target` (a repo-relative path,
resolved and traversal-checked by `_resolve_target`) and searches it for
`liveness_pattern` with `re.search(..., re.MULTILINE)`. It's the default
for anything expressible as text: the manifest ingester gives an npm
dependency like `dep:npm:zod` a pattern matching `"zod":` in
`package.json`, and `meristem decide` lets a human attach one directly —
`--liveness-target model.py --liveness-pattern 'is_verified.*False'` for a
constraint that a flag stays off. One instructive bug: a leading `\b`
word-boundary can never match a name whose preceding character is already
non-word — `@supabase/supabase-js` (preceded by a JSON quote) and `$emit`
both hit this, making the predicate permanently unsatisfiable regardless of
whether the fact still held (`tests/test_ingesters.py`, commit `4f593d1`) —
part of why symbol atoms moved to `ast`.

**`ast`** — `_check_ast` re-parses `liveness_target` with tree-sitter and
asks whether `liveness_pattern` (a symbol name, e.g. `$emit`) is still
declared, via `treesitter.symbol_declared`. It matches the parsed
identifier node's text rather than raw source, sidestepping the
boundary-assertion bug above by construction. `ingesters/symbols.py`
attaches it to every extracted function/class/method — a Python file's
`foo`, `Bar`, `baz` all get `liveness_kind = 'ast'`. It's the one
*conditionally* runnable kind: it needs both the `tree-sitter` core package
and that file's grammar importable, checked dynamically per atom
(`treesitter.language_for_suffix` / `is_available`) since that varies by
environment — a language with no grammar installed (Swift, in one test)
falls back to `regex`.

**`sql`** — self-referential: `_check_sql` runs `liveness_pattern` as a
`SELECT` against the atoms store itself, no `target` involved; it passes if
the query returns a row. Test example: a constraint "every invariant has an
owner" carries `liveness_pattern = "SELECT 1 FROM live_atoms WHERE
type='owner' LIMIT 1"` — live only as long as the store still contains at
least one owner atom. Because this runs unattended from a background sweep,
`_SQL_SELECT_RE` rejects anything not starting with `SELECT`, and
`sqlite3.execute()` independently refuses multi-statement strings, so a
`SELECT …; DELETE …` smuggling attempt fails closed as invalid SQL.

## What triggers a check

Two triggers. **At read time**, `retrieve()` verifies each candidate as
it's about to be emitted, when called with a `repo_root`: if
`liveness_last_ok` is older than `verify_freshness_seconds` (default 1h)
the predicate re-runs via `check_atom`; a fresher pass reports `trusted`
without re-running. A now-failing predicate drops that atom from the
results entirely, replaced by the next-best live one — never a fact
Meristem can't currently stand behind. `meristem query`, MCP `query_facts`,
and `router.route()` all pass `repo_root` so all three verify live (a
2026-08-07 fix closed a gap where `route()` alone skipped this, silently
reporting every atom as `none`); with no `repo_root` (unit tests,
ranking-only callers), verification is skipped outright. **On a background
sweep**, `revalidate_sweep` re-checks a bounded batch independent of any
query (below). `check_atom` is the shared entry point both triggers call:
look up `liveness_kind`/`target`/`pattern`, dispatch to the matching
`_check_*`, and on a pass bump `liveness_last_ok = now`.

Neither trigger adds anything. Liveness can only shrink a store; what the repo
grew since the last `ingest` arrives through `meristem sync` (SPEC §19), which
has its own triggers: the managed git hooks (`post-commit`, `post-merge`,
`post-rewrite`) and, as a backstop for commits made where no hook is
installed, a detached sync started by a Claude Code session that finds the
index behind HEAD (`[freshness] auto_sync_on_session_start`). A sync is
separate from a liveness check and does not re-run predicates; `meristem
doctor`'s `hooks.git` check reports when nothing is wired to run it.

## `revalidate_sweep`: bounded, not exhaustive

`revalidate_sweep(conn, repo_root, limit=50, threshold_seconds=86_400)`
does **not** touch every atom. It calls `stale_atoms()`, which selects only
atoms with a predicate (`liveness_kind` set, not `'none'`) whose
`liveness_last_ok` is `NULL` or older than the threshold (default 24h),
ordered oldest-first, capped at `limit` (default 50) — the `LIMIT` is
pushed into the SQL itself, not applied after fetching everything. The
sweep runs `check_atom` on just those 50 stalest candidates. Because a pass
bumps `liveness_last_ok` to now, the next atom in line becomes the new
oldest, so successive sweeps walk forward through the staleness frontier
rather than re-checking the same 50 forever. That's the "bounded
incremental" design: cost per sweep is `O(limit)` regardless of store size,
never a global scan over millions of atoms. An atom whose predicate can't
run at all right now (`ast` with no grammar installed for that language) is
counted separately as `unverifiable`, not folded into `failed` — a coverage
gap isn't evidence of rot.

## What "stale" means, and where `⚠ N stale` comes from

"Stale" is not a status column, it's a query. `stale_atoms()` is the same
function the sweep uses to pick candidates, and it also powers the Memory
Pulse's `⚠ N stale` glyph: atoms with a predicate whose last passing check
is missing or older than the threshold. The pulse recomputes this nightly
during consolidation, not every turn, since the underlying check is a
sweep, not a cheap count. In `meristem query`'s row output, `liveness_state`
also renders per-row (`✓` for `verified`/`trusted`, `?` for `unverifiable`,
`–` for `none`), so a reader sees atom by atom whether "still live" was
actually re-checked or just assumed.

## Atoms with no predicate

`liveness_kind = 'none'` (or `NULL`) short-circuits `check_atom` before any
runner: it returns `state="none"` immediately without touching
`liveness_last_ok`, since nothing was checked. Liveness has nothing to say
about these atoms — they're the `confirm_by` path from Section 2, a
reconfirmation horizon rather than a re-runnable check, for claims no grep
or AST query could verify against code. `module:`/`owner:` atoms are the
concrete case (`reaper.py`): carrying no predicate by construction, neither
`check_atom` nor `revalidate_sweep` can ever flag one stale — only
`confirm_by` expiry or the deletion reaper closing it outright can.

## The gap between catching and cleaning up

`meristem reap` only detects whole-file git deletions; a function renamed
or removed from a file that itself still exists is invisible to it. Five
symbol atoms across the test suite hit exactly this (commit `6e98ecc`):
their `ast`/`regex` predicates correctly failed on re-check since the named
symbol was no longer declared, so `retrieve()` dropped them from every
read — but they stayed **live** in the store, inflating `doctor`'s census,
until someone closed them by hand. Liveness hides a dead fact from readers
the moment its check fails; it does not, by itself, close the row.
