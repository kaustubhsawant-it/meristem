# Meristem — Living Memory Substrate

*Renamed from DLMS (2026-08-11) — the name collided with DLMS/COSEM (IEC 62056),
the international standard for smart-meter/device-management communication.*

A project-agnostic memory layer for coding agents. Deepest integration with
Claude Code (hooks + skills); the MCP tools work with any MCP-capable agent
(Cursor, Windsurf, Cline, Continue, Claude Desktop) — see
[Connecting other agents](#connecting-other-agents).

Drop into any repo. Claude Code learns the project's invariants, decisions, and
connections — and stays oriented across sessions without re-reading the codebase
or bloating context.

## Why

Coding agents forget. Every session starts cold, so they re-read files to rebuild
context — which burns tokens and, on large or mature codebases, hits the context
ceiling before any real work happens. Meristem gives the agent a persistent, typed,
self-invalidating memory so it can answer "what's true about this project" from a
graph of facts instead of re-reading source every time.

## How it works

Meristem stores knowledge as **typed atoms** (invariants, schema facts, decisions,
owners, …) in a local SQLite graph, with **bi-temporal validity** and **liveness
predicates** that auto-expire facts when the code they describe changes. Retrieval
walks that graph with **Personalized PageRank** to surface the most relevant
connected facts for the current query — no LLM call in the hot path.

The PageRank-over-knowledge-graph retrieval core is the same insight behind
[HippoRAG](https://arxiv.org/abs/2405.14831) (NeurIPS 2024), which showed single-step
PPR retrieval can match iterative RAG while being far cheaper and faster. Meristem is an
independent, from-scratch implementation of that idea, re-engineered as a *live*
memory layer for a coding agent: typed atoms, liveness-based invalidation, and tight
Claude Code hook integration — none of which HippoRAG (a static document index) has.
It is not a fork or derivative of the HippoRAG codebase.

## Quick start

```bash
uvx meristem setup             # detect your coding agents and wire Meristem into them
cd your-repo
meristem init --all            # writes meristem.toml, creates .meristem/, then ingests + embeds
meristem status                # show atom count, last indexed SHA
```

`meristem setup` detects Claude Code, Cursor, Windsurf, Codex and Gemini CLI. It
writes the Claude Code hooks and the MCP entry for Claude Code, Cursor and
Windsurf, and prints a paste-in snippet for Codex and Gemini CLI. Preview with
`meristem setup --dry-run`; existing files are merged, never overwritten, and
backed up once to `<file>.meristem-bak`. Run it from a repo that already has a
store (`meristem init` first) to also get the project-scoped files and the git
hooks. Details, requirements and uninstall: [docs/11-install.md](docs/11-install.md).
(`uvx` needs the package on an index you can reach; from a source checkout use
`pip install -e .` and `meristem setup`.)

`meristem init --all` (short flag `-a`) is the one-shot setup — it chains
`init` → `ingest` → `embed` so the substrate is ready immediately. Prefer the
step-by-step form if you want to inspect each stage:

```bash
meristem init                  # writes meristem.toml + creates .meristem/ (schema applied)
meristem hooks install         # Claude Code hooks, if you skipped `meristem setup`
meristem ingest                # first-time scan (~60s default budget; raise for big repos)
meristem embed                 # build local embedding vectors
meristem status                # show atom count, last indexed SHA
```

`meristem ingest` runs the adapters, then derives typed edges over the resulting
atoms (SPEC §4 auto-discovery). If you have an existing workspace whose
`meristem.toml` pins an older adapter list, run `meristem ingest --all` once to backfill
with every registered adapter.

## Staying current

An index nobody updates answers today's questions from last month's code — and
it does so with exactly the same confidence, which is what makes it dangerous.
Liveness can only ever *shrink* a store; nothing re-adds what the repo grew.

```bash
meristem sync --install-hook   # git hooks (commit, merge/pull, rebase): keep the index level, automatically
meristem sync --check          # how far behind am I? (exit 1 if behind)
meristem sync                  # catch up now
```

The hooks (`post-commit`, `post-merge`, `post-rewrite`) run `meristem sync`
detached — a git operation never waits on indexing, and never fails because of it.
`post-merge` matters as much as `post-commit`: merging a worktree branch into main,
or a `git pull`, moves HEAD without firing `post-commit`. The sync is incremental
(only files changed since the last indexed SHA) and debounced, so a 30-commit push
costs one ingest, not thirty. If you already have a hook of the same name that
Meristem didn't write, it refuses to overwrite it and prints the line to paste
instead.

A repo's hooks are shared, so each hook lists every workspace that uses the repo;
installing from a second workspace adds to the list instead of replacing it, and
workspaces whose store no longer exists are dropped. `meristem doctor`'s
`hooks.git` check warns if nothing is wired to sync the index, if the hook doesn't
list this workspace, or if it lists one that's gone — re-run
`meristem sync --install-hook` to fix any of them. (Upgrading from 0.2.0: re-run it
once per workspace to pick up `post-merge` and `post-rewrite`.)

Hooks only cover commits made where they're installed, so a Claude Code session
that starts behind HEAD also kicks off a detached `meristem sync --quiet` itself
(`[freshness] auto_sync_on_session_start`, default on). The staleness notice stays
in the digest, annotated that a sync started.

Drift is reported everywhere an answer is given: `meristem doctor` fails at 20+
commits behind, the SessionStart digest carries a `staleness` field, the Memory
Pulse shows `⟳ N behind`, and `meristem status` has a per-root drift column. Commits
behind is the honest measure — a root indexed 40 days ago with no commits since
is current, and one indexed this morning can already be 56 commits stale.

## Recording a decision

Not every decision is a commit. `meristem decide` captures a constraint at the
moment of choosing — which is when the reasoning is cheapest and most accurate:

```bash
meristem decide "No verified badge — legal liability; is_verified stays false" \
  --topic verified-badge --status CLOSED \
  --because "Counsel advised the claim creates liability we can't back." \
  --blocks src/users \
  --liveness-target src/users/model.py --liveness-pattern "is_verified.*False"
```

`--status` accepts `OPEN`, `CLOSED` or `DEFERRED`, so a decision still being
made is representable. `--blocks` raises `BLOCKS` edges to the atoms under those
paths, so retrieval surfaces the constraint when work approaches the code it
governs. The optional liveness predicate is what lets the constraint
*self-invalidate* when the code drifts from it — Meristem warns when you omit one.

A predicate can also check Meristem's own memory instead of a file — pass
`--liveness-kind sql` with a `SELECT` in place of `--liveness-target`:

```bash
meristem decide "Every invariant ships with an owner" --topic invariant-owners \
  --liveness-kind sql \
  --liveness-pattern "SELECT 1 FROM live_atoms WHERE type='owner' LIMIT 1"
```

It fails (self-invalidates) the moment that query stops returning a row. Only
a bare `SELECT` is accepted — the predicate runs unattended from background
liveness sweeps, so anything else is rejected before it runs.

Decisions recorded this way are classed `constraint`; the one-per-commit atoms
the git ingester mints are classed `change`. Context injection defaults to
constraints, so commit history doesn't crowd out the decisions that actually
bind you.

## Learning from the conversation

`meristem decide` captures a constraint when you stop and type it. On a real
workspace across ten weeks that happened **twice**, against 317 atoms the git
ingester minted automatically — so everything Meristem knew was re-derivable from
the repo, and everything worth remembering was not being captured at all.

The Stop hook closes that. It scans the session's *user* turns for the grammar
of a standing rule — universals, exceptions, prohibitions, policy modals — and
queues what it finds:

```bash
meristem review                      # what was captured this session
meristem review --accept c047f4a7    # promote to an atom
meristem review --reject 85c26372    # permanent — never proposed again
meristem propose "we never deploy on fridays"   # queue one by hand
```

Rules learn from your reviews. A new candidate that is a near-duplicate
(token Jaccard ≥ 0.6) of one you already rejected, or of one still pending, is
not queued again, and a handful of precision filters drop quoted templates,
agent narration, first-person musing and requests aimed at the agent. After
upgrading, clear the backlog the new filter would not have proposed:

```bash
meristem review --noise          # pending items the filter would now drop, with the rule
meristem review --reject-noise   # reject exactly those (explicit; never automatic)
```

### Agent suggestions (advisory)

An agent connected over MCP can help with the queue. `pending_facts` lists the
pending candidates (id, text, score, proposed type, and the noise rule that would
now drop it, if any), and `suggest_review(id, verdict, reason)` records an
`accept` or `reject` *suggestion*. `meristem review` then shows it beside the
item (`agent suggests reject: ...`). A suggestion never changes a candidate's
status: only you accept or reject. Suggestions live in a local sidecar file,
`.meristem/review_suggestions.json` (gitignored), not in the store.

### Statusline

`meristem hooks install --statusline` sets the Memory Pulse status line. When the
`meristem-statusline` console script is on `PATH` it is used instead of
`meristem statusline --compact`: same output, but it skips the CLI framework import
(roughly 75 ms against 125 ms in our measurement, see
[docs/15-performance.md](docs/15-performance.md)). The pulse shows `N to review`
when captured facts are waiting, next to `⟳ N behind` and `⚠ N stale`.

Extraction proposes; you dispose. A candidate is **not** memory — `retrieve()`
cannot see the queue — which is what lets the heuristics be permissive. They are
marker-based and stdlib-only: no model call, no network, nothing added to the
per-turn latency budget.

Accepted facts carry a **reconfirmation horizon** instead of a liveness
predicate. No regex can verify "the nightly job runs for every environment
except staging", so after 90 days the fact still surfaces but is marked
`unconfirmed` rather than silently trusted.

Claude Code picks up the substrate automatically once you run `meristem hooks
install` — it wires the five hooks below into `.claude/settings.json`
(project-local by default; `--global` targets `~/.claude/settings.json`
instead, and `--statusline` also sets the Memory Pulse status line). It's
idempotent and merges in place, so re-running it or running it alongside other
tools' hooks is safe. Check what's wired with `meristem hooks status`.
- SessionStart hook (digest injection, including a Housekeeping block: index behind HEAD, captured facts awaiting review)
- UserPromptSubmit hook (per-turn relevant atoms; repeats the Housekeeping block once when it changes)
- PostToolUse hook (invariant-watcher)
- Stop hook (conversation capture → review queue)
- PreCompact hook (handoff generation)
- The `meristem` MCP server (`query_facts`, `assert_fact`, `propose_fact`, etc.)

Every hook invocation writes a heartbeat row before it decides anything, and
`meristem doctor`'s `hooks.heartbeat` check reads it back — because a hook
that's misconfigured or silently no-ops otherwise looks identical to one
that's working, and that exact gap once hid a dead ambient layer for 8 days
after a rename. If `doctor` reports a hook unseen, something
upstream of this package — the settings.json wiring, the workspace path — is
wrong, not the substrate itself.

## Skills

- `/meristem:tweak` — tiny changes (≤2 files, no schema/auth) with KB-guided edits
- `/meristem:patch` — multi-file changes with sub-agent verification
- `/meristem:check` — invariant + test impact audit against staged diff
- `/meristem:trace` — "where does X live, who writes Y, what closed decisions block Z"
- `/meristem:handoff` — write a resumption handoff before context runs out
- `/meristem:resume` — restore the last handoff in a fresh session
- `/meristem:plan` — constrain a plan with graph facts before any edit

## Connecting other agents

The `meristem` MCP server speaks standard [Model Context Protocol](https://modelcontextprotocol.io),
so any MCP-capable agent can use its tools — `query_facts`, `assert_fact`,
`propose_fact`, `pending_facts`, `suggest_review`, `supersede`, `why`,
`graph_neighbors`, `embed_status`, `ping`. Install the extra
(`pip install 'meristem[mcp]'`) and point your client at the `meristem mcp` command.

**The quick way:** `meristem setup` does the wiring below for you. It writes the
`mcpServers.meristem` entry for Claude Code (`.mcp.json`), Cursor
(`.cursor/mcp.json`) and Windsurf (`~/.codeium/windsurf/mcp_config.json`), and
prints a snippet to paste for Codex and Gemini CLI, whose formats it does not
write. `--dry-run` shows the plan first; see [docs/11-install.md](docs/11-install.md).
The manual config below is for everything else.

What's portable vs. Claude-Code-specific:
- **Portable (any MCP client):** the substrate tools above — query the graph,
  write facts back, walk edges.
- **Claude Code only (for now):** the *ambient* layer — SessionStart digest,
  per-turn injection, PostToolUse liveness watcher, PreCompact handoff, the
  Memory Pulse statusline, and the `/meristem:*` skills. Other agents get the
  tools; they call them explicitly rather than getting auto-injected context.

**Local clients (stdio)** — they spawn the server as a subprocess. Example
`mcpServers` entry (Claude Desktop, Cursor, Windsurf, Cline, Continue all use
this shape):

```jsonc
{
  "mcpServers": {
    "meristem": {
      "command": "meristem",
      "args": ["mcp"],
      // run from your project root so it finds that repo's .meristem/
      "cwd": "/path/to/your-repo"
    }
  }
}
```

See [`examples/mcp/`](examples/mcp/) for ready-to-edit config files per client.

**Remote / web agents (http or sse)** — serve over a port instead:

```bash
meristem mcp --transport http --host 127.0.0.1 --port 8765
```

then point the client's MCP URL at `http://127.0.0.1:8765`.

## Team memory

The shared substrate travels through git as text. `meristem export` writes the
atoms to `.meristem/atoms.jsonl` (or sharded files), `meristem import` merges
them back, and a registered git merge driver merges two branches' exports row by
row. Two people recording different answers to the same topic get a `CONTRADICTS`
edge instead of a silent winner. On a fresh clone, `meristem init` imports the
team's export for you (`imported N atoms from the shared export`); imported atoms
need `meristem embed` (or `meristem sync`) before they have vectors, which
`init --all` runs. See [docs/12-team-memory.md](docs/12-team-memory.md).

## Trust

Everything runs locally; the store is a gitignored SQLite file and there is no
model call in the hot path. A deterministic guard scans for secrets (keys,
tokens, private keys, `password=` assignments, high-entropy strings) and personal
data (emails, phone numbers, your `[capture] exclude_terms`). A captured unit that
trips it is never proposed; an atom that trips it is withheld from the shared
export, and `meristem export` names the atom id and finding kind (never the
value). `meristem doctor`'s `guard.store` check counts live atoms that carry
findings. Configure with `[guard] enabled` and `allow_patterns`. See
[docs/13-trust.md](docs/13-trust.md). How this compares with instruction files and
vector-memory tools, including where Meristem is weaker:
[docs/14-comparison.md](docs/14-comparison.md).

## Maintenance

```bash
meristem doctor                # health report: schema, liveness, embeddings, edges, drift
meristem migrate               # apply pending schema migrations (idempotent)
meristem sync                  # catch the index up to HEAD (see "Staying current")
meristem calibrate             # re-derive the relevance floor from your own prompts
meristem reap                  # close atoms whose source file was deleted
```

`meristem ingest` already reaps the interval it indexes, so `meristem reap` is for the
backlog: a store built before reaping existed still holds a live atom for every
file ever deleted. Point it at an old commit to sweep them in one pass, and use
`--dry-run` first:

```bash
meristem reap --dry-run --since $(git rev-list --max-parents=0 HEAD)
```

Reaping closes atoms, it never deletes rows — the fact was true, and now it has
an end date. It acts only on deletions git reports, so an atom it cannot tie to
a deleted path is left alone.

### Calibrating the relevance floor

Meristem stays quiet when nothing it holds matches your prompt. The threshold for
"matches" is `[retrieval] min_relevance` in `meristem.toml`, and the shipped 0.70 is
**a starting point, not a constant** — it was derived from one embedder against
one corpus. Cosine scales differ per model and per store, so on a new or small
workspace 0.70 can suppress genuine matches, and under a different embedder it
may not filter anything.

`meristem calibrate` replays your own logged prompts and a fixed nonsense probe set
through real retrieval, and reports where the floor belongs:

```
              relevance calibration
  nonsense ceiling (p95)   0.2046   12 probes
  genuine floor (p25)      0.2183   28 prompts
  separation              +0.0137

recommended 0.2081  (configured 0.7) — config is 0.492 too high
```

`meristem calibrate --write` stores the result in `meristem.toml`, editing that one key
and leaving your comments and other settings alone. It needs a workspace with
some history — at least 20 embedded atoms and 20 distinct prompts — and says so
rather than guessing when it doesn't have them. `meristem doctor` watches the same
thing continuously and points you here when the floor looks wrong.

`meristem doctor` surfaces silent rot (schema drift, stale liveness predicates,
mixed embedding models, an edgeless graph, a workspace that was set up but never
ingested, queries that keep coming back empty) before it turns into a wrong
answer. If it reports a schema-version gap, `meristem migrate` brings the DB up to
date. Schema migrations also run automatically whenever the store is opened, so
read commands never trip over a column added by a newer version.

**You don't have to remember to run it.** The SessionStart digest carries any
non-ok findings, so an agent is told at token zero that the substrate is empty
or degraded rather than inferring it from a session of empty retrievals. That
matters more than it sounds: a health check nobody reads cannot prevent
anything.

Retrieved facts are labelled with how far Meristem can vouch for them — `verified`
(predicate re-ran and matched), `trusted` (checked recently), `none` (no
predicate), or `unverifiable` (the predicate has no runner yet). An atom nothing
could check is never presented as one that passed.

### Why should I believe this atom?

```bash
meristem why table:users.email.not_null    # topic_key, or an atom id
```

Prints one atom's full story: where it came from (`source_kind`/`source_ref`,
when it was asserted), a liveness check re-run right now against your repo
(not read from cache), and every edge touching it with its evidence — the
demonstrable half of the no-hallucination claim. `meristem query` answers "why was
this surfaced for my prompt"; `meristem why` answers "why should I trust this fact
at all", independent of any query.

## Developing

> **Status of this repository.** This tree currently contains the package
> source, schema, skills and documentation. The test suite, the `tools/`
> scripts (`smoke_e2e.py`, `eval_harness.py`, `bench_scale.py`,
> `replay_retrieval.py`, `replay_capture.py`), `uv.lock` and the CI workflow
> that the sections below and several docs refer to are not published here yet,
> so the commands below will not run from a fresh clone, and the benchmark
> figures in the accompanying paper cannot yet be reproduced from this
> repository.

```bash
uv venv && uv pip install -e ".[dev,vec,mcp]"
pytest                            # unit suite
ruff check .                      # lint gate — CI requires zero findings
python tools/smoke_e2e.py         # end-to-end against the `meristem` on PATH
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs three gates on
every push and pull request: `ruff check`, the unit suite on Python 3.11–3.14
across Linux and macOS (plus an advisory Windows leg that has never run on a real
runner), and a packaging job that builds the wheel, installs it
into a clean virtualenv, and drives the installed `meristem` binary through
`tools/smoke_e2e.py`.

That last job exists because the unit suite imports `meristem` from the source
tree, so it is blind to whether the *packaged* artifact works. `schema.sql` ships
as force-included package data; if that ever regresses, `meristem init` breaks for
every new user while the whole test matrix stays green. The smoke test also
drives drift as a state machine — index a repo, commit past it, assert
`sync --check` goes red, close it, assert it goes green — which is a sequence no
unit test spans.

The `embed` extra (sentence-transformers) is deliberately left out of CI: it
pulls ~2GB of torch and no test needs it, because the deterministic hash
embedder covers the same code path.

The suite is offline-deterministic: `tests/conftest.py` sets
`MERISTEM_EMBEDDER=hash` plus `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE`, so
nothing reaches the network or a real model even if `[embed]` is installed.
Real-subprocess MCP e2e tests (stdio/http/sse) are marked `slow` and included
by default; for a faster inner loop skip them with `pytest -m "not slow"`.

### Measuring retrieval quality

```bash
python tools/eval_harness.py              # print the comparison table
python tools/eval_harness.py --json out.json --keep-repo
```

A small multi-hop benchmark (SPEC §16) run entirely in-process against a
synthetic repo: three code↔schema pairs where the governing constraint shares
no vocabulary with the code symbol that touches it, so a graph hop (or a
direct embedding hit) is the only way to connect them. It reports Meristem's
recall and token cost against naive grep and full-file-read on the same
questions — a receipt for "comparable to HippoRAG," not a vibe. Like
`replay_retrieval.py`, it's a standalone script, not a CI gate.

## See

- [docs/](docs/README.md) — fifteen-section walkthrough: data model, edge graph,
  liveness, hooks, write path, sync protocol, CLI + MCP surface, design
  principles, related work, install, team memory, trust, comparison, and
  performance (`mkdocs.yml` builds it as a site: `pip install 'meristem[docs]'`
  then `mkdocs serve`)
- [SPEC.md](SPEC.md) — design decisions, atom taxonomy, OODAR loop
- `meristem.toml.example` — config schema with comments

## License

Business Source License 1.1 — see [LICENSE](LICENSE). The source is public; use for your own development, research and reproduction of the paper is allowed, and offering it to third parties as a competing product or service is not. On 2030-01-01 it converts to Apache-2.0. Earlier copies distributed under MIT keep that licence.
