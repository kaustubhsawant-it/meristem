---
name: meristem-plan
description: Orient/Decide-phase planning surface. Constrains a plan with graph facts (invariants, schema facts, closed decisions, owners) BEFORE any edit, and flags naive approaches that hit a CLOSED decision up front. Meristem supplies facts; execution stays in GSD.
---

# /meristem:plan — Orient/Decide-phase planning

Use when the user wants to plan a change rather than introspect (trace) or
patch. The substrate constrains the plan *before* any edit, so a blocked
approach surfaces now instead of mid-patch.

Trigger phrases:
- "plan how to add X"
- "what do I need to change for Y"
- "is there anything blocking Z"
- "what constraints apply to <feature>"

## Usage

```bash
meristem plan "<goal>" -f <optional file context>
```

`-f` narrows the seed set to atoms located in the given file(s); omit it for
a goal-only plan. The command emits the report below.

## Output (mandatory)

```
Plan for "<goal>":

  1. <task title>  [BLOCKED]
     - [schema_fact] <constraint summary>  (<provenance trail>)
     - [invariant] <constraint summary>  (<provenance trail>)
     owner: <summary>  (<trail>)
     ⚠ hits decision(s): <atom_id>
  2. <task title>
     - [invariant] <constraint summary>  (<trail>)

Constraints: <N> fact(s)
  • [invariant] <summary>
  • [decision] <summary>

Owners: <N>
  • <summary>  (<trail>)

  ⚠ approach hits CLOSED decision <id>: <summary> — do not plan around it silently

Next: feed these constraints to /meristem:patch or your GSD phase — do not edit
around a BLOCKED task silently.
```

Tasks are dependency-ordered: schema/migration constraints first, then
invariant-bearing edits, then everything else. Each task carries the facts it
must not violate, with their provenance trails. **Owners get their own
section** — a live `OWNS` edge into a surface is who to loop in, never a
constraint the approach must satisfy, so it is never counted among the
"Constraints: N fact(s)" tally or its `[type]`-labeled lines.

## Machine-readable output and the GSD consumer

`meristem plan "<goal>" --json` emits the same data as a JSON object —
`goal`, `top_relevance`, `tasks[]` (each with its own `constraints[]`,
`owners[]`, `blocked_by[]`), and top-level de-duped `constraints[]`,
`owners[]`, `blocking[]`. `tools/gsd_plan_reader.py` is the reference
consumer: a pure JSON→Markdown transform (no DB access) that turns a plan
into a fragment shaped to paste into a GSD `RESEARCH.md`/`PLAN.md`, with
Owners and Blocking decisions broken out as their own headings.

```bash
meristem plan "<goal>" --json | python tools/gsd_plan_reader.py >> RESEARCH.md
```

## Boundaries

- **Meristem supplies facts; it does NOT own execution.** The plan is constraint
  context — it feeds `/meristem:patch` and GSD phase/execution workflows. Don't
  treat the task list as the implementation order of record; treat it as the
  guardrails the implementation must respect.
- **If a CLOSED decision blocks the naive approach, surface it — do not plan
  around it silently.** A `[BLOCKED]` task or a `⚠ approach hits CLOSED
  decision` warning means the obvious path is forbidden; escalate or rethink,
  don't quietly route around the recorded decision.
- If 0 constraints surface, say so honestly: the plan is unconstrained and the
  substrate has no recorded knowledge for this goal — consider asserting
  invariants before proceeding.
- Persisting the plan (`--persist`) writes `decision`-tier atoms so "why did
  we choose this approach?" stays queryable; do this for non-trivial plans.

## Halt safety

Planning is mostly read-only (persist is an explicit opt-in), but still pause
at ≥85% capacity. A plan can resume in a fresh session via `/meristem:resume`.
