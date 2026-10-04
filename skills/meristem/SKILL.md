---
name: meristem
description: Living Memory Substrate for Claude Code — typed atom store, PPR retrieval, OODAR loop, halt-before-hallucination handoff. Use when the user asks about Meristem, mentions atoms/invariants/decisions/MIRRORS, or wants memory persisted across sessions. Sub-skills: meristem-tweak, meristem-patch, meristem-check, meristem-trace, meristem-handoff, meristem-resume, meristem-plan.
---

# Meristem — Living Memory Substrate

Meristem is a project-agnostic memory substrate that makes Claude Code an
*oriented* collaborator from token zero. The full design is in
`<workspace>/meristem/SPEC.md` once installed. This skill is the umbrella
entry — sub-skills handle specific phases of the OODAR loop.

## Mental model

Five sentences:

1. **Commits are atoms** — ingestion unit, not files.
2. **BM25 is bread, embeddings are luxuries** — embed only summaries.
3. **Tree-sitter is the universal parser** — 200+ languages.
4. **Memory is JSONL on git** — teammates pull memory like code.
5. **Atoms come in three sizes** — hook picks the size that fits the budget.

## The skills

| Skill | When to use |
|---|---|
| `/meristem:tweak`   | Tiny change, ≤2 files, no schema/auth |
| `/meristem:patch`   | Multi-file change needing invariant context |
| `/meristem:check`   | Audit staged diff before commit |
| `/meristem:trace`   | KB introspection — "where does X live" |
| `/meristem:handoff` | Write resumption doc at session end |
| `/meristem:resume`  | Restore tick from LATEST.md, continue |
| `/meristem:plan`    | Constrain a plan with graph facts before any edit |

## CLI surface

```bash
meristem init              # install in workspace
meristem ingest            # scan with all adapters
meristem status            # workspace state
meristem embed             # (re-)embed live atom summaries
meristem query "<text>"    # PPR retrieval
meristem route "<prompt>"  # classify + retrieve (UserPromptSubmit hook)
meristem digest            # SessionStart bootstrap JSON
meristem watch <file>...   # PostToolUse invariant check
meristem handoff --next X  # PreCompact resumption doc
meristem statusline        # Memory Pulse glyph line
meristem enumerate         # edge-case enumerator (SPEC §11)
meristem mcp               # FastMCP stdio server (needs meristem[mcp])
```

## Quickstart for a new repo

```bash
cd <your-repo>
pip install /path/to/meristem          # or `pip install meristem[mcp,embed]`
meristem init
meristem ingest
meristem embed
meristem status   # confirms atoms + repos
```

Then wire the hooks:

```bash
meristem hooks install             # project-local .claude/settings.json (default)
meristem hooks install --global    # ~/.claude/settings.json instead
meristem hooks install --statusline  # also set the Memory Pulse status line
meristem hooks status              # what's wired, and each hook's last heartbeat
```

It's idempotent (merges in place, byte-for-byte preserves every other key and
every other tool's hooks) and identifies its own entries by the `meristem hook
<event>` command prefix. Each hook event dispatches through one command, reading
its JSON payload from stdin — there's no `$CLAUDE_USER_PROMPT`-style shell
variable to interpolate:

```bash
meristem hook session-start   # SessionStart  — digest injection
meristem hook prompt          # UserPromptSubmit — per-turn relevant atoms
meristem hook post-tool       # PostToolUse — invariant-watcher
meristem hook stop            # Stop — conversation capture → review queue
meristem hook pre-compact     # PreCompact — handoff generation
```

`meristem doctor`'s `hooks.heartbeat` check reports when a wired hook hasn't
fired recently — install writes the wiring, but only a heartbeat proves it's
actually running.

## Halt-before-hallucination

At ≥85% context capacity, agentic skills (`patch`, `check`, `trace` with
sub-agents) enter safe-halt:
1. Write a handoff (`/meristem:handoff` or `meristem handoff --reason halt_safety`).
2. Stop. Refuse new tool calls except git status + atom-draft.
3. Resume in a fresh session via `/meristem:resume`.

Read-only operations may continue past the threshold but cannot draft
new atoms. Override with `--force-continue` (logged; user owns the risk).
