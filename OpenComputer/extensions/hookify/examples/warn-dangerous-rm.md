---
name: warn-dangerous-rm
enabled: true
event: bash
action: warn
pattern: 'rm\s+-rf'
---

You're about to run `rm -rf`. This is permanent and recursive.

Before executing:
- Confirm the path is what you think it is.
- Consider `git mv` / `git rm` instead if the target is tracked.
- Use `trash` (brew install trash) if you want a safety net on macOS.
