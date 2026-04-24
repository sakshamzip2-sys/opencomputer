# Example — workspace-escape guard as a Python plugin

A single `PreToolUse` hook that refuses Edit / MultiEdit / Write calls
whose `file_path` argument resolves outside the current workspace root.
Ships as a minimal plugin so it can be enabled / disabled per-profile.

## Directory layout

```
workspace-guard/
├── plugin.json
└── plugin.py
```

## `plugin.json`

```json
{
  "id": "workspace-guard",
  "name": "Workspace Guard",
  "version": "0.1.0",
  "description": "PreToolUse hook — refuse edits outside the current workspace.",
  "author": "Example",
  "license": "MIT",
  "kind": "mixed",
  "entry": "plugin"
}
```

`kind="mixed"` because a hook-only plugin registers no tools, providers,
or channels — the I.5 drift check warns about `kind="tool"` plugins
that register no tools, but `"mixed"` is the catch-all.

## `plugin.py`

```python
"""workspace-guard — block edits that escape the workspace root."""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

_WORKSPACE_ROOT = Path.cwd().resolve()
_GUARDED_TOOLS = "Edit|MultiEdit|Write|NotebookEdit"


async def _guard(ctx: HookContext) -> HookDecision | None:
    if ctx.tool_call is None:
        return None
    raw_path = ctx.tool_call.arguments.get("file_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None  # let the tool's own argument validation handle it

    try:
        resolved = Path(raw_path).resolve()
    except OSError as e:
        return HookDecision(
            decision="block",
            reason=f"{ctx.tool_call.name}: cannot resolve {raw_path!r}: {e}",
        )

    try:
        resolved.relative_to(_WORKSPACE_ROOT)
    except ValueError:
        return HookDecision(
            decision="block",
            reason=(
                f"{ctx.tool_call.name} refused: {resolved} is outside "
                f"the workspace root {_WORKSPACE_ROOT}."
            ),
        )
    return None  # pass — path is inside the workspace


def register(api) -> None:
    api.register_hook(
        HookSpec(
            event=HookEvent.PRE_TOOL_USE,
            handler=_guard,
            matcher=_GUARDED_TOOLS,
            fire_and_forget=False,
        )
    )
```

## How it fires

1. Agent loop receives a tool call from the model, e.g.
   `Edit(file_path="/etc/passwd", ...)`.
2. Before dispatch, `HookEngine.fire_blocking(ctx)` iterates every
   `HookSpec` registered for `HookEvent.PRE_TOOL_USE`.
3. The `matcher` regex `"Edit|MultiEdit|Write|NotebookEdit"` matches
   `"Edit"` — the handler runs.
4. `_guard` resolves the path, finds it's outside `_WORKSPACE_ROOT`,
   returns `HookDecision(decision="block", reason=...)`.
5. The loop short-circuits dispatch: the model sees a `ToolResult` with
   `is_error=True` whose content is the block reason. No filesystem
   write happens.

## Testing the hook

```python
# tests/test_workspace_guard.py
import asyncio
from pathlib import Path
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent

from workspace_guard.plugin import _guard  # depends on the plugin being on sys.path


def test_block_escapes_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from workspace_guard import plugin as guard_plugin
    guard_plugin._WORKSPACE_ROOT = tmp_path.resolve()

    ctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="test",
        tool_call=ToolCall(id="1", name="Edit", arguments={"file_path": "/etc/passwd"}),
    )
    decision = asyncio.run(_guard(ctx))
    assert decision is not None
    assert decision.decision == "block"
    assert "outside the workspace root" in decision.reason
```

Equivalent behavior can be declared in YAML without shipping a plugin —
see `settings-yaml-hook.md` for the settings-path rewrite.
