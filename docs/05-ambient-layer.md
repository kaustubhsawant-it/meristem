# 5. The ambient layer (hooks)

## Wiring

Claude Code fires five lifecycle events Meristem listens to: `SessionStart`,
`UserPromptSubmit`, `PostToolUse`, `Stop`, `PreCompact`. `meristem hooks
install` (project-local `.claude/settings.json` by default, `--global` for
`~/.claude/settings.json`) JSON-merges five entries under the `hooks` key,
each pointing at `meristem hook <event>` (`hooks.py::install_hooks`,
`_EVENT_SPEC`), with per-event timeouts mirrored from the original JS
hooks' own budgets: 10s for `session-start` and `post-tool`, 12s for
`prompt` and `stop`, 15s for `pre-compact`. `PostToolUse` is the only entry
with a matcher, `Edit|Write|MultiEdit|NotebookEdit`; the other four fire on
every occurrence of their event. Install is idempotent (`_merge_event` /
`_is_ours` match by the `meristem hook ` command prefix): a repeat run
reports every event `unchanged` and leaves a hand-written neighbor hook
alone.

One dispatcher, `hooks.dispatch`, handles every invocation: resolve the
workspace from the hook JSON's `cwd`, chdir into it for the call
(downstream modules resolve their own workspace via `detect_layout()`,
which reads the process cwd, not the payload), and run the handler in a
daemon thread joined against an 8-second budget (`DEFAULT_BUDGET_SECONDS`)
— past that, `dispatch` gives up and stays silent rather than stall the
turn. `run_hook` never raises past its own boundary and always exits 0.
Before deciding anything, it writes a heartbeat (event, outcome, timestamp)
into the workspace's `hook_heartbeat` table, or a per-user JSON file when
the cwd isn't a Meristem workspace, so `meristem doctor`'s `hooks.heartbeat`
check tells "wired but doing nothing" apart from "never wired at all."

## SessionStart

`digest.build_digest()` assembles a `Digest`: branch and dirty files (`git
status --porcelain`), the last 3 commits, up to 5 top atoms, doctor health
findings, the pending-review count, staleness, and a literal halt-contract
string. What reaches the model is narrower — `_handle_session_start` in
`hooks.py` renders only atom count, schema version, branch, the top atoms,
a `Housekeeping:` block, and health findings into the injected block.
`dirty_files`, `recent_commits`, and `halt_contract` are computed and
available via `meristem digest --pretty` but are not part of the hook's
`additionalContext`. Top atoms are capped at `top_k=5`:
invariants first, then — only if invariants didn't already fill the quota
— CLOSED decisions classed `constraint` (not the git ingester's
one-per-commit `change` decisions), both ordered by confidence, re-sorted
pinned-first. An empty store gets pointed at `meristem ingest --all`; a
`fail`-severity doctor finding tells the agent to treat memory as
unreliable this session.

The Housekeeping block holds the two nudges a human has to act on, rendered
whenever present: `index is N commit(s) behind HEAD` (SPEC §19) and `N
captured facts awaiting review` (SPEC §18). Both were computed by the digest
before anything displayed them. When the index is behind and `[freshness]
auto_sync_on_session_start` is on (default), `_maybe_auto_sync` spawns
`meristem sync --quiet` fully detached (own session, stdio on `/dev/null`,
never raises) and the staleness line is kept, annotated `background sync
started; answers may lag until it finishes`. A sync that is debounced or
still running does not remove the notice. With the toggle off, or if no
`meristem` binary is found, the line says to run `meristem sync`. Doctor's
own `repo.freshness` line is dropped from the health list when the staleness
line is shown, so the same fact isn't stated twice; a *fail*-severity one
still triggers the DEGRADED verdict.

There is no branching in this hook between a fresh digest and a resumption
block. SPEC §9 specifies that behavior — a capped block replacing the
digest when `.meristem/handoffs/LATEST.md` is under 24 hours old — but it's
realized as the separate `/meristem:resume` skill, not in-hook logic:
`skills/meristem-resume/SKILL.md` reads `LATEST.md`'s frontmatter as
authoritative, applies `do_not_redo`/`failed_assumptions` as constraints,
and treats a handoff older than 24h as historical context, re-orienting via
`/meristem:trace` first rather than resuming directly. `SessionStart`
itself always calls `build_digest()`, regardless of a recent handoff.

## UserPromptSubmit (per turn)

Before any of that, `_handle_prompt` runs a Housekeeping check that applies
even to trivial prompts, since it costs one `COUNT(*)` and one `git rev-list`
per root and loads no embedder. SessionStart's notice is one-shot and sessions
run for days, so the check re-surfaces the block, headed `Housekeeping (changed
since last shown)`, when the pending-review count changed or the index became
stale since this session last saw it. "Stale" is a boolean in that comparison,
not the commit count — otherwise every commit in an active session would
re-fire the nudge before its post-commit sync caught up; one nudge per stale
episode. A change that is only a clearing (a sync caught up) says nothing.
Mid-session drift also starts the background sync, as at SessionStart. What a
session was last shown is kept per user, not in the repo: `housekeeping_seen.json`
in `~/.meristem/` (or `$XDG_STATE_HOME/meristem/`), keyed by workspace and then
`session_id`, newest sessions kept. A payload with no `session_id` gets no
re-surfacing. A failure here is swallowed and never costs the recall below.

