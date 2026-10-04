# 8. CLI + MCP surface

A reference map of the full `meristem` command surface and the MCP server,
grouped by whether each command is documented in depth elsewhere. Counted
from the `@app.command` decorators: **32 commands** — 29 top-level (2
hidden: `merge-driver`, `hook`) plus 3 under `meristem hooks`. A console
script, `meristem-statusline`, is a second entry point (below).

## Reference

Covered in depth elsewhere — one line each:

| command | does |
|---|---|
| `meristem init` | initialize `.meristem/` in the workspace (§7); `--all`/`-a` chains ingest + embed; a fresh store in a clone that carries the shared export imports it and reports the count ([team memory](12-team-memory.md)) |
| `meristem setup` | detect coding agents and wire Meristem into them — `--dry-run`, `--agents claude,cursor,codex,windsurf,gemini`, `--yes`; writes Claude Code/Cursor/Windsurf, prints snippets for Codex/Gemini ([install](11-install.md)) |
| `meristem ingest` | run ingestion across configured roots (§6) |
| `meristem embed` | embed live atoms whose summary changed since last run (§6) |
| `meristem decide` | assert a decision atom (OPEN/CLOSED/DEFERRED) with a liveness predicate and `--blocks` edges (§6) |
| `meristem review` | list/accept/reject captured facts from the review queue (§6); `--accept`, `--reject`, `--type`, `--topic`, `--limit`, `--noise` / `--reject-noise` (pending items the current filter would drop); an `agent` column / `[agent suggests ...]` hint shows advisory suggestions recorded over MCP |
| `meristem export` / `import` | write/merge the shared JSONL export for git-mergeable sync (§7); `export` withholds atoms carrying secrets or personal data and reports their ids and finding kinds ([trust](13-trust.md)) |
| `meristem merge-driver` | git merge driver for the export, hidden — not for direct use (§7) |
| `meristem sync` | catch the local index up to HEAD when it's behind; `--install-hook` wires the post-commit/post-merge/post-rewrite git hooks that run it (§7; SPEC §19.6) |
| `meristem hooks install/uninstall/status` | wire, remove, or inspect the five Claude Code hook entries (§5) |

Not yet covered — full list, with real depth below on `query`, `doctor`, and MCP:

| command | does |
|---|---|
| `meristem status` | config/db paths, schema version, atom count, per-repo drift |
| `meristem query` | ad-hoc PPR retrieval for a text query — below |
| `meristem plan` | constrain a plan for a goal with invariant/decision/BLOCKS context (SPEC §15) |
| `meristem why` | one atom's provenance: source, live liveness re-check, edges |
| `meristem enumerate` | enumerate edge cases for the staged diff (SPEC §11) |
| `meristem statusline` | render the Memory Pulse statusline; `--compact` takes the fast path (no CLI-framework import), which also shows `N to review` when captured facts are pending. The `meristem-statusline` console script is the same path without the CLI import cost |
| `meristem handoff` | write a resumption handoff, refresh `LATEST.md` |
| `meristem watch` | re-run liveness predicates for given files, or `--all` for the stalest store-wide |
| `meristem route` | classify a prompt, emit a token-capped atom injection block (JSON) |
| `meristem digest` | print the SessionStart digest as JSON |
| `meristem mcp` | run the MCP server — below |
| `meristem doctor` | health check: schema, atoms, liveness, embeddings, edges, retrieval — below |
| `meristem calibrate` | re-derive `retrieval.min_relevance` from this workspace's logged prompts |
| `meristem reap` | close atoms whose source file was deleted (`--since`, `--dry-run`) |
| `meristem migrate` | apply pending schema migrations |
| `meristem capture` | extract durable facts from a transcript into the review queue |
| `meristem propose` | queue one fact for review without a transcript |
| `meristem version` | print the CLI version |
| `meristem hook` | hidden — dispatches a hook event from stdin JSON; wired by `hooks install` |

