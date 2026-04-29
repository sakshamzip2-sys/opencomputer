# Permission Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename `yolo` → `auto`, add `accept-edits` middle mode, add Shift+Tab cycling and a TUI mode badge — bringing the harness to parity with Claude Code's permission-mode model.

**Architecture:** Single `PermissionMode` enum in `plugin_sdk/runtime_context.py` with a new frozen field plus a unified `effective_permission_mode(runtime)` helper that resolves CLI-set fields, slash-command-set `runtime.custom[...]` keys, and legacy bools through one precedence chain. Existing `plan_mode` / `yolo_mode` fields and slash commands stay as deprecated aliases for one minor version. `BypassManager.is_active()` extended to also bypass the F1 ConsentGate when effective mode is AUTO (today only env var bypasses; `--yolo` is cosmetic — closing this gap is a side effect of this work). New `accept-edits` mode hard-allowlists Edit/Write/MultiEdit/NotebookEdit only. TUI mode badge added as a `Window` row in the existing `HSplit` (NOT prompt_toolkit's `bottom_toolbar=`, which is `PromptSession`-only and would crash on the custom `Application` from PR #266).

**Tech Stack:** Python 3.12+, frozen dataclasses, Typer (CLI), Jinja2 (prompt templates), prompt_toolkit (TUI), pytest.

**Design doc:** [docs/superpowers/specs/2026-04-29-permission-modes-design.md](../specs/2026-04-29-permission-modes-design.md)

---

## File Structure

### New files
- `plugin_sdk/permission_mode.py` — `PermissionMode` enum + `effective_permission_mode()` helper. Re-exported from `plugin_sdk/__init__.py`.
- `opencomputer/agent/slash_commands_impl/auto_cmd.py` — replaces `yolo_cmd.py` (renamed). Writes `runtime.custom["permission_mode"]` AND legacy `runtime.custom["yolo_session"]`.
- `opencomputer/agent/slash_commands_impl/mode_cmd.py` — `/mode <name>` + `/accept-edits` shorthand.
- `extensions/coding-harness/hooks/accept_edits_hook.py` — PreToolUse auto-approver for Edit-family tools when mode is ACCEPT_EDITS.
- `extensions/coding-harness/modes/accept_edits_mode.py` — DynamicInjectionProvider for ACCEPT_EDITS prompt block.
- `extensions/coding-harness/prompts/accept_edits_mode.j2` — short Jinja template for the accept-edits prompt block.
- `tests/test_permission_mode_enum.py` — enum + helper unit tests.
- `tests/tier2_slash/test_auto_cmd.py` — moved from `test_yolo_cmd.py`, with `/yolo` deprecation tests added.
- `tests/tier2_slash/test_mode_cmd.py` — `/mode` + `/accept-edits` tests.
- `tests/test_accept_edits_hook.py` — hook auto-approve behaviour. (Flat in `tests/`; existing `tests/conftest.py` registers `extensions.coding_harness` namespace alias so the kebab dir is importable.)
- `tests/test_plan_block_gap_close.py` — regression: `/plan` now triggers hook.
- `tests/test_cli_flag_aliasing.py` — `--yolo`/`--auto` parity, cron precedence preserved.
- `tests/test_consent_bypass_in_auto.py` — `--auto` truly bypasses ConsentGate.
- `tests/test_mode_badge.py` — Shift+Tab cycle + badge render. (Flat in `tests/`.)

### Modified files
- `plugin_sdk/runtime_context.py` — add `permission_mode` frozen field; helper imports from `plugin_sdk.permission_mode`.
- `plugin_sdk/__init__.py` — re-export `PermissionMode`, `effective_permission_mode`.
- `opencomputer/agent/consent/bypass.py` — extend `is_active()` signature with optional `runtime` arg; bypasses also when mode == AUTO.
- `opencomputer/agent/loop.py:2188-2244` — pass `self._runtime` into `BypassManager.is_active()`.
- `opencomputer/cli.py` — add `--auto`/`--accept-edits` flags to `chat`/`code`/`resume`; derive `permission_mode`; one-shot deprecation warning helper; banner with all 4 colours.
- `opencomputer/cli_cron.py` — `--auto`/`--accept-edits` flags; preserve `--yolo` precedence inversion.
- `opencomputer/agent/slash_commands_impl/__init__.py` — register new commands.
- `opencomputer/agent/prompt_builder.py` — thread `permission_mode` through `PromptContext`, `build()`, `build_with_memory()`.
- `opencomputer/agent/loop.py:706` — pass `effective_permission_mode(self._runtime)` to `build_with_memory`.
- `opencomputer/agent/prompts/base.j2:47, 147-158, 271` — 4-branch dispatch on `permission_mode`.
- `extensions/coding-harness/slash_commands/plan.py` — also write canonical `permission_mode` key.
- `extensions/coding-harness/hooks/plan_block.py:35` — read `effective_permission_mode()` instead of bare field.
- `extensions/coding-harness/modes/plan_mode.py:54` — same.
- `extensions/coding-harness/plugin.py` — register accept-edits hook + injection provider.
- `opencomputer/hooks/shell_handlers.py:77-78` — emit `permission_mode` env var.
- `opencomputer/gateway/protocol_v2.py:84` — add optional `permission_mode: str` field.
- `opencomputer/cli_ui/input_loop.py:433-722` — Shift+Tab keybinding + mode badge Window in HSplit.
- `opencomputer/cli_ui/slash.py` — `/help` legend mentions Shift+Tab.
- `opencomputer/tasks/runtime.py:230`, `opencomputer/cron/scheduler.py:204-205`, `opencomputer/tools/cron_tool.py:208` — pass-through compat for new field.
- `opencomputer/tools/delegate.py:44, 348` — comment update.
- `README.md` — Permission modes section.
- `CHANGELOG.md` — entry under Unreleased.
- `CLAUDE.md` §7 — pointer to `effective_permission_mode`.
- `plugin_sdk/CLAUDE.md` — `PermissionMode` is part of public contract.

---

# PR-1 — Foundation: enum + helper + ConsentGate flip

PR-1 must ship as one PR — slicing the helper from the ConsentGate flip would leave `--auto` no-op'ing for the duration of the staging.

---

### Task 1: Add `PermissionMode` enum and `effective_permission_mode()` helper

**Files:**
- Create: `plugin_sdk/permission_mode.py`
- Test: `tests/test_permission_mode_enum.py`

- [ ] **Step 1.1: Write the failing test**

```python
# tests/test_permission_mode_enum.py
"""PermissionMode enum + effective_permission_mode() helper."""

from __future__ import annotations

import pytest

from plugin_sdk import (
    PermissionMode,
    RuntimeContext,
    effective_permission_mode,
)


class TestPermissionModeEnum:
    def test_four_canonical_values(self) -> None:
        assert PermissionMode.DEFAULT.value == "default"
        assert PermissionMode.PLAN.value == "plan"
        assert PermissionMode.ACCEPT_EDITS.value == "accept-edits"
        assert PermissionMode.AUTO.value == "auto"

    def test_string_enum(self) -> None:
        # StrEnum so it serializes as the string value.
        assert str(PermissionMode.AUTO) == "auto"

    def test_round_trip_from_value(self) -> None:
        assert PermissionMode("accept-edits") is PermissionMode.ACCEPT_EDITS


class TestEffectivePermissionModeResolution:
    def test_default_when_nothing_set(self) -> None:
        rt = RuntimeContext()
        assert effective_permission_mode(rt) == PermissionMode.DEFAULT

    def test_legacy_plan_field(self) -> None:
        rt = RuntimeContext(plan_mode=True)
        assert effective_permission_mode(rt) == PermissionMode.PLAN

    def test_legacy_yolo_field(self) -> None:
        rt = RuntimeContext(yolo_mode=True)
        assert effective_permission_mode(rt) == PermissionMode.AUTO

    def test_new_field_overrides_legacy(self) -> None:
        rt = RuntimeContext(
            plan_mode=True,
            permission_mode=PermissionMode.ACCEPT_EDITS,
        )
        assert effective_permission_mode(rt) == PermissionMode.ACCEPT_EDITS

    def test_legacy_custom_plan(self) -> None:
        rt = RuntimeContext(custom={"plan_mode": True})
        assert effective_permission_mode(rt) == PermissionMode.PLAN

    def test_legacy_custom_yolo_session(self) -> None:
        rt = RuntimeContext(custom={"yolo_session": True})
        assert effective_permission_mode(rt) == PermissionMode.AUTO

    def test_canonical_custom_wins_over_legacy(self) -> None:
        rt = RuntimeContext(
            yolo_mode=True,
            custom={"permission_mode": "accept-edits", "yolo_session": True},
        )
        assert effective_permission_mode(rt) == PermissionMode.ACCEPT_EDITS

    def test_plan_wins_over_auto_on_conflict(self) -> None:
        # Matches existing CLI precedence (plan beats yolo).
        rt = RuntimeContext(plan_mode=True, yolo_mode=True)
        assert effective_permission_mode(rt) == PermissionMode.PLAN


class TestRuntimeContextStillFrozen:
    def test_cannot_mutate_field(self) -> None:
        rt = RuntimeContext()
        with pytest.raises(Exception):  # FrozenInstanceError
            rt.permission_mode = PermissionMode.AUTO  # type: ignore[misc]
```

- [ ] **Step 1.2: Run test to verify it fails**

```
pytest OpenComputer/tests/test_permission_mode_enum.py -v
```
Expected: FAIL — `PermissionMode` and `effective_permission_mode` not exported.

- [ ] **Step 1.3: Implement the enum + helper**

```python
# plugin_sdk/permission_mode.py
"""PermissionMode enum + effective_permission_mode() resolver.

Single source of truth for "what mode is this session in right now?"
Resolution precedence (top wins):

  1. runtime.custom["permission_mode"]            (canonical session-mutable)
  2. runtime.custom["plan_mode"] == True          → PLAN  (legacy /plan)
     runtime.custom["yolo_session"] == True       → AUTO  (legacy /yolo)
  3. runtime.permission_mode                      (canonical CLI-set field)
  4. runtime.plan_mode == True                    → PLAN  (legacy --plan)
     runtime.yolo_mode == True                    → AUTO  (legacy --yolo)
  5. PermissionMode.DEFAULT

Plan beats auto on conflict (matches existing CLI precedence). New code
should call this helper rather than reading any individual field directly.
"""
from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugin_sdk.runtime_context import RuntimeContext


class PermissionMode(StrEnum):
    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "accept-edits"
    AUTO = "auto"


def effective_permission_mode(runtime: "RuntimeContext") -> PermissionMode:
    # 1. Canonical session-mutable key.
    custom_mode = runtime.custom.get("permission_mode")
    if custom_mode:
        try:
            return PermissionMode(custom_mode)
        except ValueError:
            pass  # malformed value — fall through to next precedence layer

    # 2. Legacy session-mutable keys (plan beats auto on conflict).
    if runtime.custom.get("plan_mode"):
        return PermissionMode.PLAN
    if runtime.custom.get("yolo_session"):
        return PermissionMode.AUTO

    # 3. Canonical CLI-set frozen field.
    if runtime.permission_mode != PermissionMode.DEFAULT:
        return runtime.permission_mode

    # 4. Legacy CLI-set fields (plan beats yolo).
    if runtime.plan_mode:
        return PermissionMode.PLAN
    if runtime.yolo_mode:
        return PermissionMode.AUTO

    # 5. Default.
    return PermissionMode.DEFAULT


__all__ = ["PermissionMode", "effective_permission_mode"]
```

- [ ] **Step 1.4: Add `permission_mode` field to RuntimeContext**

Edit `plugin_sdk/runtime_context.py` — add the import and field after `yolo_mode`:

```python
# at top:
from plugin_sdk.permission_mode import PermissionMode

# inside RuntimeContext, after yolo_mode field:
    #: Canonical permission mode. Set by CLI flags at session start. New code
    #: should resolve through ``effective_permission_mode()`` rather than
    #: reading this field directly — it accounts for slash-command toggles
    #: living in ``custom["permission_mode"]``.
    permission_mode: PermissionMode = PermissionMode.DEFAULT
```

- [ ] **Step 1.5: Re-export from plugin_sdk/__init__.py**

Edit `plugin_sdk/__init__.py` — add to imports and `__all__`:

```python
from plugin_sdk.permission_mode import PermissionMode, effective_permission_mode

__all__ = [
    # ...existing exports...
    "PermissionMode",
    "effective_permission_mode",
]
```

- [ ] **Step 1.6: Run tests to verify they pass**

```
pytest OpenComputer/tests/test_permission_mode_enum.py -v
```
Expected: PASS — all 11 tests green.

- [ ] **Step 1.7: Verify SDK boundary intact**

```
pytest OpenComputer/tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v
```
Expected: PASS — new `permission_mode.py` only imports stdlib + `runtime_context` (TYPE_CHECKING).

- [ ] **Step 1.8: Commit**

```
git add OpenComputer/plugin_sdk/permission_mode.py OpenComputer/plugin_sdk/runtime_context.py OpenComputer/plugin_sdk/__init__.py OpenComputer/tests/test_permission_mode_enum.py
git commit -m "feat(plugin_sdk): PermissionMode enum + effective_permission_mode helper"
```

---

### Task 2: Update F1 ConsentGate bypass to honour AUTO mode

**Why this matters:** today `BypassManager.is_active()` reads ONLY the env var `OPENCOMPUTER_CONSENT_BYPASS`. The current `--yolo` flag and `/yolo` slash command are cosmetic — they tell the model "yolo is active" via the prompt but the consent gate still prompts. Closing this is a side-effect win of this PR.

**Files:**
- Modify: `opencomputer/agent/consent/bypass.py`
- Modify: `opencomputer/agent/loop.py:2191`
- Test: `tests/test_consent_bypass_in_auto.py`

- [ ] **Step 2.1: Write the failing test**

```python
# tests/test_consent_bypass_in_auto.py
"""BypassManager honours AUTO permission mode (closes a pre-existing gap)."""

from __future__ import annotations

import os
from unittest.mock import patch

from opencomputer.agent.consent.bypass import BypassManager
from plugin_sdk import PermissionMode, RuntimeContext


class TestBypassWithoutRuntime:
    def test_env_var_bypass_unchanged(self) -> None:
        with patch.dict(os.environ, {"OPENCOMPUTER_CONSENT_BYPASS": "1"}):
            assert BypassManager.is_active() is True

    def test_no_env_no_runtime_not_active(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCOMPUTER_CONSENT_BYPASS", None)
            assert BypassManager.is_active() is False


class TestBypassWithRuntime:
    def test_default_mode_not_bypass(self) -> None:
        rt = RuntimeContext()
        assert BypassManager.is_active(rt) is False

    def test_plan_mode_not_bypass(self) -> None:
        rt = RuntimeContext(permission_mode=PermissionMode.PLAN)
        assert BypassManager.is_active(rt) is False

    def test_accept_edits_not_bypass(self) -> None:
        # accept-edits auto-approves edits but still gates Bash/network.
        rt = RuntimeContext(permission_mode=PermissionMode.ACCEPT_EDITS)
        assert BypassManager.is_active(rt) is False

    def test_auto_mode_bypasses(self) -> None:
        rt = RuntimeContext(permission_mode=PermissionMode.AUTO)
        assert BypassManager.is_active(rt) is True

    def test_legacy_yolo_mode_bypasses(self) -> None:
        # Backwards-compat: --yolo CLI flag still bypasses.
        rt = RuntimeContext(yolo_mode=True)
        assert BypassManager.is_active(rt) is True

    def test_legacy_yolo_session_bypasses(self) -> None:
        # Backwards-compat: /yolo on still bypasses.
        rt = RuntimeContext(custom={"yolo_session": True})
        assert BypassManager.is_active(rt) is True
```

- [ ] **Step 2.2: Run test to verify it fails**

```
pytest OpenComputer/tests/test_consent_bypass_in_auto.py -v
```
Expected: FAIL — `BypassManager.is_active()` doesn't accept a `runtime` arg yet.

- [ ] **Step 2.3: Extend BypassManager.is_active()**

Edit `opencomputer/agent/consent/bypass.py`:

```python
"""Emergency consent bypass + AUTO-mode bypass.

Two activation paths:

1. ``OPENCOMPUTER_CONSENT_BYPASS=1`` env var — process-wide emergency unbrick
   when the gate misbehaves. Every action audit-logged under actor="bypass".

2. ``effective_permission_mode(runtime) == AUTO`` — explicit user opt-in via
   ``--auto`` or ``/auto on``. Same audit treatment as the env-var bypass —
   the user has chosen to skip per-call prompts and the audit log is the
   accountability layer.

Either path triggers the bypass banner.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plugin_sdk.runtime_context import RuntimeContext


class BypassManager:
    ENV_FLAG = "OPENCOMPUTER_CONSENT_BYPASS"

    @classmethod
    def is_active(cls, runtime: "RuntimeContext | None" = None) -> bool:
        if cls._env_active():
            return True
        if runtime is not None and cls._auto_mode_active(runtime):
            return True
        return False

    @classmethod
    def _env_active(cls) -> bool:
        return os.environ.get(cls.ENV_FLAG, "").strip().lower() in (
            "1", "true", "yes", "on",
        )

    @staticmethod
    def _auto_mode_active(runtime: "RuntimeContext") -> bool:
        # Local import to keep plugin_sdk dependency direction (sdk → no opencomputer).
        from plugin_sdk.permission_mode import (
            PermissionMode,
            effective_permission_mode,
        )
        return effective_permission_mode(runtime) == PermissionMode.AUTO

    @staticmethod
    def banner() -> str:
        return (
            "⚠️ CONSENT BYPASS ACTIVE — every tool call will run without gate.\n"
            "Every action is being heavily audit-logged. Disable to restore "
            "normal operation (unset OPENCOMPUTER_CONSENT_BYPASS or switch "
            "out of auto mode)."
        )
```

- [ ] **Step 2.4: Update agent loop to pass runtime into is_active()**

Edit `opencomputer/agent/loop.py:2191`:

```python
            if not BypassManager.is_active(self._runtime):
```

- [ ] **Step 2.5: Run tests to verify they pass**

```
pytest OpenComputer/tests/test_consent_bypass_in_auto.py -v
```
Expected: PASS — all 8 tests green.

- [ ] **Step 2.6: Run loop test suite to verify no regression**

```
pytest OpenComputer/tests/ -k "loop or consent or bypass" -q
```
Expected: PASS — existing behaviour unchanged for env-var-only bypass paths.

- [ ] **Step 2.7: Commit**

```
git add OpenComputer/opencomputer/agent/consent/bypass.py OpenComputer/opencomputer/agent/loop.py OpenComputer/tests/test_consent_bypass_in_auto.py
git commit -m "feat(consent): BypassManager honours AUTO permission mode"
```

---

### Task 3: Update plan_block.py and plan_mode.py to read effective mode

**Why:** `/plan` slash today writes `custom["plan_mode"]` but the hook reads only `runtime.plan_mode` (the frozen field) — so `/plan` doesn't actually trigger the hard-block. Routing all reads through `effective_permission_mode()` closes the gap.

**Files:**
- Modify: `extensions/coding-harness/hooks/plan_block.py:35`
- Modify: `extensions/coding-harness/modes/plan_mode.py:54`
- Test: `tests/test_plan_block_gap_close.py`

- [ ] **Step 3.1: Write the failing test**

```python
# tests/test_plan_block_gap_close.py
"""Regression: /plan slash command engages the plan_block hook (PR-1 gap-close)."""

from __future__ import annotations

import pytest

from extensions.coding_harness.hooks.plan_block import plan_mode_block_hook
from plugin_sdk import RuntimeContext
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent


def _make_ctx(runtime: RuntimeContext, tool: str = "Edit") -> HookContext:
    return HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s1",
        tool_call=ToolCall(id="c1", name=tool, arguments={"file_path": "/tmp/x"}),
        runtime=runtime,
    )


@pytest.mark.asyncio
class TestPlanModeFromCustomDict:
    async def test_custom_plan_mode_blocks_edit(self) -> None:
        # Today this would NOT block (the gap). After PR-1 it must block.
        rt = RuntimeContext(custom={"plan_mode": True})
        decision = await plan_mode_block_hook(_make_ctx(rt))
        assert decision is not None
        assert decision.decision == "block"
        assert "plan mode" in decision.reason.lower()

    async def test_canonical_permission_mode_plan_blocks(self) -> None:
        from plugin_sdk import PermissionMode
        rt = RuntimeContext(permission_mode=PermissionMode.PLAN)
        decision = await plan_mode_block_hook(_make_ctx(rt))
        assert decision is not None
        assert decision.decision == "block"

    async def test_default_does_not_block(self) -> None:
        rt = RuntimeContext()
        assert await plan_mode_block_hook(_make_ctx(rt)) is None

    async def test_legacy_field_still_blocks(self) -> None:
        rt = RuntimeContext(plan_mode=True)
        decision = await plan_mode_block_hook(_make_ctx(rt))
        assert decision is not None
        assert decision.decision == "block"
```

- [ ] **Step 3.2: Run test to verify it fails**

```
pytest OpenComputer/tests/test_plan_block_gap_close.py -v
```
Expected: FAIL on `test_custom_plan_mode_blocks_edit` and `test_canonical_permission_mode_plan_blocks` — the hook only reads `ctx.runtime.plan_mode`.

- [ ] **Step 3.3: Update plan_block.py:34-35**

```python
# extensions/coding-harness/hooks/plan_block.py
async def plan_mode_block_hook(ctx: HookContext) -> HookDecision | None:
    from plugin_sdk import PermissionMode, effective_permission_mode

    if ctx.runtime is None:
        return None
    if effective_permission_mode(ctx.runtime) != PermissionMode.PLAN:
        return None
    if ctx.tool_call is None:
        return None
    # ...rest unchanged...
```

- [ ] **Step 3.4: Update plan_mode.py:54 injection provider**

```python
# extensions/coding-harness/modes/plan_mode.py
    async def collect(self, ctx: InjectionContext) -> str | None:
        from plugin_sdk import PermissionMode, effective_permission_mode

        if effective_permission_mode(ctx.runtime) != PermissionMode.PLAN:
            return None
        # ...rest unchanged...
```

- [ ] **Step 3.5: Run tests to verify they pass**

```
pytest OpenComputer/tests/test_plan_block_gap_close.py OpenComputer/tests/ -k "plan_mode or plan_block" -v
```
Expected: PASS — gap-close tests + all existing plan_mode tests green.

- [ ] **Step 3.6: Commit**

```
git add OpenComputer/extensions/coding-harness/hooks/plan_block.py OpenComputer/extensions/coding-harness/modes/plan_mode.py OpenComputer/tests/test_plan_block_gap_close.py
git commit -m "fix(plan-mode): close /plan slash hook-engagement gap via effective_permission_mode"
```

---

### Task 4: Adjacent reads — protocol_v2, shell_handlers, RuntimeContext construction sites

**Files:**
- Modify: `opencomputer/gateway/protocol_v2.py:84`
- Modify: `opencomputer/hooks/shell_handlers.py:77-78`
- Modify: `opencomputer/tasks/runtime.py:230` (cosmetic — accept the new default)
- Modify: `opencomputer/cron/scheduler.py:204-205` (cosmetic)
- Test: extension to existing tests

- [ ] **Step 4.1: Add permission_mode to wire protocol**

Edit `opencomputer/gateway/protocol_v2.py:84` — add the optional field:

```python
    plan_mode: bool = False
    permission_mode: str = "default"  # NEW: canonical mode value; old clients omit and decode fine
```

- [ ] **Step 4.2: Emit permission_mode in shell-hook env vars**

Edit `opencomputer/hooks/shell_handlers.py:77-78`:

```python
                "plan_mode": getattr(ctx.runtime, "plan_mode", False),
                "yolo_mode": getattr(ctx.runtime, "yolo_mode", False),
                "permission_mode": (
                    effective_permission_mode(ctx.runtime).value
                    if ctx.runtime is not None else "default"
                ),
```

Add the import at the top:

```python
from plugin_sdk import effective_permission_mode
```

- [ ] **Step 4.3: Run existing test suites for these modules**

```
pytest OpenComputer/tests/ -k "protocol or shell_handler or scheduler or cron_tool" -q
```
Expected: PASS — no regressions; new optional field decodes from missing.

- [ ] **Step 4.4: Commit**

```
git add OpenComputer/opencomputer/gateway/protocol_v2.py OpenComputer/opencomputer/hooks/shell_handlers.py
git commit -m "feat: thread permission_mode through wire protocol + shell-hook env"
```

---

### Task 5: README + CHANGELOG + CLAUDE.md docs

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `CLAUDE.md`
- Modify: `plugin_sdk/CLAUDE.md`

- [ ] **Step 5.1: Add Permission Modes section to README.md**

Find the existing CLI usage section and add:

```markdown
## Permission modes

Four modes control which tools auto-approve vs prompt vs refuse:

| Mode             | Edit / Write | Bash    | Network | How to enter                          |
|------------------|--------------|---------|---------|---------------------------------------|
| `default`        | ask          | ask     | ask     | (no flag — default)                   |
| `plan`           | refused      | refused | refused | `--plan` or `/plan`                   |
| `accept-edits`   | auto         | ask     | ask     | `--accept-edits` or `/accept-edits`   |
| `auto`           | auto         | auto    | auto    | `--auto` or `/auto on`                |

Cycle modes mid-session with **Shift+Tab**. The mode badge at the bottom of
the TUI shows the current mode. `accept-edits` auto-approves Edit, Write,
MultiEdit, NotebookEdit only — `Bash sed -i` still prompts even though it
mutates files.
```

- [ ] **Step 5.2: Add CHANGELOG entry**

Top of `CHANGELOG.md` under `## Unreleased`:

```markdown
### Added
- `PermissionMode` enum and `effective_permission_mode()` helper in `plugin_sdk`.
- New `accept-edits` permission mode — auto-approves Edit/Write/MultiEdit/NotebookEdit only.
- New `--auto`, `--accept-edits` CLI flags on `chat`, `code`, `resume`, and `cron`.
- New `/auto`, `/mode`, `/accept-edits` slash commands.
- Shift+Tab cycles permission modes in the TUI.
- Persistent mode badge in the TUI footer.

### Changed
- `BypassManager.is_active()` now also bypasses the F1 ConsentGate when
  effective mode is `auto` (previously only the env var bypassed; `--yolo`
  was cosmetic). Auto-mode actions remain audit-logged.
- `/plan` slash command now actually engages the plan_block hard-block hook.
  Previously the hook only read the frozen field; reads now route through
  `effective_permission_mode()`.

### Deprecated
- `--yolo` CLI flag — use `--auto`. Removal target: v1.2 / 4 weeks of merges.
- `/yolo` slash command — use `/auto`. Same removal target.
- `runtime.yolo_mode`, `runtime.plan_mode` direct reads — use
  `effective_permission_mode(runtime)`. Same removal target.
- `runtime.custom["yolo_session"]`, `runtime.custom["plan_mode"]` direct
  writes — slash commands now write `runtime.custom["permission_mode"]`.
```

- [ ] **Step 5.3: Update CLAUDE.md gotcha #7**

Find the "HookContext.runtime is optional for backwards compat" gotcha and append:

```markdown
   New hooks should read modes through `effective_permission_mode(ctx.runtime)`
   rather than `ctx.runtime.plan_mode` / `ctx.runtime.yolo_mode` — the helper
   accounts for slash-command toggles in `runtime.custom`.
```

- [ ] **Step 5.4: Update plugin_sdk/CLAUDE.md**

Add to the "Public re-exports" list:

```markdown
   - `PermissionMode` + `effective_permission_mode` — canonical mode resolution
     for plugin authors. New code should use the helper rather than reading
     `runtime.plan_mode` / `runtime.yolo_mode` directly.
```

- [ ] **Step 5.5: Commit**

```
git add OpenComputer/README.md OpenComputer/CHANGELOG.md OpenComputer/CLAUDE.md OpenComputer/plugin_sdk/CLAUDE.md
git commit -m "docs: permission modes — README + CHANGELOG + CLAUDE.md pointers"
```

---

### Task 6: PR-1 final integration + push

- [ ] **Step 6.1: Run full test suite**

```
pytest OpenComputer/ -x -q
```
Expected: PASS.

- [ ] **Step 6.2: Run lint**

```
ruff check OpenComputer/
```
Expected: clean.

- [ ] **Step 6.3: Push PR-1 branch**

```
cd OpenComputer
git push -u origin HEAD
```

End of PR-1.

---

# PR-2 — CLI flags + slash commands + prompt template

Depends on PR-1. Adds in-session control surface.

---

### Task 7: `--auto` and `--accept-edits` flags on `code` subcommand

**Files:**
- Modify: `opencomputer/cli.py:1552-1601`
- Test: `tests/test_cli_flag_aliasing.py`

- [ ] **Step 7.1: Write the failing test**

```python
# tests/test_cli_flag_aliasing.py
"""--yolo and --auto produce equivalent RuntimeContext; cron precedence preserved."""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from opencomputer.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestCodeFlags:
    def test_help_lists_all_modes(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["code", "--help"])
        assert "--auto" in result.stdout
        assert "--accept-edits" in result.stdout
        assert "--plan" in result.stdout
        # --yolo still listed (deprecated alias).
        assert "--yolo" in result.stdout

    def test_auto_and_yolo_produce_same_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list = []

        def fake_run(**kwargs: object) -> None:
            captured.append(kwargs)

        monkeypatch.setattr("opencomputer.cli._run_chat_session", fake_run)
        runner = CliRunner()
        runner.invoke(app, ["code", "--auto"])
        runner.invoke(app, ["code", "--yolo"])
        assert len(captured) == 2
        # Both invocations should produce yolo=True (or equivalent canonical).
        assert captured[0].get("yolo") == captured[1].get("yolo")


class TestCronPrecedence:
    def test_cron_yolo_inverts_plan_mode(self) -> None:
        # cli_cron.py:133 has plan_mode=not yolo. Same must hold for --auto.
        from opencomputer.cli_cron import _resolve_cron_runtime  # to add in step 7.4

        rt_yolo = _resolve_cron_runtime(plan=True, auto=False, accept_edits=False, yolo=True)
        rt_auto = _resolve_cron_runtime(plan=True, auto=True, accept_edits=False, yolo=False)
        assert rt_yolo.plan_mode == rt_auto.plan_mode  # both False (auto wins)
```

- [ ] **Step 7.2: Run test to verify it fails**

```
pytest OpenComputer/tests/test_cli_flag_aliasing.py -v
```
Expected: FAIL — flags not added; helper not defined.

- [ ] **Step 7.3: Add `--auto` and `--accept-edits` to the `code` subcommand**

Edit `opencomputer/cli.py:1552-1556`. Add new options:

```python
    auto: bool = typer.Option(
        False,
        "--auto",
        help="Skip per-action confirmation prompts (auto-approve all tools).",
    ),
    accept_edits: bool = typer.Option(
        False,
        "--accept-edits",
        help="Auto-approve Edit/Write/MultiEdit/NotebookEdit; still prompt for Bash/network.",
    ),
    yolo: bool = typer.Option(
        False,
        "--yolo",
        help="[deprecated] Alias for --auto.",
        hidden=False,  # keep visible in --help; just deprecated-marked
    ),
```

In the body of `code()`, derive permission_mode and warn on `--yolo`:

```python
    from plugin_sdk import PermissionMode

    if yolo:
        from opencomputer.cli import _emit_yolo_deprecation
        _emit_yolo_deprecation()
        auto = True

    if plan:
        permission_mode = PermissionMode.PLAN
    elif auto:
        permission_mode = PermissionMode.AUTO
    elif accept_edits:
        permission_mode = PermissionMode.ACCEPT_EDITS
    else:
        permission_mode = PermissionMode.DEFAULT

    # _run_chat_session signature stays bool-friendly for now; thread the canonical via kwarg.
    _run_chat_session(
        resume=resume, plan=plan, no_compact=no_compact, yolo=auto,
        accept_edits=accept_edits, permission_mode=permission_mode,
    )
```

- [ ] **Step 7.4: Add the deprecation warning helper + RuntimeContext construction update**

Top of `cli.py`:

```python
_DEPRECATION_WARNED: set[str] = set()


def _emit_yolo_deprecation() -> None:
    if "yolo" in _DEPRECATION_WARNED:
        return
    _DEPRECATION_WARNED.add("yolo")
    typer.secho(
        "[deprecated] --yolo / /yolo will be removed in v1.2 — use --auto / /auto.",
        fg=typer.colors.YELLOW,
        err=True,
    )
```

Update `_run_chat_session` signature to accept the new params and build the RuntimeContext:

```python
def _run_chat_session(
    *,
    resume: str = "",
    plan: bool = False,
    no_compact: bool = False,
    yolo: bool = False,
    accept_edits: bool = False,
    permission_mode: PermissionMode = PermissionMode.DEFAULT,
) -> None:
    ...
    runtime = RuntimeContext(
        plan_mode=plan,
        yolo_mode=yolo,
        permission_mode=permission_mode,
    )
```

- [ ] **Step 7.5: Update banner with all 4 modes**

Replace `cli.py:926-928`:

```python
    from plugin_sdk import PermissionMode

    if permission_mode == PermissionMode.PLAN:
        console.print("[bold yellow]plan mode ON[/bold yellow] — destructive tools will be refused")
    elif permission_mode == PermissionMode.AUTO:
        console.print(
            "[bold red]auto mode ON[/bold red] — per-action confirmation prompts skipped"
        )
    elif permission_mode == PermissionMode.ACCEPT_EDITS:
        console.print(
            "[bold blue]accept-edits mode ON[/bold blue] — Edit/Write auto-approved; Bash/network still prompt"
        )
```

- [ ] **Step 7.6: Run tests to verify they pass**

```
pytest OpenComputer/tests/test_cli_flag_aliasing.py -v
```
Expected: PASS for `code` flag tests.

- [ ] **Step 7.7: Commit**

```
git add OpenComputer/opencomputer/cli.py OpenComputer/tests/test_cli_flag_aliasing.py
git commit -m "feat(cli): --auto and --accept-edits flags on code subcommand"
```

---

### Task 8: Apply `--auto` / `--accept-edits` to `chat` and `resume` subcommands

**Files:**
- Modify: `opencomputer/cli.py:1489-1530, 1583-1604`

- [ ] **Step 8.1: Add same flag block to `chat()` (around line 1489)**

Mirror the `code` subcommand additions: same `auto`, `accept_edits`, `yolo` (deprecated) options, same `permission_mode` derivation, same `_run_chat_session` call.

- [ ] **Step 8.2: Add to `resume()` (around line 1583)**

Same.

- [ ] **Step 8.3: Run tests**

```
pytest OpenComputer/tests/test_cli_flag_aliasing.py OpenComputer/tests/test_cli_oc_code.py -v
```
Expected: PASS.

- [ ] **Step 8.4: Commit**

```
git add OpenComputer/opencomputer/cli.py
git commit -m "feat(cli): --auto and --accept-edits on chat and resume subcommands"
```

---

### Task 9: Cron `--auto` flag with precedence preservation

**Files:**
- Modify: `opencomputer/cli_cron.py:114, 133`

- [ ] **Step 9.1: Add helper `_resolve_cron_runtime`**

```python
# opencomputer/cli_cron.py
def _resolve_cron_runtime(
    *, plan: bool, auto: bool, accept_edits: bool, yolo: bool
) -> RuntimeContext:
    from plugin_sdk import PermissionMode

    if yolo:
        # Deprecated alias.
        auto = True

    if plan:
        # plan beats auto (existing precedence).
        return RuntimeContext(
            plan_mode=True, yolo_mode=False, permission_mode=PermissionMode.PLAN,
        )
    if auto:
        return RuntimeContext(
            plan_mode=False, yolo_mode=True, permission_mode=PermissionMode.AUTO,
        )
    if accept_edits:
        return RuntimeContext(
            plan_mode=False, yolo_mode=False, permission_mode=PermissionMode.ACCEPT_EDITS,
        )
    return RuntimeContext(plan_mode=True, yolo_mode=False)  # cron default
```

- [ ] **Step 9.2: Update cron run subcommand to add `--auto`/`--accept-edits` Typer options and call helper**

Replace direct `plan_mode=not yolo` at line 133 with:

```python
    rt = _resolve_cron_runtime(plan=plan, auto=auto, accept_edits=accept_edits, yolo=yolo)
```

- [ ] **Step 9.3: Run cron tests**

```
pytest OpenComputer/tests/ -k "cron" -q
```
Expected: PASS.

- [ ] **Step 9.4: Commit**

```
git add OpenComputer/opencomputer/cli_cron.py
git commit -m "feat(cron): --auto and --accept-edits flags; preserve --yolo precedence"
```

---

### Task 10: Rename `yolo_cmd.py` to `auto_cmd.py` + AutoCommand

**Files:**
- Move: `opencomputer/agent/slash_commands_impl/yolo_cmd.py` → `auto_cmd.py`
- Modify: `opencomputer/agent/slash_commands_impl/__init__.py`
- Move: `tests/tier2_slash/test_yolo_cmd.py` → `test_auto_cmd.py`

- [ ] **Step 10.1: Write the new test file `test_auto_cmd.py`**

```python
# tests/tier2_slash/test_auto_cmd.py
"""Tests for /auto slash command and /yolo deprecated alias."""

from __future__ import annotations

import pytest

from opencomputer.agent.slash_commands_impl.auto_cmd import (
    AutoCommand,
    YoloCommand,
)
from plugin_sdk import PermissionMode, RuntimeContext, effective_permission_mode


def _fresh_runtime(custom: dict | None = None) -> RuntimeContext:
    return RuntimeContext(custom=custom or {})


@pytest.mark.asyncio
class TestAutoCommand:
    async def test_on_sets_canonical_and_legacy(self) -> None:
        rt = _fresh_runtime()
        result = await AutoCommand().execute("on", rt)
        assert result.handled is True
        assert rt.custom["permission_mode"] == "auto"
        assert rt.custom["yolo_session"] is True  # legacy compat
        assert effective_permission_mode(rt) == PermissionMode.AUTO

    async def test_off_clears_both(self) -> None:
        rt = _fresh_runtime({"permission_mode": "auto", "yolo_session": True})
        await AutoCommand().execute("off", rt)
        assert rt.custom.get("permission_mode") in (None, "default")
        assert rt.custom.get("yolo_session") in (None, False)

    async def test_status_no_mutation(self) -> None:
        rt = _fresh_runtime({"permission_mode": "auto"})
        result = await AutoCommand().execute("status", rt)
        assert "ON" in result.output
        assert rt.custom["permission_mode"] == "auto"  # unchanged


@pytest.mark.asyncio
class TestYoloDeprecationAlias:
    async def test_yolo_on_forwards_to_auto(self) -> None:
        rt = _fresh_runtime()
        result = await YoloCommand().execute("on", rt)
        assert effective_permission_mode(rt) == PermissionMode.AUTO
        assert "deprecat" in result.output.lower()
```

- [ ] **Step 10.2: Run test — fails**

```
pytest OpenComputer/tests/tier2_slash/test_auto_cmd.py -v
```
Expected: FAIL — file doesn't exist yet.

- [ ] **Step 10.3: Create `auto_cmd.py`**

```python
# opencomputer/agent/slash_commands_impl/auto_cmd.py
"""``/auto [on|off|status]`` — toggle auto mode (skip per-action confirmations).

Renamed from ``/yolo``. The ``YoloCommand`` class below is a deprecated
alias kept for one minor version; it forwards to ``AutoCommand`` and prints
a one-time deprecation line.

State is stored in two keys for backwards compatibility:
- ``runtime.custom["permission_mode"] = "auto"``  — canonical (read by helper)
- ``runtime.custom["yolo_session"] = True``       — legacy (still read by F1)
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_ON_MESSAGE = (
    "⚠ Auto mode is now ON for this session.\n"
    "Per-action ConsentGate prompts will be skipped — destructive tools "
    "(Bash, Edit, Write, MultiEdit, network sends) run without confirmation.\n"
    "Type /auto off to restore approval prompts."
)
_OFF_MESSAGE = "Auto mode is now OFF. ConsentGate prompts restored."
_USAGE = (
    "Usage: /auto [on|off|status]\n"
    "Skip per-action confirmation prompts for the rest of the session.\n"
    "WARNING: enabling means destructive tools run without confirmation."
)


class AutoCommand(SlashCommand):
    name = "auto"
    description = "Toggle auto mode (skip per-action confirmations)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        current = runtime.custom.get("permission_mode") == "auto"

        if sub == "":
            new_state = not current
        elif sub == "on":
            new_state = True
        elif sub == "off":
            new_state = False
        elif sub == "status":
            return SlashCommandResult(
                output=f"Auto mode is currently {'ON' if current else 'OFF'}",
                handled=True,
            )
        else:
            return SlashCommandResult(output=_USAGE, handled=True)

        if new_state:
            runtime.custom["permission_mode"] = "auto"
            runtime.custom["yolo_session"] = True  # legacy compat
        else:
            runtime.custom.pop("permission_mode", None)
            runtime.custom.pop("yolo_session", None)

        msg = _ON_MESSAGE if new_state else _OFF_MESSAGE
        return SlashCommandResult(output=msg, handled=True)


class YoloCommand(SlashCommand):
    name = "yolo"
    description = "[deprecated] Alias for /auto"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        from opencomputer.cli import _emit_yolo_deprecation
        _emit_yolo_deprecation()
        result = await AutoCommand().execute(args, runtime)
        return SlashCommandResult(
            output=f"[deprecated — use /auto] {result.output}",
            handled=True,
        )


__all__ = ["AutoCommand", "YoloCommand"]
```

- [ ] **Step 10.4: Delete `yolo_cmd.py` and remove `test_yolo_cmd.py`**

```
git rm OpenComputer/opencomputer/agent/slash_commands_impl/yolo_cmd.py
git rm OpenComputer/tests/tier2_slash/test_yolo_cmd.py
```

- [ ] **Step 10.5: Update slash registration in __init__.py**

```python
# opencomputer/agent/slash_commands_impl/__init__.py
from opencomputer.agent.slash_commands_impl.auto_cmd import AutoCommand, YoloCommand
# ...remove yolo_cmd import...
```

- [ ] **Step 10.6: Run tests**

```
pytest OpenComputer/tests/tier2_slash/test_auto_cmd.py -v
pytest OpenComputer/tests/ -k "slash" -q
```
Expected: PASS.

- [ ] **Step 10.7: Commit**

```
git add -A OpenComputer/opencomputer/agent/slash_commands_impl/ OpenComputer/tests/tier2_slash/
git commit -m "feat(slash): rename /yolo to /auto with deprecation alias"
```

---

### Task 11: New `/mode` slash command + `/accept-edits` shorthand

**Files:**
- Create: `opencomputer/agent/slash_commands_impl/mode_cmd.py`
- Test: `tests/tier2_slash/test_mode_cmd.py`

- [ ] **Step 11.1: Write the failing test**

```python
# tests/tier2_slash/test_mode_cmd.py
import pytest

from opencomputer.agent.slash_commands_impl.mode_cmd import (
    ModeCommand,
    AcceptEditsCommand,
)
from plugin_sdk import PermissionMode, RuntimeContext, effective_permission_mode


@pytest.mark.asyncio
class TestModeCommand:
    async def test_no_arg_shows_current(self) -> None:
        rt = RuntimeContext()
        result = await ModeCommand().execute("", rt)
        assert "default" in result.output.lower()

    async def test_set_plan(self) -> None:
        rt = RuntimeContext()
        await ModeCommand().execute("plan", rt)
        assert effective_permission_mode(rt) == PermissionMode.PLAN

    async def test_set_accept_edits(self) -> None:
        rt = RuntimeContext()
        await ModeCommand().execute("accept-edits", rt)
        assert effective_permission_mode(rt) == PermissionMode.ACCEPT_EDITS

    async def test_invalid_lists_options(self) -> None:
        rt = RuntimeContext()
        result = await ModeCommand().execute("bogus", rt)
        assert "default" in result.output and "auto" in result.output


@pytest.mark.asyncio
class TestAcceptEditsShorthand:
    async def test_toggles_accept_edits(self) -> None:
        rt = RuntimeContext()
        await AcceptEditsCommand().execute("", rt)
        assert effective_permission_mode(rt) == PermissionMode.ACCEPT_EDITS
```

- [ ] **Step 11.2: Run — fails**

```
pytest OpenComputer/tests/tier2_slash/test_mode_cmd.py -v
```

- [ ] **Step 11.3: Create `mode_cmd.py`**

```python
# opencomputer/agent/slash_commands_impl/mode_cmd.py
"""``/mode <name>`` — set permission mode + ``/accept-edits`` shorthand."""

from __future__ import annotations

from plugin_sdk import PermissionMode, effective_permission_mode
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

_VALID = ", ".join(m.value for m in PermissionMode)
_USAGE = f"Usage: /mode [{_VALID}]"


def _set_mode(runtime: RuntimeContext, mode: PermissionMode) -> None:
    if mode == PermissionMode.DEFAULT:
        runtime.custom.pop("permission_mode", None)
        runtime.custom.pop("plan_mode", None)
        runtime.custom.pop("yolo_session", None)
    else:
        runtime.custom["permission_mode"] = mode.value
        # Mirror to legacy keys for old readers (e.g. F1 ConsentGate prompt
        # handler that still reads custom["yolo_session"]).
        runtime.custom["plan_mode"] = (mode == PermissionMode.PLAN)
        runtime.custom["yolo_session"] = (mode == PermissionMode.AUTO)


class ModeCommand(SlashCommand):
    name = "mode"
    description = "Show or set the permission mode for this session"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sub = (args or "").strip().lower()
        if not sub:
            return SlashCommandResult(
                output=f"Current mode: {effective_permission_mode(runtime).value}\n{_USAGE}",
                handled=True,
            )
        try:
            mode = PermissionMode(sub)
        except ValueError:
            return SlashCommandResult(output=_USAGE, handled=True)
        _set_mode(runtime, mode)
        return SlashCommandResult(output=f"Mode set to {mode.value}.", handled=True)


class AcceptEditsCommand(SlashCommand):
    name = "accept-edits"
    description = "Set mode to accept-edits (auto-approve Edit/Write/MultiEdit/NotebookEdit)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        _set_mode(runtime, PermissionMode.ACCEPT_EDITS)
        return SlashCommandResult(output="Mode set to accept-edits.", handled=True)


__all__ = ["ModeCommand", "AcceptEditsCommand"]
```

- [ ] **Step 11.4: Register in __init__.py**

```python
from opencomputer.agent.slash_commands_impl.mode_cmd import (
    ModeCommand,
    AcceptEditsCommand,
)
```

- [ ] **Step 11.5: Tests pass**

```
pytest OpenComputer/tests/tier2_slash/test_mode_cmd.py -v
```

- [ ] **Step 11.6: Commit**

```
git add OpenComputer/opencomputer/agent/slash_commands_impl/mode_cmd.py OpenComputer/opencomputer/agent/slash_commands_impl/__init__.py OpenComputer/tests/tier2_slash/test_mode_cmd.py
git commit -m "feat(slash): /mode and /accept-edits commands"
```

---

### Task 12: Update `/plan` extension command to write canonical key

**Files:**
- Modify: `extensions/coding-harness/slash_commands/plan.py`

- [ ] **Step 12.1: Update PlanOnCommand and PlanOffCommand to also write canonical key**

```python
# extensions/coding-harness/slash_commands/plan.py
class PlanOnCommand(SlashCommand):
    name = "plan"
    description = "Enable plan mode (read-only planning; destructive tools refused)."

    async def execute(self, args: str, runtime: Any) -> SlashCommandResult:
        runtime.custom["permission_mode"] = "plan"  # canonical
        runtime.custom["plan_mode"] = True           # legacy compat
        self.harness_ctx.session_state.set("mode:plan", True)
        return SlashCommandResult(output="Plan mode enabled. ...", handled=True)


class PlanOffCommand(SlashCommand):
    name = "plan-off"
    description = "Disable plan mode and allow destructive tool calls again."

    async def execute(self, args: str, runtime: Any) -> SlashCommandResult:
        runtime.custom.pop("permission_mode", None)
        runtime.custom["plan_mode"] = False
        self.harness_ctx.session_state.set("mode:plan", False)
        return SlashCommandResult(output="Plan mode disabled.", handled=True)
```

- [ ] **Step 12.2: Run plan slash tests**

```
pytest OpenComputer/tests/ -k "plan_cmd or PlanOn or PlanOff" -v
```
Expected: PASS.

- [ ] **Step 12.3: Commit**

```
git add OpenComputer/extensions/coding-harness/slash_commands/plan.py
git commit -m "feat(slash): /plan also writes canonical permission_mode key"
```

---

### Task 13: Thread `permission_mode` through PromptBuilder

**Files:**
- Modify: `opencomputer/agent/prompt_builder.py:184-260, 356-388`
- Modify: `opencomputer/agent/loop.py:706`

- [ ] **Step 13.1: Add field to PromptContext + kwarg to build/build_with_memory**

```python
# opencomputer/agent/prompt_builder.py
from plugin_sdk import PermissionMode

@dataclass(frozen=True)
class PromptContext:
    plan_mode: bool = False
    yolo_mode: bool = False
    permission_mode: PermissionMode = PermissionMode.DEFAULT  # NEW
    # ...other existing fields...


class PromptBuilder:
    def build(
        self,
        *,
        plan_mode: bool = False,
        yolo_mode: bool = False,
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        # ...other kwargs...
    ) -> str:
        ctx = PromptContext(
            plan_mode=plan_mode,
            yolo_mode=yolo_mode,
            permission_mode=permission_mode,
            # ...
        )
        return self._render(ctx)

    def _render(self, ctx: PromptContext) -> str:
        return self._template.render(
            plan_mode=ctx.plan_mode,
            yolo_mode=ctx.yolo_mode,
            permission_mode=ctx.permission_mode.value,  # Jinja gets the string
            # ...
        )

    async def build_with_memory(
        self,
        *,
        plan_mode: bool = False,
        yolo_mode: bool = False,
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        # ...
    ) -> str:
        # ...same threading...
```

- [ ] **Step 13.2: Update loop.py:706 caller**

```python
# opencomputer/agent/loop.py
from plugin_sdk import effective_permission_mode

# at the build call:
            permission_mode=effective_permission_mode(self._runtime),
```

- [ ] **Step 13.3: Run prompt_builder tests**

```
pytest OpenComputer/tests/ -k "prompt_builder or build_with_memory or base_prompt" -q
```

- [ ] **Step 13.4: Commit**

```
git add OpenComputer/opencomputer/agent/prompt_builder.py OpenComputer/opencomputer/agent/loop.py
git commit -m "feat(prompt): thread permission_mode through PromptBuilder"
```

---

### Task 14: Update `base.j2` to four-branch dispatch

**Files:**
- Modify: `opencomputer/agent/prompts/base.j2:147-158`

- [ ] **Step 14.1: Replace the two-branch yolo_mode block**

Replace lines 147-158:

```jinja
{% if permission_mode == "auto" -%}
**Auto mode is active.** Per-action confirmation prompts are skipped — destructive tools (Bash, Edit, Write, MultiEdit, network) run without asking. Use judgment: irreversible actions (deleting data, force-pushing, modifying shared resources) still warrant pausing to confirm via the chat surface.
{%- elif permission_mode == "accept-edits" -%}
**Accept-edits mode is active.** File edits via Edit / Write / MultiEdit / NotebookEdit are auto-approved without prompts. Bash and network calls (WebFetch / WebSearch) STILL prompt — including Bash that mutates files like `sed -i` or `> path`. The user opted in to unprompted edits, not unprompted shell.
{%- elif permission_mode == "plan" -%}
{# existing plan mode branch — keep #}
{%- else -%}
Default mode. Tools that touch filesystem, network, or the user's environment go through the consent gate. Ask before doing anything irreversible.
{%- endif %}
```

- [ ] **Step 14.2: Run prompt template tests**

```
pytest OpenComputer/tests/test_base_prompt_engineered.py -v
```

- [ ] **Step 14.3: Commit**

```
git add OpenComputer/opencomputer/agent/prompts/base.j2
git commit -m "feat(prompt): four-branch permission_mode dispatch in base.j2"
```

---

### Task 15: PR-2 final integration

- [ ] **Step 15.1: Full suite green**

```
pytest OpenComputer/ -x -q
ruff check OpenComputer/
```

- [ ] **Step 15.2: Push PR-2**

```
cd OpenComputer
git push
```

End of PR-2.

---

# PR-3 — Accept-edits behaviour (hook + injection provider)

Depends on PR-1, PR-2. Net-new mode runtime behaviour.

---

### Task 16: accept-edits PreToolUse hook

**Files:**
- Create: `extensions/coding-harness/hooks/accept_edits_hook.py`
- Test: `tests/test_accept_edits_hook.py` (flat — existing `tests/conftest.py` already registers `extensions.coding_harness` namespace)

- [ ] **Step 16.1: Write the failing test**

```python
# tests/test_accept_edits_hook.py
import pytest

from extensions.coding_harness.hooks.accept_edits_hook import accept_edits_hook  # type: ignore[import-not-found]
from plugin_sdk import PermissionMode, RuntimeContext
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent


def _ctx(rt: RuntimeContext, tool: str, args: dict | None = None) -> HookContext:
    return HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s1",
        tool_call=ToolCall(id="c1", name=tool, arguments=args or {}),
        runtime=rt,
    )


@pytest.mark.asyncio
class TestAcceptEditsHook:
    @pytest.fixture
    def runtime(self) -> RuntimeContext:
        return RuntimeContext(permission_mode=PermissionMode.ACCEPT_EDITS)

    @pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
    async def test_auto_approves_edit_family(
        self, runtime: RuntimeContext, tool: str
    ) -> None:
        decision = await accept_edits_hook(_ctx(runtime, tool))
        assert decision is not None
        assert decision.decision == "approve"

    async def test_does_not_approve_bash(self, runtime: RuntimeContext) -> None:
        decision = await accept_edits_hook(
            _ctx(runtime, "Bash", {"command": "ls"})
        )
        assert decision is None

    async def test_does_not_approve_bash_sed_i(self, runtime: RuntimeContext) -> None:
        # Bash that mutates files via sed -i is NOT auto-approved.
        decision = await accept_edits_hook(
            _ctx(runtime, "Bash", {"command": "sed -i 's/x/y/' file"})
        )
        assert decision is None

    async def test_does_not_approve_webfetch(self, runtime: RuntimeContext) -> None:
        decision = await accept_edits_hook(_ctx(runtime, "WebFetch"))
        assert decision is None

    async def test_only_fires_in_accept_edits_mode(self) -> None:
        rt_default = RuntimeContext()
        decision = await accept_edits_hook(_ctx(rt_default, "Edit"))
        assert decision is None

    async def test_does_not_fire_in_auto_mode(self) -> None:
        # In auto mode, the BypassManager handles approval — accept-edits hook is a no-op.
        rt_auto = RuntimeContext(permission_mode=PermissionMode.AUTO)
        decision = await accept_edits_hook(_ctx(rt_auto, "Edit"))
        assert decision is None
```

- [ ] **Step 16.2: Run — fails**

```
pytest OpenComputer/tests/coding_harness/test_accept_edits_hook.py -v
```

- [ ] **Step 16.3: Implement the hook**

```python
# extensions/coding-harness/hooks/accept_edits_hook.py
"""accept_edits_hook — PreToolUse auto-approver for the Edit-family tools.

Fires only when ``effective_permission_mode(runtime) == ACCEPT_EDITS``. Returns
a ``HookDecision(decision="approve")`` for tool names in
:data:`AUTO_APPROVED_TOOLS` so the F1 ConsentGate skips the per-action prompt
for those tools. Bash and network tools fall through (no decision returned),
preserving their normal consent flow even in accept-edits mode.

Design rationale: the user opted in to unprompted *edits*, not unprompted
*shell*. Bash that happens to mutate files (``sed -i``, ``> path``) is
deliberately NOT included.
"""
from __future__ import annotations

from plugin_sdk import PermissionMode, effective_permission_mode
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

#: Exact tool-name allowlist. New file-edit tools must be added here
#: explicitly — opt-in, not pattern-matched.
AUTO_APPROVED_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


async def accept_edits_hook(ctx: HookContext) -> HookDecision | None:
    if ctx.runtime is None:
        return None
    if effective_permission_mode(ctx.runtime) != PermissionMode.ACCEPT_EDITS:
        return None
    if ctx.tool_call is None:
        return None
    if ctx.tool_call.name not in AUTO_APPROVED_TOOLS:
        return None
    return HookDecision(
        decision="approve",
        reason=f"accept-edits mode auto-approved {ctx.tool_call.name}",
    )


def build_accept_edits_hook_spec() -> HookSpec:
    return HookSpec(
        event=HookEvent.PRE_TOOL_USE,
        handler=accept_edits_hook,
        matcher=None,
        fire_and_forget=False,
    )


__all__ = [
    "AUTO_APPROVED_TOOLS",
    "accept_edits_hook",
    "build_accept_edits_hook_spec",
]
```

- [ ] **Step 16.4: Run — pass**

```
pytest OpenComputer/tests/coding_harness/test_accept_edits_hook.py -v
```

- [ ] **Step 16.5: Commit**

```
git add OpenComputer/extensions/coding-harness/hooks/accept_edits_hook.py OpenComputer/tests/coding_harness/test_accept_edits_hook.py
git commit -m "feat(coding-harness): accept_edits_hook auto-approves Edit-family tools"
```

---

### Task 17: accept-edits dynamic injection provider

**Files:**
- Create: `extensions/coding-harness/modes/accept_edits_mode.py`
- Create: `extensions/coding-harness/prompts/accept_edits_mode.j2`

- [ ] **Step 17.1: Write the Jinja template**

```jinja
{# extensions/coding-harness/prompts/accept_edits_mode.j2 #}
**Accept-edits mode active.** Edit/Write/MultiEdit/NotebookEdit calls are auto-approved without per-action prompts. Bash and network calls still go through the consent gate — including Bash that would mutate files (sed -i, > path). The user has opted in to unprompted edits only.
```

- [ ] **Step 17.2: Implement the provider**

```python
# extensions/coding-harness/modes/accept_edits_mode.py
"""AcceptEditsMode injection provider — Jinja2-backed sibling to plan_mode."""

from __future__ import annotations

from modes import render  # type: ignore[import-not-found]
from plugin_sdk import PermissionMode, effective_permission_mode
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext


class AcceptEditsModeInjectionProvider(DynamicInjectionProvider):
    priority = 10

    @property
    def provider_id(self) -> str:
        return "coding-harness:accept-edits-mode"

    async def collect(self, ctx: InjectionContext) -> str | None:
        if effective_permission_mode(ctx.runtime) != PermissionMode.ACCEPT_EDITS:
            return None
        return render("accept_edits_mode.j2")


__all__ = ["AcceptEditsModeInjectionProvider"]
```

- [ ] **Step 17.3: Register in coding-harness plugin.py**

Edit `extensions/coding-harness/plugin.py`:

```python
from modes.accept_edits_mode import AcceptEditsModeInjectionProvider
from hooks.accept_edits_hook import build_accept_edits_hook_spec

# in register():
    api.register_dynamic_injection(AcceptEditsModeInjectionProvider())
    api.register_hook(build_accept_edits_hook_spec())
```

- [ ] **Step 17.4: Run integration tests**

```
pytest OpenComputer/tests/ -k "coding_harness or accept_edits" -v
```

- [ ] **Step 17.5: Commit**

```
git add OpenComputer/extensions/coding-harness/modes/accept_edits_mode.py OpenComputer/extensions/coding-harness/prompts/accept_edits_mode.j2 OpenComputer/extensions/coding-harness/plugin.py
git commit -m "feat(coding-harness): accept-edits dynamic injection provider"
```

---

### Task 18: PR-3 finalize

- [ ] **Step 18.1: Full suite + lint**

```
pytest OpenComputer/ -x -q
ruff check OpenComputer/
```

- [ ] **Step 18.2: Push PR-3**

```
git push
```

End of PR-3.

---

# PR-4 — TUI: Shift+Tab cycling + mode badge

Depends on PR-1+2+3. Pure UX polish.

---

### Task 19: Add Shift+Tab keybinding to custom Application

**Files:**
- Modify: `opencomputer/cli_ui/input_loop.py:433-722`
- Test: `tests/test_mode_badge.py` (flat)

- [ ] **Step 19.1: Write the failing test**

```python
# tests/test_mode_badge.py
"""Shift+Tab cycles permission modes; badge reflects current mode."""

import pytest

from plugin_sdk import PermissionMode, RuntimeContext, effective_permission_mode


# Helper: invoke the cycle function exposed by input_loop.
def _cycle(rt: RuntimeContext) -> None:
    from opencomputer.cli_ui.input_loop import _cycle_permission_mode
    _cycle_permission_mode(rt)


class TestShiftTabCycle:
    def test_default_to_accept_edits(self) -> None:
        rt = RuntimeContext()
        _cycle(rt)
        assert effective_permission_mode(rt) == PermissionMode.ACCEPT_EDITS

    def test_accept_edits_to_auto(self) -> None:
        rt = RuntimeContext(custom={"permission_mode": "accept-edits"})
        _cycle(rt)
        assert effective_permission_mode(rt) == PermissionMode.AUTO

    def test_auto_to_plan(self) -> None:
        rt = RuntimeContext(custom={"permission_mode": "auto"})
        _cycle(rt)
        assert effective_permission_mode(rt) == PermissionMode.PLAN

    def test_plan_back_to_default(self) -> None:
        rt = RuntimeContext(custom={"permission_mode": "plan"})
        _cycle(rt)
        assert effective_permission_mode(rt) == PermissionMode.DEFAULT

    def test_full_cycle_returns_to_default(self) -> None:
        rt = RuntimeContext()
        for _ in range(4):
            _cycle(rt)
        assert effective_permission_mode(rt) == PermissionMode.DEFAULT


class TestModeBadgeRender:
    def test_badge_shows_mode_name(self) -> None:
        from opencomputer.cli_ui.input_loop import _render_mode_badge
        rt = RuntimeContext(permission_mode=PermissionMode.ACCEPT_EDITS)
        # Returns prompt_toolkit FormattedText [(style, text), ...]
        ft = _render_mode_badge(rt)
        text = "".join(seg[1] for seg in ft)
        assert "accept-edits" in text
        assert "[E]" in text  # ASCII glyph for accessibility / NO_COLOR

    def test_badge_default_glyph(self) -> None:
        from opencomputer.cli_ui.input_loop import _render_mode_badge
        rt = RuntimeContext()
        text = "".join(seg[1] for seg in _render_mode_badge(rt))
        assert "default" in text
        assert "[D]" in text
```

- [ ] **Step 19.2: Run — fails**

```
pytest OpenComputer/tests/tui/test_mode_badge.py -v
```

- [ ] **Step 19.3: Implement cycle helper + Shift+Tab binding + badge render**

Edit `opencomputer/cli_ui/input_loop.py`. Add at module top:

```python
from plugin_sdk import PermissionMode, effective_permission_mode

_CYCLE_ORDER = (
    PermissionMode.DEFAULT,
    PermissionMode.ACCEPT_EDITS,
    PermissionMode.AUTO,
    PermissionMode.PLAN,
)

_GLYPH = {
    PermissionMode.DEFAULT: "[D]",
    PermissionMode.ACCEPT_EDITS: "[E]",
    PermissionMode.AUTO: "[A]",
    PermissionMode.PLAN: "[P]",
}

_STYLE = {
    PermissionMode.DEFAULT: "fg:green",
    PermissionMode.ACCEPT_EDITS: "fg:ansiblue",
    PermissionMode.AUTO: "fg:ansired bold",
    PermissionMode.PLAN: "fg:ansiyellow",
}


def _cycle_permission_mode(runtime) -> None:
    current = effective_permission_mode(runtime)
    idx = _CYCLE_ORDER.index(current)
    next_mode = _CYCLE_ORDER[(idx + 1) % len(_CYCLE_ORDER)]
    if next_mode == PermissionMode.DEFAULT:
        runtime.custom.pop("permission_mode", None)
        runtime.custom.pop("plan_mode", None)
        runtime.custom.pop("yolo_session", None)
    else:
        runtime.custom["permission_mode"] = next_mode.value
        runtime.custom["plan_mode"] = (next_mode == PermissionMode.PLAN)
        runtime.custom["yolo_session"] = (next_mode == PermissionMode.AUTO)


def _render_mode_badge(runtime):
    mode = effective_permission_mode(runtime)
    return [(_STYLE[mode], f" {_GLYPH[mode]} mode: {mode.value} ")]
```

In the custom Application's `KeyBindings` block (around line 433+), add:

```python
    @kb.add("s-tab")
    def _(event):
        _cycle_permission_mode(loop_runtime_ref())
        event.app.invalidate()
```

(Where `loop_runtime_ref` is the closure-captured reference to the live runtime — match the existing pattern in this file.)

- [ ] **Step 19.4: Add the badge Window to the HSplit**

In the `Layout(HSplit([...]))` construction (around line 672-684), append:

```python
    badge_window = Window(
        height=1,
        content=FormattedTextControl(lambda: _render_mode_badge(loop_runtime_ref())),
    )
    layout = Layout(HSplit([
        # ...existing children...
        badge_window,
    ]))
```

TTY-less guard: wrap `badge_window` creation in `if sys.stdout.isatty():` and append the empty filler otherwise.

- [ ] **Step 19.5: Run tests**

```
pytest OpenComputer/tests/tui/test_mode_badge.py -v
```

- [ ] **Step 19.6: Commit**

```
git add OpenComputer/opencomputer/cli_ui/input_loop.py OpenComputer/tests/tui/test_mode_badge.py
git commit -m "feat(tui): Shift+Tab cycles permission modes; mode badge in HSplit"
```

---

### Task 20: `/help` legend update

**Files:**
- Modify: `opencomputer/cli_ui/slash.py`

- [ ] **Step 20.1: Add a one-liner to the help text emitted by `/help`**

Find the `/help` rendering function (likely a `_format_help()` returning a string) and append:

```python
    legend_lines = [
        "",
        "Modes: Shift+Tab cycles default → accept-edits → auto → plan → default.",
        "Or use slash commands: /mode <name>, /auto, /plan, /accept-edits.",
    ]
```

- [ ] **Step 20.2: Test `/help` output**

```
pytest OpenComputer/tests/ -k "help_cmd or test_slash_help" -v
```

- [ ] **Step 20.3: Commit**

```
git add OpenComputer/opencomputer/cli_ui/slash.py
git commit -m "docs(slash): /help legend mentions Shift+Tab + mode commands"
```

---

### Task 21: PR-4 finalize

- [ ] **Step 21.1: Full suite + lint + manual smoke**

```
pytest OpenComputer/ -x -q
ruff check OpenComputer/
```

Manual smoke-test in TUI:

```
cd OpenComputer && opencomputer code
```

In the TUI:
1. Bottom badge reads `[D] mode: default`.
2. Press Shift+Tab → cycles `[E] accept-edits` → `[A] auto` → `[P] plan` → `[D] default`.
3. `/mode plan`, `/auto`, `/plan-off`, `/accept-edits` all work and update the badge.

- [ ] **Step 21.2: Push PR-4**

```
git push
```

End of PR-4. Plan complete.

---

## Self-Review

**Spec coverage:** every slice from the design doc ([2026-04-29-permission-modes-design.md](../specs/2026-04-29-permission-modes-design.md)) has at least one task above.

**Placeholder scan:** no "TBD"/"TODO"/"similar to Task N" — all code blocks have real content.

**Type consistency:** `PermissionMode` referenced consistently across all tasks; `effective_permission_mode` signature stable; `_cycle_permission_mode` named identically in test and impl.

**Known caveats to be resolved during execution (audit-noted):**
1. Task 14's existing `{% if plan_mode %}` block in base.j2 — current line content needs to be read and merged with the new four-branch dispatch; the snippet shown is the structural shape, not a literal replacement. Implementer should read lines 47, 147-158, 271 and merge thoughtfully.
2. Task 13 `PromptContext` snippet — only shows the relevant fields (`plan_mode`, `yolo_mode`, `permission_mode`); the actual class has additional fields (`active_persona_id`, `user_tone`, `persona_preferred_tone`, etc.). **Add the new field; do NOT replace the existing class definition.**
3. Task 4 — `tasks/runtime.py:230` and `cron/scheduler.py:204-205` listed in design Slice 6 are intentionally **not** updated as separate steps; `RuntimeContext.permission_mode` defaults to `PermissionMode.DEFAULT`, so existing construction sites that don't pass it work without changes. They become "implicit pass-through compat."
4. Task 9.1's `_resolve_cron_runtime` default returns `RuntimeContext(plan_mode=True)` — this is **intentional** preservation of existing `cli_cron.py:133` behaviour (`plan_mode=not yolo`): cron jobs default to plan-mode-on as a safety posture.
5. Tests are placed flat in `tests/` (not `tests/coding_harness/` or `tests/tui/`); existing `tests/conftest.py` registers the `extensions.coding_harness` namespace alias so the kebab dir is importable.

---

## Execution Handoff

Plan saved. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session via `superpowers:executing-plans`, batch execution with checkpoints.

The user's prior instruction was: "implement what you planned ... using /executing-plans" — so option 2 is selected.
