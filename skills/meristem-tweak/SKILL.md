---
name: meristem-tweak
description: Make a tiny change (≤2 files, no schema/auth) — typos, colors, copy. Single context, no sub-agents. Use when the user asks for a trivial edit that has no cross-cutting risk.
---

# /meristem:tweak — compressed OODAR cycle

Use this skill when the user asks for a **tiny** change:
- ≤2 files touched
- No schema, no auth, no cross-app boundaries
- Examples: rename a label, fix a typo, tweak a color, adjust copy

If the change might affect more than 2 files OR touches schema/auth/cross-app
state, **stop and route to `/meristem:patch` instead.**

## Compressed cycle

1. **Observe** — `git status`, identify target.
2. **Decide + Act** — make the edit directly. No KB query needed for trivial
   changes; if you find yourself second-guessing, abort and use `:patch`.
3. **Verify** — re-read the changed lines.
4. **Remember** — if you discovered something non-obvious (a hidden coupling,
   a comment that was wrong), capture it:
   ```bash
   meristem route "<short claim text>" -f <touched_file>
   ```
   then if the surfaced atoms don't already cover it, call the MCP
   `assert_fact` tool with `type=convention` or `glossary`.

## Output template (mandatory)

```
✓ Tweaked: <N files>, +X -Y
Atoms touched: <list or "none">
Next: <user-visible follow-up or "done">
```

No edge-case enumeration — that's `:check`'s job. If you find yourself
wanting to enumerate, abort and use `:patch`.

## Halt safety

At ≥85% context capacity, write a handoff (`meristem handoff --next "..."`)
and stop. Do not start a second `:tweak` after halting.
