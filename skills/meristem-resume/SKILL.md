---
name: meristem-resume
description: Resume from the last handoff. Reads .meristem/handoffs/LATEST.md as authoritative context, restores the in-flight tick, and continues. Use at the start of a fresh session after a halt.
---

# /meristem:resume — read LATEST, restore state

## Steps

1. Read `.meristem/handoffs/LATEST.md`. The frontmatter is the source of
   truth — treat it as more authoritative than your own assumptions.
2. Emit a SessionStart-style preamble citing the handoff:
   *"Resuming session <session_id>. Tick: <title>. Phase: <phase>. Next:
   <next_action>."*
3. Apply `do_not_redo` as constraints. Do not re-attempt the listed
   approaches.
4. Apply `failed_assumptions` as a check — if you find yourself making
   one of these assumptions again, stop.
5. Resume from `next_action` directly. Do not re-Orient unless the
   handoff is >24h old.

## When to NOT resume

- LATEST.md is missing or unreadable → emit the normal SessionStart digest
  via `meristem digest` and proceed without a tick.
- LATEST.md is >24h old → treat as historical context, not authoritative.
  Use it for orientation but re-Orient via `/meristem:trace` first.
- `current_tick.status == "completed"` → the prior session finished
  cleanly; offer the user a choice of new tasks rather than continuing.

## Output

```
Resumed from <handoff path>:
  Tick: <id> — <title> (<phase>)
  Last commit: <sha>  Branch: <branch>  Dirty: <N files>
  Do-not-redo: <count items>
  Open questions: <count items>
Next: <next_action verbatim>
```

Then proceed with the next_action.
