"""Tests for Hermes-parity Tier A+B slash wrappers (2026-04-30).

Covers /config, /insights, /skills, /cron, /plugins, /profile, /image,
/tools (Tier A) plus /retry, /stop (Tier B). The slash command's job is
to render information; behaviour for callbacks (queue add, bg-process
kill, image queue) is verified via SlashContext stub callbacks rather
than running an actual loop.
"""
from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from opencomputer.cli_ui.slash import is_slash_command, resolve_command
from opencomputer.cli_ui.slash_handlers import (
    SlashContext,
    _handle_config,
    _handle_cron_inline,
    _handle_image,
    _handle_insights,
    _handle_plugins_inline,
    _handle_profile_inline,
    _handle_retry,
    _handle_skills_inline,
    _handle_stop_bg,
    _handle_tools_inline,
    dispatch_slash,
)


def _ctx(**overrides) -> SlashContext:
    """Minimal SlashContext that captures console output to StringIO."""
    buf = StringIO()
    base = dict(
        console=Console(file=buf, force_terminal=False, width=120),
        session_id="s-test",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {"total_tokens": 1234},
        get_session_list=lambda: [
            {"id": "abc12345", "title": "Hello"},
            {"id": "def67890", "title": "World"},
        ],
    )
    base.update(overrides)
    return SlashContext(**base)


def _captured(ctx: SlashContext) -> str:
    """Pull captured output from the SlashContext's StringIO console."""
    return ctx.console.file.getvalue()


# ─── Registry presence ──────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "config", "insights", "skills", "cron", "plugins",
    "profile", "image", "tools", "retry", "stop",
])
def test_command_is_registered(name):
    assert resolve_command(name) is not None
    assert is_slash_command(f"/{name}")


# ─── /config ────────────────────────────────────────────────────────


def test_handle_config_renders_model_and_paths():
    cfg = MagicMock()
    cfg.model.provider = "anthropic"
    cfg.model.model = "claude-opus-4-7"
    cfg.model.cheap_model = None
    cfg.model.max_tokens = 4096
    cfg.model.temperature = 1.0
    cfg.session.db_path = "/tmp/sessions.db"
    cfg.memory.declarative_path = "/tmp/MEMORY.md"
    ctx = _ctx(config=cfg)
    result = _handle_config(ctx, [])
    out = _captured(ctx)
    assert result.handled
    assert "anthropic" in out
    assert "claude-opus-4-7" in out


def test_handle_config_degrades_on_attribute_error():
    cfg = MagicMock(spec=[])  # No attributes — every access raises
    ctx = _ctx(config=cfg)
    result = _handle_config(ctx, [])
    assert result.handled  # never crashes


# ─── /insights ──────────────────────────────────────────────────────


def test_handle_insights_renders_session_count():
    ctx = _ctx()
    result = _handle_insights(ctx, [])
    out = _captured(ctx)
    assert result.handled
    assert "2" in out  # 2 sessions in stub
    assert "Hello" in out


def test_handle_insights_handles_empty_session_list():
    ctx = _ctx(get_session_list=lambda: [])
    result = _handle_insights(ctx, [])
    assert result.handled


# ─── /skills ────────────────────────────────────────────────────────


def test_handle_skills_inline_runs_without_crashing():
    """Handler depends on MemoryManager; verify it gracefully degrades."""
    ctx = _ctx()  # config is MagicMock — memory paths won't resolve
    result = _handle_skills_inline(ctx, [])
    assert result.handled  # shouldn't crash


# ─── /cron ──────────────────────────────────────────────────────────


def test_handle_cron_inline_runs_without_crashing():
    ctx = _ctx()
    result = _handle_cron_inline(ctx, [])
    assert result.handled


# ─── /plugins ───────────────────────────────────────────────────────


def test_handle_plugins_inline_runs_without_crashing():
    ctx = _ctx()
    result = _handle_plugins_inline(ctx, [])
    assert result.handled


# ─── /profile ───────────────────────────────────────────────────────


