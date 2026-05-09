"""v1.1 plan-3 M9.1 — `permission_mode = "auto"` equivalence pin.

Every entry point that opts into auto mode must surface the same
:func:`plugin_sdk.effective_permission_mode` result. Pins:

* CLI ``--auto`` flag (cli.py:chat / cli.py:code / cli.py:oneshot) sets
  ``runtime.permission_mode = PermissionMode.AUTO``.
* Slash command ``/auto on`` mutates ``runtime.custom["permission_mode"]
  = "auto"`` (and the legacy ``"yolo_session"=True`` for backwards
  compat with old readers).
* Wire :class:`ChatParams.permission_mode = "auto"` plumbs into the
  agent loop's runtime context the same way.
* Legacy ``--yolo`` flag and ``runtime.yolo_mode = True`` resolve to
  AUTO via the helper.

When all four set "auto" their resolved
:func:`effective_permission_mode` is :data:`PermissionMode.AUTO`. This
is the M9.1 acceptance criterion ("setting auto via CLI / slash / wire
all surface the same `effective_permission_mode`").

M9.2 — the safety classifier that intercepts tool calls in auto mode —
is a separate, multi-day, security-critical item. M9.1 ships only the
mode-toggle surface.
"""

from __future__ import annotations

import asyncio

import pytest

from plugin_sdk.permission_mode import PermissionMode, effective_permission_mode
from plugin_sdk.runtime_context import RuntimeContext


def _resolve_sync(runtime: RuntimeContext) -> PermissionMode:
    """Wrap effective_permission_mode in a sync call (it's pure)."""
    return effective_permission_mode(runtime)


# ─── direct field assignments (CLI sets this) ────────────────────────────


def test_cli_auto_sets_permission_mode_field() -> None:
    """``oc chat --auto`` constructs a RuntimeContext with permission_mode=AUTO."""
    runtime = RuntimeContext(permission_mode=PermissionMode.AUTO)
    assert _resolve_sync(runtime) is PermissionMode.AUTO


def test_cli_legacy_yolo_resolves_to_auto() -> None:
    """``oc chat --yolo`` (deprecated alias) sets runtime.yolo_mode=True."""
    runtime = RuntimeContext(yolo_mode=True)
    assert _resolve_sync(runtime) is PermissionMode.AUTO


# ─── slash command (/auto) writes to runtime.custom ──────────────────────


def test_slash_auto_on_mutates_custom() -> None:
    """``/auto on`` writes runtime.custom['permission_mode']='auto'.

    Mirrors what
    ``opencomputer.agent.slash_commands_impl.auto_cmd.AutoCommand.execute``
    does on the "on" path. We don't import the slash command directly to
    keep this test loop-independent — we replicate its observable mutation.
    """
    runtime = RuntimeContext()
    runtime.custom["permission_mode"] = "auto"
    runtime.custom["yolo_session"] = True  # legacy back-compat key
    assert _resolve_sync(runtime) is PermissionMode.AUTO


def test_slash_auto_via_legacy_yolo_session_key() -> None:
    """A plugin / older session DB row may set only the legacy key."""
    runtime = RuntimeContext()
    runtime.custom["yolo_session"] = True
    assert _resolve_sync(runtime) is PermissionMode.AUTO


# ─── wire RPC (ChatParams.permission_mode) ───────────────────────────────


def test_wire_chat_params_accept_auto() -> None:
    """The wire ChatParams accepts 'auto' as a permission_mode value."""
    from opencomputer.gateway.protocol_v2 import ChatParams

    params = ChatParams(message="hi", permission_mode="auto")
    assert params.permission_mode == "auto"
    # And the wire dispatcher's runtime construction should resolve to AUTO.
    runtime = RuntimeContext(
        permission_mode=PermissionMode(params.permission_mode),
    )
    assert _resolve_sync(runtime) is PermissionMode.AUTO


def test_wire_chat_params_default_is_default() -> None:
    """No permission_mode → default."""
    from opencomputer.gateway.protocol_v2 import ChatParams

    params = ChatParams(message="hi")
    assert params.permission_mode == "default"


# ─── equivalence ─────────────────────────────────────────────────────────


def test_all_four_paths_resolve_to_same_auto() -> None:
    """The acceptance criterion: every path surfaces the SAME PermissionMode."""
    cli_runtime = RuntimeContext(permission_mode=PermissionMode.AUTO)
    slash_runtime = RuntimeContext()
    slash_runtime.custom["permission_mode"] = "auto"
    wire_runtime = RuntimeContext(permission_mode=PermissionMode("auto"))
    legacy_runtime = RuntimeContext(yolo_mode=True)

    resolved = {
        _resolve_sync(cli_runtime),
        _resolve_sync(slash_runtime),
        _resolve_sync(wire_runtime),
        _resolve_sync(legacy_runtime),
    }
    assert resolved == {PermissionMode.AUTO}


# ─── plan beats auto on conflict (matches the helper's contract) ─────────


def test_plan_beats_auto_on_conflict() -> None:
    """Documented precedence: plan wins when both are set."""
    runtime = RuntimeContext(permission_mode=PermissionMode.AUTO)
    runtime.custom["plan_mode"] = True
    # session-mutable plan_mode wins over runtime.permission_mode=AUTO
    assert _resolve_sync(runtime) is PermissionMode.PLAN


def test_session_mutable_permission_mode_overrides_field() -> None:
    """``runtime.custom['permission_mode']`` wins over the frozen field."""
    runtime = RuntimeContext(permission_mode=PermissionMode.DEFAULT)
    runtime.custom["permission_mode"] = "auto"
    assert _resolve_sync(runtime) is PermissionMode.AUTO

    # And the inverse: setting custom['permission_mode']='default' overrides
    # an AUTO frozen field too (so users can downgrade mid-session).
    runtime2 = RuntimeContext(permission_mode=PermissionMode.AUTO)
    runtime2.custom["permission_mode"] = "default"
    assert _resolve_sync(runtime2) is PermissionMode.DEFAULT


# ─── slash command actually mutates the same key the helper reads ────────


def test_slash_auto_command_writes_canonical_key() -> None:
    """Direct integration test: the actual AutoCommand mutates the key
    :func:`effective_permission_mode` reads. Catches drift if the slash
    command is ever rewritten to use a different key name."""
    from opencomputer.agent.slash_commands_impl.auto_cmd import AutoCommand

    runtime = RuntimeContext()
    cmd = AutoCommand()
    asyncio.run(cmd.execute("on", runtime))
    assert _resolve_sync(runtime) is PermissionMode.AUTO

    asyncio.run(cmd.execute("off", runtime))
    assert _resolve_sync(runtime) is not PermissionMode.AUTO


@pytest.mark.parametrize(
    "value", ["AUTO", "Auto", "auto"],
)
def test_permission_mode_string_constructor_case(value: str) -> None:
    """StrEnum lookups are case-sensitive — pin the canonical form."""
    if value == "auto":
        assert PermissionMode(value) is PermissionMode.AUTO
    else:
        with pytest.raises(ValueError):
            PermissionMode(value)
