# 6. The write path

Three routes put an atom in the store, and all three converge on one
function: `atoms.assert_fact`. It hashes `(type, topic_key, summary_50w)`
into the atom id, so re-asserting the same claim under the same topic is a
no-op, and asserting a *different* claim under an existing `topic_key`
auto-supersedes the prior live atom instead of duplicating it. Ingesters,
`meristem decide`, and an accepted review candidate all call this same
function — they differ only in what they call it with.

## Automatic ingestion

`src/meristem/ingesters/` ships eight adapters (`modules` and `owners` run
last, since they link edges to atoms the earlier ones produce):

- **readme** — README/CLAUDE/AGENTS/CONTRIBUTING/ARCHITECTURE at the root →
  one `glossary` atom per file, liveness on the H1 text. Re-run is a no-op
  unless the title or first paragraph changed.
- **manifest** — pyproject.toml, package.json, pubspec.yaml, Cargo.toml,
  requirements.txt, go.mod → `dependency`, `runtime`, `build_recipe` atoms
  (capped 300/run), each with a regex liveness against the manifest text.
- **schema** (`schema_sql.py`) — `CREATE TABLE` in `*.sql` → one
  `schema_fact` per table (`topic_key=table:<name>`, capped 300), liveness
  re-checks the declaration; a dropped/renamed table goes stale.
- **invariants** — the same `CREATE TABLE` bodies, keeping the constraint
  clauses (NOT NULL/UNIQUE/CHECK/REFERENCES/PRIMARY KEY) → one `invariant`
  atom per constraint (capped 400). Only *declared* constraints, not
  inferred business rules — the predicate has to actually be runnable.
- **git** — commits since `last_indexed_sha` (or last 50 on a fresh repo,
  capped 500/catch-up) → one `decision` atom per commit, `CLOSED`,
  `decision_class=change`, `topic_key=commit:<short_sha>`. No liveness (a
  commit is immutable); never supersedes, since each SHA is its own topic.
- **symbols** — top-level fn/class declarations, tree-sitter first with a
  per-language regex fallback → one `glossary` atom per symbol (capped
  200/run), `ast` or `regex` liveness.
- **modules** — top-level source dirs → one `glossary` atom per module
  (`tier=module`) plus `ROLLS_UP` edges to existing child atoms under it.
- **owners** — git-log authorship per directory → one `owner` atom per
  author above a 25% commit-share floor (max 3/dir), plus `OWNS` edges. No
  liveness — ownership isn't a regex-checkable claim; `valid_from` is the
  author's last commit there, so a quiet owner ages out via decay.

All eight route through `assert_fact`, so running `meristem ingest --all`
twice is safe: an unchanged source re-derives the identical atom id and
inserts nothing. A changed source supersedes the prior atom on its topic
and raises a `CONTRADICTS` edge if the claims actually diverge — except for
`commit`, `schema_snapshot`, and `manifest` sources, which count as normal
re-ingestion, not conflicting knowledge.

## Manual