`hooks._handle_prompt` skips slash commands, empty prompts, and anything
under `MIN_PROMPT_CHARS` (20 characters) before paying for an embedder
load. `router.classify` regex-matches the prompt into one of four classes,
checked in priority order — contradictory (`but we said`, `didn't we
decide`) beats implementation (`add|fix|refactor|remove|rename|…`) beats
navigational (`where is`, `show me`) beats semantic, the fallback at
confidence 0.3. Class sets retrieval shape: navigational pulls ≤3 atoms at
10-word summary resolution, semantic and implementation ≤5 at 50w,
contradictory ≤7 at 50w. `route()` calls `retrieval.retrieve` (PPR, per
section 3) then `retrieval.gate` against `min_relevance`, dropping anything
below the floor and separately hiding scaffolding atoms. Kept atoms are
added to the injected block one at a time until the next one would push
the running total past `TURN_TOKEN_CAP = 3000` (~4 chars/token) — the
per-turn cap section 1 alludes to without a number — enforced inside
`route()`'s own accumulation loop, not by the hook. An empty result after
gating renders nothing; the hook treats that as a real answer, not a
failure. Every call, kept atoms or not, is logged to `retrieval_log` with
its relevance scores.

## PostToolUse (invariant watcher)

Fires only for `EDIT_TOOLS = {Edit, Write, NotebookEdit, MultiEdit}`, and
only when the touched file resolves inside the workspace root.
`watcher.watch` looks up atoms whose `liveness_target` or `source_ref`
names that file and re-runs each one's predicate — the regex/ast/sql
checks from section 4 — via `liveness.check_atom`. No violations: silent,
nothing injected, the module's own "zero-cost happy path." A violation
injects a warning block naming up to 5 broken atoms (topic key or id, plus
the failure reason) and tells the agent to decide whether the edit or the
recorded fact is wrong, recording a genuine supersession via `meristem
decide` rather than leaving the substrate contradicting the code. This is
advisory, not blocking: `PostToolUse` fires after the edit has already
landed, so there is no mechanism here to reject it.

## Stop hook (conversation capture)

Reads `transcript_path` from the payload and runs
`capture.capture_transcript` over the last `MAX_CAPTURE_MESSAGES = 40`
human turns, filtering out subagent and injected-system messages by
`promptSource`. `capture.extract` splits each message into clauses and
scores each against weighted signal patterns — universal quantifiers,
exceptions, policy modals, prohibitions, defaults, conditionals,
corrections, scope, and a bare declarative copula — penalized for
imperative-lead phrasing (`add|fix|remove|…`) or dominant deixis (`this`,
`that`, "the current"), with pasted documents and machine-authored text
(the agent quoting its own output) filtered out before scoring. This is
the grammar of a standing rule, not sentence understanding: a unit scoring
≥ `DEFAULT_THRESHOLD` (4) becomes a `CandidateFact`, typed `invariant`
(prohibition/policy signals), `decision` (correction signals), or
`convention` (default). `candidates.queue_many` inserts each new
fingerprint into the `candidates` table via `INSERT OR IGNORE`, so a fact
the user already rejected never reappears. The hook itself stays silent
either way — nothing is injected into the current turn. The SessionStart
digest's pending-review count is what surfaces the backlog; the review
queue itself, `meristem review`, is covered in depth in section 6.

## PreCompact (handoff)

Fires on Claude Code's `PreCompact` event and unconditionally calls
`handoff.write(HandoffInput())` — no gating inside the hook itself. SPEC §9
lists other triggers (a skill's clean exit, a user-invoked
`/meristem:handoff`, ≥85% halt-safety, a `Stop` hook finding dirty files
with no handoff yet this session), but those fire outside this hook.
`handoff.write` renders YAML frontmatter — branch, last commit SHA, dirty
files, current tick, atoms consulted/drafted, open questions, next action,
failed assumptions, do-not-redo, edge cases pending — plus a narrative
capped at `NARRATIVE_TOKEN_CAP = 400` tokens, truncated at the nearest
sentence boundary. When the caller doesn't supply `atoms_consulted` /
`atoms_drafted` (true of every bare hook-triggered call),
`_auto_detect_atoms` fills them from `retrieval_log` and `atoms.created_at`
since the previous handoff's `ended_at` (or a 24-hour fallback window for a
workspace's first handoff), capped at 50 and 30 entries respectively. The
file lands at `.meristem/handoffs/<UTC-ts>-<session_id>.md` and is
committed; `LATEST.md` is refreshed as a symlink (gitignored) pointing at
it — the concrete artifact SessionStart's "recent handoff" means:
`LATEST.md` is what `/meristem:resume` reads as authoritative when under
24 hours old, and as historical orientation only once older.
