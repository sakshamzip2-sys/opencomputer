"""Shift+Tab cycles permission modes; badge reflects current mode."""

from __future__ import annotations

from opencomputer.cli_ui.input_loop import (
    _cycle_permission_mode,
    _render_mode_badge,
)
from plugin_sdk import PermissionMode, RuntimeContext, effective_permission_mode


class TestShiftTabCycle:
    def test_default_to_accept_edits(self) -> None:
        rt = RuntimeContext()
        _cycle_permission_mode(rt)
        assert effective_permission_mode(rt) == PermissionMode.ACCEPT_EDITS

    def test_accept_edits_to_auto(self) -> None:
        rt = RuntimeContext(custom={"permission_mode": "accept-edits"})
        _cycle_permission_mode(rt)
        assert effective_permission_mode(rt) == PermissionMode.AUTO

    def test_auto_to_plan(self) -> None:
        rt = RuntimeContext(custom={"permission_mode": "auto"})
        _cycle_permission_mode(rt)
        assert effective_permission_mode(rt) == PermissionMode.PLAN

    def test_plan_back_to_default(self) -> None:
        rt = RuntimeContext(custom={"permission_mode": "plan"})
        _cycle_permission_mode(rt)
        assert effective_permission_mode(rt) == PermissionMode.DEFAULT

    def test_full_cycle_returns_to_default(self) -> None:
        rt = RuntimeContext()
        for _ in range(4):
            _cycle_permission_mode(rt)
        assert effective_permission_mode(rt) == PermissionMode.DEFAULT

    def test_cycle_clears_legacy_keys(self) -> None:
        # Going from auto → plan should NOT leave yolo_session=True hanging.
        rt = RuntimeContext(custom={"permission_mode": "auto", "yolo_session": True})
        _cycle_permission_mode(rt)  # → plan
        assert effective_permission_mode(rt) == PermissionMode.PLAN
        assert rt.custom.get("yolo_session") is False


class TestModeBadgeRender:
    def test_badge_shows_default(self) -> None:
        rt = RuntimeContext()
        ft = _render_mode_badge(rt)
        text = "".join(seg[1] for seg in ft)
        assert "default" in text
        assert "[D]" in text  # ASCII glyph for accessibility / NO_COLOR

    def test_badge_shows_accept_edits(self) -> None:
        rt = RuntimeContext(permission_mode=PermissionMode.ACCEPT_EDITS)
        text = "".join(seg[1] for seg in _render_mode_badge(rt))
        assert "accept-edits" in text
        assert "[E]" in text

    def test_badge_shows_auto(self) -> None:
        rt = RuntimeContext(permission_mode=PermissionMode.AUTO)
        text = "".join(seg[1] for seg in _render_mode_badge(rt))
        assert "auto" in text
        assert "[A]" in text

    def test_badge_shows_plan(self) -> None:
        rt = RuntimeContext(permission_mode=PermissionMode.PLAN)
        text = "".join(seg[1] for seg in _render_mode_badge(rt))
        assert "plan" in text
        assert "[P]" in text

    def test_badge_legend_includes_shift_tab(self) -> None:
        rt = RuntimeContext()
        text = "".join(seg[1] for seg in _render_mode_badge(rt))
        assert "Shift+Tab" in text

    def test_badge_collapses_when_no_runtime(self) -> None:
        assert _render_mode_badge(None) == []
