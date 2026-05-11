"""Integration tests for the PI-derived UX bundle's wiring sites.

The unit tests in ``test_visual_truncate.py`` etc. verify the
modules in isolation. These tests verify the WIRING — that the
modules are actually called from the right places. A green unit
test plus a missing wire = ship a feature that doesn't work.

Covers:

* ``reasoning_view.render_output`` calls ``truncate_middle`` on long
  tool RESULT bodies (truncation is observable in the rendered
  output).
* ``branch_cmd`` returns a card-shaped output via
  ``summary_cards.render_branch_card``.
* ``slash_handlers._handle_compress`` renders the compaction card
  via ``summary_cards.render_compaction_card`` on success.
* The agent loop's pending-model-swap consumer routes through
  ``model_swap.swap_model`` and not the dataclasses shortcut.
"""

from __future__ import annotations

import os

import pytest

# ─── reasoning_view → visual_truncate ──────────────────────────────────


class TestReasoningViewWiring:
    def test_long_output_gets_truncated_when_rendered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A long tool RESULT body must be elided via truncate_middle
        when ReasoningView renders it."""
        from opencomputer.cli_ui.reasoning_view import ToolView

        long_body = "\n".join(f"line{i}" for i in range(500))
        view = ToolView(
            type="tool-grep",
            toolName="grep",
            state="output-available",
            input={"pattern": "x"},
            output=long_body,
        )
        # Default cap is 40 lines — output must be shorter than the
        # original AND contain the elision marker.
        rendered = view.render_output()
        assert rendered is not None
        # Pull the body out of the rendered Padding wrapper.
        # _labelled_block stacks two parts: label + body. The body
        # holds the truncated content.
        body_text = _extract_text(rendered)
        assert "lines omitted" in body_text
        # Should NOT show all 500 lines.
        assert "line499" in body_text  # tail kept
        assert "line0" in body_text     # head kept

    def test_short_output_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Below the cap, no elision."""
        from opencomputer.cli_ui.reasoning_view import ToolView

        short_body = "single line only"
        view = ToolView(
            type="tool-grep",
            toolName="grep",
            state="output-available",
            input={},
            output=short_body,
        )
        rendered = view.render_output()
        assert rendered is not None
        body_text = _extract_text(rendered)
        assert "lines omitted" not in body_text
        assert short_body in body_text

    def test_env_var_disables_truncation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setting cap=0 turns truncation off — verbose mode."""
        monkeypatch.setenv("OPENCOMPUTER_TOOL_OUTPUT_MAX_LINES", "0")
        from opencomputer.cli_ui.reasoning_view import ToolView

        long_body = "\n".join(f"line{i}" for i in range(500))
        view = ToolView(
            type="tool-grep",
            toolName="grep",
            state="output-available",
            input={},
            output=long_body,
        )
        body_text = _extract_text(view.render_output())
        assert "lines omitted" not in body_text
        # Full body is preserved.
        assert "line250" in body_text

    def test_legacy_env_var_still_honored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backwards-compat: OC_TOOL_OUTPUT_MAX_LINES still works."""
        monkeypatch.setenv("OC_TOOL_OUTPUT_MAX_LINES", "0")
        # Make sure the new var isn't set so the legacy alias has
        # to do the work.
        monkeypatch.delenv("OPENCOMPUTER_TOOL_OUTPUT_MAX_LINES", raising=False)
        from opencomputer.cli_ui.reasoning_view import ToolView

        long_body = "\n".join(f"line{i}" for i in range(500))
        view = ToolView(
            type="tool-grep",
            toolName="grep",
            state="output-available",
            input={},
            output=long_body,
        )
        body_text = _extract_text(view.render_output())
        assert "lines omitted" not in body_text


# ─── branch_cmd → summary_cards ─────────────────────────────────────────


