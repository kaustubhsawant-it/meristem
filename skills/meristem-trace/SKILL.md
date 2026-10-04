---
name: meristem-trace
description: Observe-phase KB introspection. Answers "where does X live, who writes Y, what closed decisions block Z". One sub-agent per workspace root, parent merges by symbol. Read-only.
---

# /meristem:trace — Observe-phase introspection

Use when the user asks the substrate a question rather than for a change.
Trigger phrases:
- "where does X live"
- "who writes Y"
- "what closed decisions block Z"
- "show me the chain from A to B"
- "what owns this"

## Two paths

### Single-workspace

```bash
meristem query "<question>" -f <optional path context>
```

Render the atoms with their provenance trails verbatim. Cite each atom
with: *"Surfaced because <trail> (atom_id=<id>)."*

### Multi-workspace (`--workspaces` flag)

Spawn one sub-agent per registered root (`meristem.toml [workspace].roots`).
Each agent runs `meristem query` against its own root and emits the SPEC §10
JSON contract with `agent="trace_worker"`. Parent merges by symbol and
deduplicates.

Hard cap: 5 sub-agents (SPEC §10 table). If `roots > 5`, the user
chooses which to scan — don't silently truncate.

## Output (mandatory)

```
Trace for "<question>":

  • <atom 1 summary>
    └── trail: <provenance, e.g. "1-hop via MIRRORS from auth_service.py">
  • <atom 2 summary>
    └── trail: <...>

Workspaces scanned: <N> (atoms: <M>)
```

## Boundaries

- **Read-only.** Never assert atoms during a trace.
- If the user asks "what should I do about X" — that's not a trace, it's
  a patch request. Suggest `/meristem:patch`.
- If 0 atoms surface, say so honestly: *"no atoms matched. The substrate
  has no recorded knowledge about <topic>; consider asserting one."*

## Halt safety

Read-only, but still pause at ≥85% capacity. The trace can resume in a
fresh session via `/meristem:resume`.