There is no `meristem graph` command in this codebase. Graph structure is
inspected via `meristem why <ref>` (an atom's live edges) and, over MCP,
`graph_neighbors` (BFS from an atom). `query --file`/`plan --file` use the
graph as a retrieval seed but don't render it.

## `meristem query`

`meristem query "<text>"` is the direct, human-driven counterpart to the
retrieval `route()` runs every turn (§3, §5).

- `--file`/`-f` (repeatable) — file paths as structural PPR seeds.
- `--top` — atoms to return (default 10).
- `--min-relevance` — overrides `retrieval.min_relevance` from `meristem.toml`.
- `--gate` — drop below-floor atoms instead of dimming them. Unlike `route`, `query` shows everything by default since a human typed it; `--gate` opts into the injection path's usual suppression.
- `--scaffolding` — include module-tier atoms, hidden by default as scaffolding.

Output columns: `rel`, `score`, `live`, `type`, `topic_key`, `trail`,
`summary`. `rel` (cosine relevance) leads because it's the number SPEC
§14.6 found actually correlates with a real match; `score` (PPR mass share,
comparable only within one query) explains the ordering, not the match. `?`
in `rel` means unknown relevance, not zero. `live` renders the same
`liveness_state` glyphs as §4: `✓` verified/trusted, `?` unverifiable, `–`
none. Below-floor rows print dimmed, and trailing notes report how many
were suppressed, hidden as scaffolding, or unverified (no predicate runner
— confirm against source before relying on them).

## `meristem doctor`

Runs a fixed, read-only sequence of independent checks and prints one
finding each: `✓` ok, `⚠` warn, `✗` fail. Warnings don't affect the exit
code; exit code equals the fail count (capped at 1). `-v`/`--verbose` prints
each finding's detail lines.

22 checks in order, spanning schema/db integrity, atom census, liveness
(coverage/freshness/unverifiable), embeddings (drift/model match),
`edges.density`, ingest (completed/notes), retrieval (quality/calibration/
activity), capture queue/unconfirmed, `guard.store`, `hooks.heartbeat`, `repo.freshness`,
`hooks.git`, `handoff.dir`. `db.integrity` is skipped in the internal `quick` mode
digest/statusline use, for latency. One failing check never short-circuits
the rest, and a check that throws is itself recorded `fail` rather than
skipped — a dangling SQLite view once hid behind a swallowed exception.

Two checks tie to earlier sections. `hooks.heartbeat` reads the
`hook_heartbeat` row every Claude Code hook writes before doing anything
else (§5), and warns if the most recent heartbeat across all five events is
older than 7 days — a misconfigured hook otherwise looks identical to a
working one. `edges.density` fails once a workspace has more than 20
graph-eligible atoms (excluding commit-sourced ones, which no ingester
links) and still has zero edges; below that an edgeless store is just
small, not broken. The same 20-commit drift threshold gates `repo.freshness`.
`hooks.git` is read-only and asks whether anything is actually wired to keep
the index level with HEAD (SPEC §19.6): per configured git root it warns if
there is no meristem-managed `post-commit` hook, if that hook doesn't list this
workspace with a `sync` action, or if it lists a workspace whose store no
longer exists. The summary names the fix, `meristem sync --install-hook`.
Roots that aren't git repos are skipped. `guard.store` (0.3.0) warns with the
number of live atoms whose text trips the trust guard, naming atom ids and finding
kinds but never the matched value; those atoms are withheld from the shared export
([trust](13-trust.md)). It reports ok when none are found or when `[guard] enabled`
is false.

## MCP server

`meristem mcp` runs the server on FastMCP (optional extra:
`pip install 'meristem[mcp]'`; missing it prints an install hint, not a
crash). Three transports, per `SUPPORTED_TRANSPORTS`: `stdio` (default —
local clients spawn it as a subprocess: Claude Code, Claude Desktop,
Cursor, Windsurf, Cline, Continue) and `http` / `sse` (`--host`/`--port`,
default `127.0.0.1:8765`, for remote or web agents), rejected before
FastMCP is even imported if the name is a typo.

A stdio client config points at the binary, run from the target repo so it
finds that repo's `.meristem/`:

```jsonc
{
  "mcpServers": {
    "meristem": { "command": "meristem", "args": ["mcp"], "cwd": "/path/to/your-repo" }
  }
}
```

For http/sse: `meristem mcp --transport http --port 8765`, then point the
client's MCP URL at `http://127.0.0.1:8765`.

The complete tool set, 10 tools, read directly from `mcp_server.py`:

- `ping()` — health check, returns the resolved workspace path.
- `query_facts(query, file_context, top_n, min_relevance, include_suppressed, include_scaffolding)` — mirrors `meristem query`; returns a dict (`atoms`, `suppressed`, `scaffolding_hidden`, `top_relevance`, `silenced`), each atom carrying a `_retrieval` block (`score`, `relevance`, `liveness_state`, …) so "no answer" (empty `atoms`, nonzero `suppressed`) reads differently from a real failure.
- `assert_fact(type, topic_key, summary_50w, ..., blocks)` — direct write-back, mirrors `meristem decide` (§6); `blocks` links BLOCKS edges from path prefixes.
- `propose_fact(text, proposed_type)` — low-risk write path, queues for `meristem review` instead of writing immediately.
- `pending_facts(limit)` — the review queue for an agent to read: pending candidates, best first, each with `id` (the 8-char prefix `meristem review` shows), `text`, `score`, `proposed_type`, `noise_rule` (the capture-filter rule that would now drop it, or null) and any recorded `suggestion`. Nothing here is retrievable memory.
- `suggest_review(id, verdict, reason)` — record an `accept` or `reject` *suggestion* for a pending candidate. Advisory only: the candidate stays pending and the suggestion (stored in `.meristem/review_suggestions.json`) is shown beside it in `meristem review`, where a human decides. An id matching no single pending candidate is an error.
- `supersede(old_id, new_id)` — closes a prior atom in favor of a new one.
- `graph_neighbors(atom_id, hops, kinds)` — BFS over live edges; rows carry a `_hop` block (`depth`, `via_kind`, `via_edge`, `weight_acc`).
- `why(ref)` — one atom's full provenance, mirroring `meristem why` exactly (SPEC §16): source, a liveness re-check run now (never cached), edges.
- `embed_status()` — live atom count, embedded count, embedding model in use.

Every tool works from any MCP client. The ambient layer — SessionStart
digest, per-turn injection, the PostToolUse liveness watcher, PreCompact
handoff, the statusline, `/meristem:*` skills — stays Claude Code only;
other clients call these tools explicitly instead of getting auto-injected
context.
