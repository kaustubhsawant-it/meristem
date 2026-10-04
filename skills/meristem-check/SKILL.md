---
name: meristem-check
description: Verify+Remember phase. Runs against the staged diff to audit invariants and find test gaps. Always spawns two sub-agents (invariant auditor + test impact analyzer). Use after /meristem:patch and before commit.
---

# /meristem:check — Verify + Remember

Runs after a patch is staged, before commit. Two-agent fan-out (always
spawn both):

## Sub-agent B1 — Invariant auditor

Scope: each touched atom's liveness predicate, post-diff.

```bash
# For each file in `git diff --staged --name-only`:
meristem watch <file>
```

Collects violations into the SPEC §10 JSON contract:
```json
{
  "agent": "invariant_auditor",
  "scope": "<staged diff>",
  "findings": [{
    "kind": "invariant_risk",
    "severity": "high",
    "claim": "<atom topic_key> liveness broke: <reason>",
    "evidence": ["<file:line>", "<atom_id>"],
    "suggested_action": "<concrete next step>"
  }]
}
```

## Sub-agent B2 — Test impact analyzer

For each touched line, find covering tests (test files referencing the
touched symbol):

```bash
# Pseudo: for each symbol in `git diff --staged`,
#   ripgrep across test dirs for that symbol name.
```

Emits `kind="test_gap"` findings when a touched line has no covering
test reference.

## Validation gate (mandatory)

For every finding from B1 and B2:
1. `evidence[]` files must exist
2. `evidence[]` line numbers must be in range
3. `evidence[]` atom_ids must exist in Meristem

A finding failing validation is **rejected**, not demoted. If 0 findings
survive, surface honestly: *"no concerns detected (N candidates rejected
as unverifiable)."*

## Output (mandatory)

```
✓ Checked: <N files staged>, <atoms audited>
  Invariant findings: <count by severity>
  Test gaps: <count>

Findings (top 5):
  • [high] <claim>  → <file:line>
  • [med]  <claim>  → <file:line>

Next: commit OR address findings then re-run
```

## Halt safety

`/meristem:check` is read-only — it may continue past 85% capacity but refuses
new atom drafts. If both sub-agents halt, surface a partial report and
suggest `--force-continue` or `/meristem:resume`.