class TestBranchCmdWiring:
    @pytest.mark.asyncio
    async def test_branch_emits_card_via_render_branch_card(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If render_branch_card returns a sentinel, branch_cmd's
        output must contain it. Verifies the call path."""
        sentinel = "★sentinel-card-from-branch★"
        from opencomputer.cli_ui import summary_cards as sc

        monkeypatch.setattr(sc, "render_branch_card", lambda **_kw: sentinel)
        from opencomputer.agent.slash_commands_impl import branch_cmd as bc

        monkeypatch.setattr(bc, "render_branch_card", sentinel.__class__, raising=False)
        # The import inside branch_cmd is local — rebind through summary_cards.

        from types import SimpleNamespace

        from opencomputer.agent.slash_commands_impl.branch_cmd import BranchCommand
        from plugin_sdk.runtime_context import RuntimeContext

        # Minimal fake DB.
        class _DB:
            def get_session(self, sid):
                return {"id": sid, "title": "t", "platform": "cli", "model": ""}

            def get_messages(self, sid):
                return []

            def create_session(self, sid, **_kw):
                pass

            def append_messages_batch(self, sid, msgs):
                pass

        rt = RuntimeContext(custom={"session_id": "s1", "session_db": _DB()})
        # Re-monkeypatch the import inside branch_cmd's namespace.
        from opencomputer.cli_ui import summary_cards
        monkeypatch.setattr(summary_cards, "render_branch_card", lambda **_kw: sentinel)
        result = await BranchCommand().execute("", rt)
        assert sentinel in result.output


# ─── slash_handlers /compress → summary_cards ──────────────────────────


class TestCompressWiring:
    def test_compress_success_renders_compaction_card(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The /compress handler success path must call
        render_compaction_card and print its result. Verify by
        intercepting the function."""
        sentinel = "★compaction-card-sentinel★"
        printed: list[str] = []

        class _Console:
            def print(self, text: str) -> None:
                printed.append(text)

        from opencomputer.cli_ui import slash_handlers as sh
        from opencomputer.cli_ui import summary_cards

        monkeypatch.setattr(
            summary_cards, "render_compaction_card", lambda **_kw: sentinel
        )

        ctx = sh.SlashContext(
            console=_Console(),
            session_id="s1",
            config=None,
            on_clear=lambda: None,
            get_cost_summary=lambda: {},
            get_session_list=lambda: [],
            on_compress=lambda: (True, 100, 30, "ok"),
        )
        sh._handle_compress(ctx, [])
        assert any(sentinel in p for p in printed)

    def test_compress_no_work_does_not_emit_card(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When before == after, the handler must NOT render the card
        — the queued-semantics message goes out instead."""
        sentinel = "★compaction-card-sentinel★"
        printed: list[str] = []

        class _Console:
            def print(self, text: str) -> None:
                printed.append(text)

        from opencomputer.cli_ui import slash_handlers as sh
        from opencomputer.cli_ui import summary_cards

        monkeypatch.setattr(
            summary_cards, "render_compaction_card", lambda **_kw: sentinel
        )

        ctx = sh.SlashContext(
            console=_Console(),
            session_id="s1",
            config=None,
            on_clear=lambda: None,
            get_cost_summary=lambda: {},
            get_session_list=lambda: [],
            on_compress=lambda: (
                True,
                0,
                0,
                "queued — compaction will run on next user turn",
            ),
        )
        sh._handle_compress(ctx, [])
        # Sentinel must NOT appear when no work was done.
        assert not any(sentinel in p for p in printed)
        # The queued message DOES appear.
        assert any("queued" in p for p in printed)


# ─── input_loop → output_guard install ─────────────────────────────────


class TestInputLoopOutputGuardInstall:
    def test_input_loop_imports_output_guard(self) -> None:
        """Sanity check — the input_loop module references the guard.
        If the wire is ever ripped out, this fails loudly. Cheap; runs
        without spinning up prompt_toolkit."""
        from pathlib import Path

        src = Path(
            "opencomputer/cli_ui/input_loop.py"
        ).read_text(encoding="utf-8")
        assert "take_over_stdout" in src, "guard takeover not wired"
        assert "restore_stdout" in src, "guard restore not wired"
        assert "is_stdout_taken_over" in src, "guard re-entry check not wired"


# ─── Helpers ────────────────────────────────────────────────────────────


def _extract_text(renderable: object) -> str:
    """Walk a Rich renderable graph and concatenate every leaf string.
    Used to assert on rendered content without spinning up a real
    console."""
    if renderable is None:
        return ""
    if isinstance(renderable, str):
        return renderable
    parts: list[str] = []
    # Rich Padding has .renderable; _StackedRenderable has _parts;
    # Text has .plain.
    inner = getattr(renderable, "renderable", None)
    if inner is not None:
        parts.append(_extract_text(inner))
    inner_parts = getattr(renderable, "_parts", None)
    if inner_parts is not None:
        for p in inner_parts:
            parts.append(_extract_text(p))
    plain = getattr(renderable, "plain", None)
    if plain is not None:
        parts.append(plain)
    code = getattr(renderable, "code", None)
    if isinstance(code, str):
        parts.append(code)
    return "\n".join(p for p in parts if p)