def test_handle_profile_inline_renders_active_profile():
    ctx = _ctx()
    result = _handle_profile_inline(ctx, [])
    out = _captured(ctx)
    assert result.handled
    assert "profile" in out.lower()


# ─── /tools ─────────────────────────────────────────────────────────


def test_handle_tools_inline_runs_without_crashing():
    ctx = _ctx()
    result = _handle_tools_inline(ctx, [])
    assert result.handled


# ─── /image ─────────────────────────────────────────────────────────


def test_handle_image_requires_path_argument():
    fired = []
    ctx = _ctx(on_image_attach=lambda p: fired.append(p) or (True, "queued"))
    result = _handle_image(ctx, [])  # no args
    out = _captured(ctx)
    assert result.handled
    assert "Usage" in out
    assert fired == []  # callback NOT invoked


def test_handle_image_calls_callback_with_path(tmp_path):
    img = tmp_path / "test.png"
    img.write_bytes(b"")
    fired = []

    def _attach(p):
        fired.append(p)
        return (True, f"queued {p}")

    ctx = _ctx(on_image_attach=_attach)
    result = _handle_image(ctx, [str(img)])
    assert result.handled
    assert fired == [str(img)]


def test_handle_image_reports_callback_failure(tmp_path):
    ctx = _ctx(on_image_attach=lambda _p: (False, "file not found"))
    result = _handle_image(ctx, ["/nonexistent.png"])
    out = _captured(ctx)
    assert result.handled
    assert "file not found" in out


# ─── /retry ─────────────────────────────────────────────────────────


def test_handle_retry_reports_no_message_to_retry():
    ctx = _ctx(on_retry=lambda: (False, "no previous user message"))
    result = _handle_retry(ctx, [])
    out = _captured(ctx)
    assert result.handled
    assert "no previous" in out


def test_handle_retry_reports_success_with_preview():
    ctx = _ctx(on_retry=lambda: (True, "rewrite the auth flow please"))
    result = _handle_retry(ctx, [])
    out = _captured(ctx)
    assert result.handled
    assert "↻" in out or "Queued" in out


def test_handle_retry_truncates_long_preview():
    long_msg = "a" * 200
    ctx = _ctx(on_retry=lambda: (True, long_msg))
    result = _handle_retry(ctx, [])
    out = _captured(ctx)
    assert result.handled
    assert "..." in out  # truncated


# ─── /stop ──────────────────────────────────────────────────────────


def test_handle_stop_zero_processes():
    ctx = _ctx(on_stop_bg=lambda: 0)
    result = _handle_stop_bg(ctx, [])
    out = _captured(ctx)
    assert result.handled
    assert "no" in out.lower() or "0" in out


def test_handle_stop_kills_processes():
    ctx = _ctx(on_stop_bg=lambda: 3)
    result = _handle_stop_bg(ctx, [])
    out = _captured(ctx)
    assert result.handled
    assert "3" in out


def test_handle_stop_handles_callback_exception():
    def _raise():
        raise RuntimeError("test failure")

    ctx = _ctx(on_stop_bg=_raise)
    result = _handle_stop_bg(ctx, [])
    out = _captured(ctx)
    assert result.handled  # never crashes


# ─── stop_all_processes integration ─────────────────────────────────


@pytest.mark.asyncio
async def test_stop_all_processes_returns_zero_when_idle():
    """When no bg processes are tracked, stop_all_processes returns 0."""
    from extensions.coding_harness.tools.background import (
        count_running_processes,
        stop_all_processes,
    )
    # Idle baseline.
    assert count_running_processes() == 0
    killed = await stop_all_processes()
    assert killed == 0


# ─── dispatch integration ──────────────────────────────────────────


def test_dispatch_slash_routes_new_commands():
    """All 10 new slash commands route to a handler (not 'unknown')."""
    for name in (
        "config", "insights", "skills", "cron", "plugins",
        "profile", "image", "tools", "retry", "stop",
    ):
        ctx = _ctx()
        result = dispatch_slash(f"/{name}", ctx)
        assert result.handled, f"/{name} not handled"
