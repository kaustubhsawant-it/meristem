---
name: meristem-handoff
description: Write a resumption handoff document. Use when context is approaching capacity, when finishing a clean tick, or when the user invokes /meristem:handoff manually. Pairs with /meristem:resume.
---

# /meristem:handoff — write the resumption doc

Triggers (any of):
- Context capacity ≥80% (PreCompact hook surfaces this)
- Skill exits cleanly (`tick_complete`)
- User invokes `/meristem:handoff`
- Halt-safety triggers (≥85% capacity)
- Stop hook with dirty files and no handoff this session

## What to capture

Run `meristem handoff` with the strongest possible `--next` value: a literal
verb + target, one sentence. *"Read SPEC.md, then implement Task #7"*
beats *"continue where I left off"*.

```bash
meristem handoff \
  --title "<current tick title>" \
  --next  "<literal verb+target>" \
  --reason "<user_handoff|halt_safety|tick_complete|capacity_pre_compact>"
```

## Narrative discipline

The narrative is hard-capped at ~400 tokens (sentence-boundary truncation).
Before invoking, draft the narrative in your head:
- Anything regenerable from `git log -p` → DO NOT include
- Atom bodies → cite IDs only
- Failed assumptions → include (so next session avoids the loop)
- Open questions the user hasn't answered → include

## Output

```
✓ Handoff written: .meristem/handoffs/<ts>-<sid>.md
  LATEST.md → <same file>
Next: <literal verb+target>
```

After writing, halt the session. Do not start new work.
