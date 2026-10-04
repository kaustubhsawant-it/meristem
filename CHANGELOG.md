# Changelog

Notable changes to Meristem. Dates are the date the work landed on `main`.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/).
Versions are not yet published to a package index, so a
version number here marks a point in this repository's history, not a release
artifact someone can install by name.

## [Unreleased]

## [0.3.0] — 2026-10-01

Adoption release: install in one command, share memory through git, keep secrets
out of it, review captured facts faster, and run on more machines. Docs now build
as a site.

### Added

- **`meristem setup`** (SPEC §21). Detects Claude Code, Cursor, Windsurf, Codex and
  Gemini CLI and wires Meristem in: Claude Code hooks plus a project `.mcp.json`,
  `mcpServers.meristem` for Cursor and Windsurf, and the sync git hooks.
  Codex and Gemini CLI get a printed snippet only — their formats are not
  documented in this repo and are not guessed. `--dry-run`, `--agents`, `--yes`.
  Idempotent, merges rather than overwrites, leaves a differing existing entry
  alone, does not touch a file that is not valid JSON, and backs a file up once to
  `<file>.meristem-bak` before first changing it. Works through `uvx meristem setup`.
- **Team onboarding.** `meristem init` in a clone whose shared export is present
  imports it into the fresh store and reports the atom count. Imported atoms have
  no vectors until `meristem embed` or `meristem sync`.
- **Trust guard** (SPEC §20). Deterministic secret and personal-data scanner
  (`guard.py`). Capture drops a unit that trips it (rule `guard`); export withholds
  an atom that trips it, with its summaries and edges, and `meristem export`
  names atom ids and finding kinds, never values; new `meristem doctor` check
  `guard.store` (22 checks now). Config `[guard] enabled`, `allow_patterns`;
  `[capture] exclude_terms` feeds the `excluded-term` kind.
- **Agent review suggestions** (SPEC §18.7). MCP tools `pending_facts` and
  `suggest_review`; `meristem review` shows the agent's suggestion beside each
  item. Advisory only: nothing is accepted or rejected except by a human. Stored
  in the gitignored sidecar `.meristem/review_suggestions.json`.
- **Statusline fast path** (SPEC §13). `meristem statusline --compact` and the
  `meristem-statusline` console script render with plain `sqlite3` and no CLI
  import (about 75 ms against about 125 ms through the CLI). The pulse shows
  `N to review` when captured facts are pending. `hooks install --statusline`
  prefers the console script when it is on `PATH`.
- **Scale benchmark** `tools/bench_scale.py`: synthetic repo, times `init --all`,
  incremental `sync`, `query` p50/p95 and store size. The symbols ingester caps at
  200 atoms per run, so results plateau; it does not demonstrate full-coverage
  monorepo indexing.
- **Docs site.** `mkdocs.yml` (Material theme), optional `docs` extra, and new
  pages: install, team memory, trust, an honest comparison with instruction files,
  editor rules and vector-memory tools, and performance.
- `meristem review --noise` / `--reject-noise`: list, or explicitly reject,
  pending candidates the current filter would no longer propose.
- `tools/replay_capture.py --labels DB [DB ...]`: per-rule counts and precision
  before/after over past review decisions; counts only, never candidate text.

### Changed

- Capture precision (SPEC §18.6). Measured from review labels (26 accepted vs
  95 rejected), `capture.extract` now hard-drops quoted/bracketed units, agent
  narration, meta-conversation, first-person musing and short verbless
  fragments, and `score_unit` penalises requests addressed to the agent
  (`-addressed`) and imperatives behind a discourse lead (`-imperative`).
  `capture.explain(unit)` names the rule that drops a unit.
- `candidates.queue_many` skips near-duplicates (token Jaccard ≥ 0.6) of
  rejected or pending candidates and of earlier items in the same batch.
- The unimplemented `[secrets]` stub is gone from `meristem.toml.example`; the
  `[guard]` section replaces it.
- CI gains an advisory Windows leg (3.11, 3.13), `continue-on-error` until it is
  green. It has never run on a real runner, so Windows remains untested.

### Fixed

- **Portability.** Text file I/O is explicitly UTF-8 throughout (a non-UTF-8
  platform default no longer changes what is read or written), detached
  background spawns are done per platform (`start_new_session` on POSIX,
  process-group flags on Windows), the `meristem.exe` sibling is found when
  locating the binary, and displayed home-relative paths use forward slashes.

## [0.2.1] — 2026-10-01

Auto-sync follow-through. A 2026-09-30 check found the git trigger from SPEC
§19.3 had silently stopped syncing in the repo Meristem is developed in, and
that the worktree-to-main flow advances HEAD in a way the trigger never saw.
Design and findings: SPEC §19.6. The fixes from the 2026-09-15 verification
pass, listed after this section, ship in this release too.

### Fixed

- **Example text in source comments and test fixtures made generic.**