`meristem decide "<claim>" --topic <key> [--status OPEN|CLOSED|DEFERRED]
[--because "<why>"] [--blocks <path>...] [--liveness-kind regex|sql]
[--liveness-target <file>] [--liveness-pattern <pattern>] [--pin]` is the
only way to write a decision that isn't a commit's after-the-fact record.
It always sets `decision_class=constraint` — a human or agent forbidding
future work, not narrating what already happened. `--blocks` raises
`BLOCKS` edges from the new atom to every already-ingested atom under those
path prefixes, making the constraint reachable by graph traversal from the
code it governs. Liveness is optional but pointed at directly: a
regex target/pattern pair against a file, or a `sql` pattern (a `SELECT`
against Meristem's own atoms table, for self-referential rules). Omitting
both prints a warning that the constraint can't self-invalidate.

The MCP server's `assert_fact` tool is the same underlying call — an
agent's direct equivalent of `meristem decide`, for any atom type, not just
decisions. It writes immediately, no review step. Its sibling,
`propose_fact`, calls itself "the low-risk write path" and states the
asymmetry directly: a proposal costs the user one keystroke to reject,
while "a wrong `assert_fact` becomes a fact that gets retrieved and
believed later." `propose_fact` doesn't assert anything itself — it calls
`candidates.queue` and lands in the same review queue as transcript
capture, covered next.

## Captured

Section 5 covered `capture.extract` scoring clauses and
`candidates.queue_many` inserting new ones (`INSERT OR IGNORE` on a
fingerprint, so a rejected fact never reappears). From there: `meristem
review` with no flags lists pending candidates — fingerprint prefix, score,
proposed type, and the clause truncated to 76 characters — plus a count of
previously-accepted facts past their reconfirmation horizon. `--accept
<prefix>` resolves the prefix (must be unique) and calls
`candidates.accept`, which calls the same `assert_fact`, with
`source_kind="manual"`, `source_ref` set to the transcript filename, and
`tier="module"`. It carries no liveness predicate — there's no code to
check a domain rule against — so it sets `confirm_by` 90 days out instead;
past that horizon the atom shows up in `unconfirmed_atoms()`. The candidate
row isn't deleted: its status flips to `accepted` and `atom_id` points at
the new atom, so the original scored clause and its transcript/session
reference persist as a permanent link back to where it came from.
`--reject <prefix>` flips status to `rejected`, and fingerprint dedup keeps
it excluded from every future run, not just this one.

Precision is measured from these reviews: every accept/reject is a label, and
the first measurement showed about one in five proposals accepted. Two things
came out of it. In `capture.extract`, named hard drops (`leading-quote`,
`narration`, `meta-conversation`, `first-person`, `verbless-fragment`) and two
`score_unit` penalties (`-addressed`, `-imperative` behind a discourse lead);
`capture.explain(unit)` returns the name of the rule that removes a unit. And in
`candidates.queue_many`, a near-duplicate check: a fact whose lower-cased
`[a-z0-9]+` token set has Jaccard ≥ `NEAR_DUP_JACCARD` (0.6) with a rejected or
pending candidate, or an earlier one in the same batch, is skipped (accepted text
never blocks). `meristem review --noise` lists pending candidates the current
filter would not propose, with the rule; `--reject-noise` rejects exactly those.
`tools/replay_capture.py --labels DB ...` replays the filter over past labels and
prints per-rule counts and precision before/after, never candidate text.

Extraction stays pattern-based rather than an LLM call for a reason
`capture.py`'s module docstring states directly: an extractor that itself
calls a model "would reintroduce exactly the cost and latency that keeps
hooks unwired." The tradeoff is explicit — marker-based extraction is
lower-precision than an LLM, chosen deliberately, because "extraction
proposes, a human disposes": a false positive costs one keystroke to
reject, but a wrong fact written straight into memory gets retrieved as
truth later.

## Worked example

Commit `2cedf6e` ("fix(hooks): surface pending-review candidates in the
SessionStart digest," 2026-08-21) is itself a plumbing fix, not a write:
`Digest.pending_review` had been computed by `build_digest()` all along,
but `_handle_session_start` never rendered it, so the nudge meant to close
this loop was silently dropped at the one surface every session reads. The
fix adds an unconditional line telling the agent to surface the backlog.

Its commit message claims something more concrete, though: "ran it
directly against this session's real transcript and it correctly extracted
a genuine candidate." That checks out against this repo's own live
`.meristem/atoms.sqlite`. A candidate (fingerprint `2c9ba9d15bb6d786`,
text "see check if an issue is genuine or not if yes then only we need to
fix it!", type `invariant`, `source=transcript`) was queued at
`2026-08-21T08:26:42Z`, roughly twelve minutes before `2cedf6e` landed, and
accepted at `08:42:20Z`, about four minutes after — into atom
`invariant_3ca42f2165f9` (`topic_key=verify-before-fix`), `confirm_by` set
exactly 90 days out. That's the loop end to end, on real data, in the same
session as the fix that made its digest nudge visible.
