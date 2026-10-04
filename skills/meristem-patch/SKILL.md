---
name: meristem-patch
description: Multi-file change that needs invariant context. Runs Orient → Decide → Act phases; spawns up to 3 sub-agents (implementer + cross-repo verifier + edge-case enumerator) when the diff is complex. Use this for any change beyond a trivial tweak.
---

# /meristem:patch — full OODAR Orient → Act

The workhorse skill. Use whenever the change is non-trivial:
- Touches multiple files
- Crosses repo boundaries (any multi-repo / sibling-repo workspace)
- Modifies schema, auth, or anything in `protected` from `meristem.toml`
- Has any chance of breaking an invariant

## Phase 1 — Orient (load-bearing, do not skip)

Query the substrate before editing. Use the file paths you're about to
touch as structural seeds:

```bash
meristem route "<user's request verbatim>" -f path/A -f path/B
```

The returned JSON has atoms with provenance trails. Read every CLOSED
decision and every invariant; cite them by atom_id in your reasoning.

If a returned atom **contradicts** the user's request: stop and ask
before proceeding.

## Phase 2 — Decide

Smallest change that achieves intent. If you find yourself adding
abstractions or future-proofing, you're out of scope — re-anchor on the
atoms you just retrieved.

## Phase 3 — Act (sub-agent spawn logic)

Spawn sub-agents when ANY of these hold:
- `repos_touched > 1`
- `schema_changed`
- `diff > 100 LOC`
- `cross_cutting_hits ≥ 3` (atoms surfaced from ≥3 different topic_keys)

Spawn shape (max 3):
- **A1 Implementer** — your primary repo only, writes the diff.
- **A2 Cross-repo verifier** — partitioned to OTHER repos. Reads MIRRORS-
  edged atoms via `meristem query "..." -f cross/path`; confirms or flags.
- **A3 Edge-case enumerator** — runs LAST, sees the staged diff + 2-hop
  neighborhood. Uses `meristem query` on touched symbols.

Spawn order: A1 → (A2 ‖ A3 in parallel after A1 stages). Partition by
repo so they don't collide on files.

Each sub-agent emits the SPEC §10 JSON contract — validate every
`evidence[]` entry resolves (file exists, line in range, atom_id in Meristem)
before surfacing.

## Phase 4 — Verify (handoff to /meristem:check)

After staging, invoke `/meristem:check` against the staged diff. Don't
self-verify.

## Phase 5 — Remember

For every non-obvious fact you discovered, write it back:
- New invariant → MCP `assert_fact(type=invariant, ...)`
- New decision → `assert_fact(type=decision, decision_status=CLOSED, ...)`
- Cross-app coupling → `assert_fact` + MCP `link_edge` (when wired) with
  kind=MIRRORS

## Output (mandatory)

```
✓ Patched: <N files>, +X -Y across <repos>
  Atoms consulted: <list of atom_ids>
  Atoms drafted: <list>
  Agents: implementer, cross-repo (M findings), edge-enum (N findings)

Edge cases to consider:
  • [high] <claim>  → <file:line>
  • [med]  <claim>  → <file:line>
  • [low]  <claim>  → <file:line>
  (+K more, run /meristem:check --deep)

Next: review staged diff, run /meristem:check before commit
```

If 0 edge cases survive validation, say so explicitly. Empty list is a
valid, honest output — do not fabricate.

## Halt safety

≥85% capacity → `meristem handoff --next "<verb+target>" --reason halt_safety`
and stop. Sub-agents must check capacity too; spawn parent halts the
whole tree.