- **A renamed or deleted workspace silently disabled sync for every other
  workspace sharing the repo.** A managed git hook hard-coded one workspace
  path, and installing overwrote any managed hook, so the last workspace to
  run `--install-hook` owned the repo's hook; once its path went away the
  hook exited on every commit. Managed hooks now list every workspace
  (`actions|path` lines in a quoted heredoc, safe for paths with spaces).
  Installing adds to the list and unions actions, so `import --install-hook`
  and `sync --install-hook` no longer drop each other's step; workspaces whose
  store is gone are pruned at install and skipped at run time. A 0.2.0
  single-workspace hook is read and upgraded in place.
- **Merges, pulls, and rebases never triggered a sync.** `post-commit` does
  not fire for `git merge`, `git pull`, or a fast-forward, which is how the
  worktree flow advances HEAD, and `post-merge` only ran `import`.
  `sync --install-hook` now also installs `post-rewrite` (amend/rebase) and a
  sync step in `post-merge`, which runs `import` then `sync` when both are
  wired. Re-run it once per workspace after upgrading.
- **The staleness notice was computed but never shown at session start.**
  Same bug class as the earlier pending-review fix: `build_digest` produced
  it, the hook never rendered it. It now appears in a Housekeeping block.
- **Git's own environment leaked into hook-spawned commands.** Managed hooks
  now `unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX
  GIT_OBJECT_DIRECTORY`, so a sync of one root cannot read another repo's
  index.

### Added

- **`meristem doctor` check `hooks.git`.** Warns, per git root, when there is
  no managed sync hook, when it does not list this workspace, or when it lists
  a workspace that no longer exists, and names the fix
  (`meristem sync --install-hook`). Doctor now runs 21 checks.
- **Background sync at session start.** When SessionStart (or a later prompt,
  once drift appears) finds the index behind HEAD it spawns a detached,
  debounced, lock-guarded `meristem sync --quiet`. The staleness notice is kept
  and annotated, never dropped because a sync started. Toggle:
  `[freshness] auto_sync_on_session_start` (default `true`).
- **Housekeeping block, re-surfaced mid-session.** SessionStart renders the
  index-behind-HEAD and captured-facts-awaiting-review nudges together;
  UserPromptSubmit repeats the block once when it changes, at most one nudge
  per stale episode (keyed on stale-or-not, not the commit count). Without a
  `session_id` it stays silent. What a session was last shown is kept per user
  in `housekeeping_seen.json` under `~/.meristem/` (or
  `$XDG_STATE_HOME/meristem/`), keyed by workspace, never inside a repo; a stray
  `<workspace>/.meristem/housekeeping_seen.json` left by an earlier build is
  removed automatically.
- A regression test that captured facts awaiting review are unchanged across
  `sync` and `ingest`.

### Carried from the 2026-09-15 verification pass

Fixes from a 2026-09-15 pre-mainstream verification pass,
found while checking the 0.2.0 stability claims and the paper's evidence
still held up before either leaves the lab.

#### Fixed

- **`uv.lock` drift.** Committed lock (`0942334`, 2026-09-12) had fallen out
  of sync with `pyproject.toml` — the first `uv run` on any checkout rewrote
  455 lines before any real work started. Regenerated; a new `lockfile` CI
  job (`uv lock --check`) catches this going forward. The `types` job, never
  in the required `ci` gate's `needs` list, was added to it in the same pass.
- **`meristem watch --all` left most of a large backlog stale.** Bounded to
  `--limit` (default 200) atoms per call, so a store with more stale
  predicates than that stayed mostly stale after following doctor's own
  printed remedy exactly. Now loops the same bounded batch until the
  staleness frontier actually clears, stopping only once a batch makes no
  further progress (a genuinely failing predicate never advances its own
  clock, so it must not be re-checked forever). Output gains `batches` and
  `frontier_cleared`.
- **Illustrative examples in docstrings, comments and docs replaced** with
  generic, synthetic infrastructure examples that preserve the same
  grammatical shape the capture heuristics are tuned against.

#### Added

- **`meristem --version`.** Previously only the `version` subcommand worked;
  the root-level flag every other CLI supports now does too.

## [0.2.0] — 2026-09-12

126 commits since 0.1.0. The headline is that the ambient layer — the part
that makes Meristem a memory rather than a database — actually ships in the
package now, and that a stability pass went looking for the class of bug this
project is most prone to and found five of them in production code.

### Added

- **The Claude Code ambient layer, in-package.** `meristem hooks install`
  writes the hook entries into `.claude/settings.json`, and the hooks
  themselves are pure-Python subcommands (`meristem hook session-start |
  prompt | post-tool | stop | pre-compact`) reading hook JSON from stdin.
  Previously these lived only in a user's `~/.claude/hooks/` and no external
  adopter could get them at all.
- **Fail-loud self-observability.** Every hook invocation writes a
  `hook_heartbeat` row *before* deciding whether to no-op, so `doctor` can
  distinguish "never invoked" from "invoked in the wrong workspace" from
  "invoked and silent". A silent no-op that cannot be detected is what cost
  this project eight days after the DLMS→Meristem rename.
