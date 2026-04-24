# Example — workspace-escape guard as a settings hook

Same behavior as `python-hook-plugin.md` — refuse edits outside the
workspace root — declared entirely in `config.yaml` with a tiny Python
snippet invoked as a shell command. No plugin needed.

## Where this lives

`~/.opencomputer/<profile>/config.yaml` under the top-level `hooks:`
block:

```yaml
hooks:
  PreToolUse:
    - matcher: "Edit|MultiEdit|Write|NotebookEdit"
      command: "python3 /usr/local/bin/oc-workspace-guard.py"
      timeout_seconds: 5
```

A single entry. The matcher restricts the hook to the four file-mutating
tools; everything else skips the subprocess spawn.

## The script — `/usr/local/bin/oc-workspace-guard.py`

```python
#!/usr/bin/env python3
"""OpenComputer settings hook — block edits outside the workspace."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    raw = sys.stdin.read()
    try:
        ctx = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        # Malformed input — fail-open (other-exit case).
        print("hook received malformed stdin", file=sys.stderr)
        return 1

    tool_input = ctx.get("tool_input") or {}
    tool_name = ctx.get("tool_name") or "?"
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return 0  # nothing to check — pass

    workspace = Path.cwd().resolve()
    try:
        resolved = Path(file_path).resolve()
        resolved.relative_to(workspace)
    except (OSError, ValueError):
        print(
            f"{tool_name}: {file_path!r} is outside workspace {workspace}",
            file=sys.stderr,
        )
        return 2  # blocks; stderr becomes the reason

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Make it executable:

```bash
chmod +x /usr/local/bin/oc-workspace-guard.py
```

## What happens on each tool call

1. Agent loop receives `Edit(file_path="/etc/passwd", ...)`.
2. Core iterates `PreToolUse` hooks — the settings-YAML hook matches.
3. `make_shell_hook_handler` spawns `python3 /usr/local/bin/oc-
   workspace-guard.py` with the `HookContext` JSON piped to stdin and
   the `OPENCOMPUTER_*` env vars set.
4. The script parses the JSON, resolves `/etc/passwd`, finds it's
   outside the workspace, prints a reason to stderr, and exits 2.
5. The hook handler returns `HookDecision(decision="block",
   reason=<stderr>)`. The tool never runs.

Python-plugin path and settings-YAML path produce identical behavior
from the model's perspective — a blocked `ToolResult` with the reason
surfaced.

## When to pick which path

Choose the **Python plugin** path when:
- The logic needs tight OpenComputer integration (reading
  `ctx.runtime`, walking `ctx.message`, mutating session state).
- The plugin ships additional surface (tools, providers, injection)
  that goes with the hook.
- You want versioned releases, tests, and dependency isolation.

Choose the **settings-YAML** path when:
- The user (not the plugin author) owns the policy.
- You want to drop in existing Claude-Code-compatible hook scripts
  unchanged (the `CLAUDE_PLUGIN_ROOT` env alias makes this work).
- The hook is a simple decision over the JSON blob — no need for
  OpenComputer-native types.

Both paths coexist. Plugin-declared hooks fire first, settings hooks
after. Multiple entries for the same event fire in declaration order;
the first blocking decision wins.

## Debugging

Useful flags while iterating:

```bash
# Dry-run the script against a fake hook payload.
echo '{"tool_name":"Edit","tool_input":{"file_path":"/etc/passwd"}}' \
  | python3 /usr/local/bin/oc-workspace-guard.py; echo "rc=$?"

# Watch OpenComputer's hook-engine log.
OPENCOMPUTER_LOG=DEBUG opencomputer
```

A script that exits non-zero for any reason other than 2 fails open
(the tool runs anyway). Check the WARNING-level log line for the exit
code if a hook silently "stops working".