- **Team sync protocol** (SPEC §16): git-mergeable JSONL export/import, a
  sharded mode for large stores, field-level merge with a real merge driver,
  and auto-import after `git pull` via installed hooks. A genuine same-topic
  disagreement between two developers surfaces as a `CONTRADICTS` edge rather
  than being decided by import order.
- **`meristem plan`** (SPEC §15) — planning mode, with a GSD `--json` consumer.
- **Evaluation harness** (`tools/eval_harness.py`): a synthetic corpus, a
  real-repo mode (`--real-repo-root`), baselines (grep, full-file-read, plain
  vector-RAG, HippoRAG-inspired, mem0-style), and ablations
  (`--with-ablations`: PPR off, liveness on, gate off).
- **`why(ref)` as an MCP tool**, mirroring `meristem why`.
- **`[capture] exclude_terms`** — a hard denylist so personal data (names
  of real people a project tracks) can never become a
  captured fact.
- **`session_state` is now written.** Declared in schema v1 and written by
  nothing until 2026-09-12; `handoff.write` now records each session, giving
  per-session halt telemetry that previously existed only as markdown.
- Swift and PHP symbol extraction (regex fallback).
- `meristem status` prints the full path and drift diagnosis below the table,
  where neither competes for column width.

### Fixed

- **Retrieval ranking was non-deterministic.** The same query against the same
  store could return different atoms across runs. Two causes: score ties broke
  in dict-insertion order, and — the subtler one — `_run_ppr` summed mass while
  iterating a hash-randomized set, and float addition is not associative, so
  the scores themselves moved in their low bits. Fixed with sorted iteration,
  `ORDER BY` on the edge reads, a sorted seed vector, and an atom-id tie-break.
- **`BLOCKS` edges could not be walked from the code they constrain.**
  `edges.link_blocks` promised in its own docstring that it "makes a constraint
  reachable by graph traversal from the code it governs", but stored the edge
  as a *directed* decision→code edge, so a CLOSED decision was unreachable from
  the file it blocked whenever its text shared no vocabulary with the query.
  The question `BLOCKS` exists to answer was silently under-answered. Fixed via
  `edges.REVERSE_TRAVERSABLE_KINDS`.
- **A repo indexed before its first commit reported "current" forever.** Its
  baseline sha was NULL, nothing could measure drift from it, and `meristem
  sync` skipped that root on every run regardless of how many commits landed.
- `meristem status` and `meristem doctor` both still rendered that repo as
  healthy after the underlying fix — `status` in green, and `doctor` as
  "drift unmeasurable — 0/N repo(s) never indexed", a count of zero pointing at
  the wrong check.
- **`meristem ingest` left stores silently unqueryable.** It does not embed
  (only `init --all` and `sync` do) and said nothing about it. A workspace was
  found with 471 atoms, zero embeddings and 14 of 14 retrievals returning
  nothing. `ingest` now warns and names `meristem embed`.
- `doctor` no longer warns about an idle `pre-compact` hook — it fires only on
  context compaction, so a week of ordinary sessions is healthy.
- An unvalidated atom type reaching `candidates.accept` surfaced as a raw
  `sqlite3.IntegrityError` with the constraint SQL in it; it now names the
  valid types.
- `edges.density` no longer false-fails commit-only (non-code) workspaces.
- `export`/`import` honour `[sync] mode` instead of always assuming
  `git-jsonl`.
- Multi-root liveness resolution, and a symbol-tier scaffolding leak in
  retrieval.
- The handoff git-state parser truncated the first dirty filename.

### Changed

- **Static type checking.** mypy runs clean over the package and gates in CI.
  Its first run found 27 errors, 13 of them from a single
  `-> tuple[object, object]` that had silently switched off checking for every
  caller.
- **The public surface is frozen** in `tests/surface_snapshot.json` — CLI
  commands, MCP tools, hook events, ingesters, edge kinds, doctor checks,
  schema version. Removing one is allowed; removing one silently is not.
- **Coverage is measured and reported in CI** (not gated). Every CLI command
  and every doctor check now has a test that asserts what it prints.
- Schema v7. Migrations are tested end-to-end from v1.

### Known limitations

- Not published. No GitHub remote, no PyPI package — `pip install meristem`
  does not work.
- A captured fact cannot be promoted to a `decision`: `assert_fact` requires a
  `decision_status` and `candidates.accept` has no way to supply one.
- `ingesters/manifest.py` is the worst-covered module at 52%.
- Retrieval quality on real repositories is far less validated than on the
  synthetic corpus; the real-repo gold question sets are thin.

## [0.1.0] — 2026-08-12

First tagged version, under the name DLMS (Dynamic Living Memory Substrate)
before the rename to Meristem. Typed atom store, PPR retrieval, liveness
predicates, `doctor`, `why`, and an MCP server over stdio/http/sse.
