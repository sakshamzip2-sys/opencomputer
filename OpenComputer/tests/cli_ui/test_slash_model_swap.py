"""Tests for /model mid-session swap (Sub-project C of model-agnosticism)."""
from __future__ import annotations

from unittest.mock import MagicMock

from opencomputer.cli_ui.slash_handlers import SlashContext, _handle_model


def test_handle_model_no_args_shows_current():
    """`/model` (no args) prints the current model + provider."""
    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.config = MagicMock()
    ctx.config.model.model = "claude-opus-4-7"
    ctx.config.model.provider = "anthropic"

    result = _handle_model(ctx, [])
    assert result.handled is True
    arg = ctx.console.print.call_args.args[0]
    assert "claude-opus-4-7" in arg
    assert "anthropic" in arg


def test_handle_model_with_arg_calls_on_model_swap():
    captured: dict = {}

    def _swap(m: str) -> tuple[bool, str]:
        captured["model"] = m
        return (True, m)

    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.on_model_swap = _swap

    result = _handle_model(ctx, ["claude-haiku-4-5-20251001"])
    assert result.handled is True
    assert captured["model"] == "claude-haiku-4-5-20251001"
    arg = ctx.console.print.call_args.args[0]
    assert "model →" in arg
    assert "claude-haiku-4-5-20251001" in arg


def test_handle_model_swap_failure_echoes_reason():
    ctx = MagicMock(spec=SlashContext)
    ctx.console = MagicMock()
    ctx.on_model_swap = lambda m: (False, f"unknown model {m!r}")

    _handle_model(ctx, ["does-not-exist"])
    arg = ctx.console.print.call_args.args[0]
    assert "swap failed" in arg
    assert "does-not-exist" in arg


def test_default_on_model_swap_returns_not_wired_message():
    """When SlashContext is built without overriding on_model_swap, the
    default returns a clear 'not wired' message rather than silently
    succeeding."""
    from rich.console import Console

    ctx = SlashContext(
        console=Console(),
        session_id="t",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {"in": 0, "out": 0},
        get_session_list=lambda: [],
    )
    ok, msg = ctx.on_model_swap("any-model")
    assert ok is False
    assert "not wired" in msg


# ─── No-args display reads from the live getter ─────────────────────────


class TestNoArgsDisplayReadsLiveModel:
    """Regression: 2026-05-11 — ``/model`` (no args) was reading
    ``ctx.config.model.model``, which is the FROZEN session-start
    snapshot. After a successful ``/model <id>`` swap the displayed
    model lagged the actual model until the session restarted —
    indistinguishable from "swap silently failed" to users.

    The fix routes the no-args read through
    ``ctx.get_active_model_info`` which production wires to
    ``loop.config.model.*`` (live).
    """

    def test_handle_model_no_args_reads_getter_when_wired(self) -> None:
        """When the getter is wired, its return value wins over
        ``ctx.config`` (which is the stale frozen snapshot)."""
        from rich.console import Console

        from opencomputer.cli_ui.slash_handlers import (
            SlashContext,
            _handle_model,
        )

        # config says sonnet (the OLD model — pre-swap value).
        stale_cfg = MagicMock()
        stale_cfg.model.model = "claude-sonnet-4-6"
        stale_cfg.model.provider = "anthropic"

        captured: list[str] = []

        class _Recording(Console):
            def print(self, *args: object, **_kw: object) -> None:  # type: ignore[override]
                captured.append(" ".join(str(a) for a in args))

        ctx = SlashContext(
            console=_Recording(),
            session_id="t",
            config=stale_cfg,
            on_clear=lambda: None,
            get_cost_summary=lambda: {"in": 0, "out": 0},
            get_session_list=lambda: [],
            # Live getter says opus (post-swap value).
            get_active_model_info=lambda: ("claude-opus-4-7", "anthropic"),
        )
        result = _handle_model(ctx, [])
        assert result.handled is True
        assert len(captured) == 1
        # The post-swap value is displayed, NOT the pre-swap stale one.
        assert "claude-opus-4-7" in captured[0]
        assert "claude-sonnet-4-6" not in captured[0]

    def test_handle_model_no_args_falls_back_to_config_when_getter_default(
        self,
    ) -> None:
        """SlashContexts built without wiring the getter (test fixtures,
        ACP one-shot dispatch) fall back to ``ctx.config`` — keeps the
        no-args read useful even when the live source isn't available."""
        from rich.console import Console

        from opencomputer.cli_ui.slash_handlers import (
            SlashContext,
            _handle_model,
        )

        cfg = MagicMock()
        cfg.model.model = "claude-sonnet-4-6"
        cfg.model.provider = "anthropic"

        captured: list[str] = []

        class _Recording(Console):
            def print(self, *args: object, **_kw: object) -> None:  # type: ignore[override]
                captured.append(" ".join(str(a) for a in args))

        ctx = SlashContext(
            console=_Recording(),
            session_id="t",
            config=cfg,
            on_clear=lambda: None,
            get_cost_summary=lambda: {"in": 0, "out": 0},
            get_session_list=lambda: [],
            # NOTE: get_active_model_info NOT passed — default returns ("","")
        )
        _handle_model(ctx, [])
        # Falls back to ctx.config — old session-start value still shown.
        assert any("claude-sonnet-4-6" in c for c in captured)

    def test_handle_model_no_args_handles_getter_exception_gracefully(
        self,
    ) -> None:
        """An adversarial getter that raises must NOT crash the slash
        render — it falls back to ``ctx.config``."""
        from rich.console import Console

        from opencomputer.cli_ui.slash_handlers import (
            SlashContext,
            _handle_model,
        )

        cfg = MagicMock()
        cfg.model.model = "claude-sonnet-4-6"
        cfg.model.provider = "anthropic"

        def _broken() -> tuple[str, str]:
            raise RuntimeError("loop went away")

        captured: list[str] = []

        class _Recording(Console):
            def print(self, *args: object, **_kw: object) -> None:  # type: ignore[override]
                captured.append(" ".join(str(a) for a in args))

        ctx = SlashContext(
            console=_Recording(),
            session_id="t",
            config=cfg,
            on_clear=lambda: None,
            get_cost_summary=lambda: {"in": 0, "out": 0},
            get_session_list=lambda: [],
            get_active_model_info=_broken,
        )
        # Must not raise.
        result = _handle_model(ctx, [])
        assert result.handled is True
        # And the fallback path painted SOMETHING from the stale config
        # rather than crashing the render.
        assert any("claude-sonnet-4-6" in c for c in captured)

    def test_handle_config_model_row_uses_live_getter(self) -> None:
        """``/config`` was the third stale-display surface — the
        ``model:`` row read ``cfg.model.{provider,model}`` directly
        and lied after every swap. Locked down here so a future
        refactor that "simplifies" /config can't reintroduce drift."""
        from rich.console import Console

        from opencomputer.cli_ui.slash_handlers import (
            SlashContext,
            _handle_config,
        )

        stale_cfg = MagicMock()
        stale_cfg.model.provider = "anthropic"
        stale_cfg.model.model = "claude-sonnet-4-6"
        stale_cfg.model.cheap_model = "claude-haiku-4-5"
        stale_cfg.model.max_tokens = 4096
        stale_cfg.model.temperature = 0.0
        stale_cfg.session.db_path = "/tmp/db.sqlite"
        stale_cfg.memory.declarative_path = "/tmp/mem.md"

        captured: list[str] = []

        class _Recording(Console):
            def print(self, *args: object, **_kw: object) -> None:  # type: ignore[override]
                captured.append(" ".join(str(a) for a in args))

        ctx = SlashContext(
            console=_Recording(),
            session_id="t",
            config=stale_cfg,
            on_clear=lambda: None,
            get_cost_summary=lambda: {"in": 0, "out": 0},
            get_session_list=lambda: [],
            # Live getter: model already swapped to opus.
            get_active_model_info=lambda: ("claude-opus-4-7", "anthropic"),
        )
        _handle_config(ctx, [])
        blob = "\n".join(captured)
        # The active model row reflects the LIVE state (opus), not the
        # stale config snapshot (sonnet).
        assert "claude-opus-4-7" in blob
        assert "claude-sonnet-4-6" not in blob, (
            f"/config still printing stale model: {blob!r}"
        )
        # Other rows still read from ctx.config — those fields aren't
        # swappable mid-session (yet).
        assert "claude-haiku-4-5" in blob  # cheap_model row

    def test_handle_model_no_args_handles_non_tuple_getter_return(self) -> None:
        """Adversarial getter that returns garbage (single int, list,
        wrong-arity tuple) must fall back to ctx.config instead of
        crashing on unpack."""
        from rich.console import Console

        from opencomputer.cli_ui.slash_handlers import (
            SlashContext,
            _handle_model,
        )

        cfg = MagicMock()
        cfg.model.model = "claude-sonnet-4-6"
        cfg.model.provider = "anthropic"

        # Factory keeps the captured-list closure bound at call time —
        # otherwise ruff B023 fires because the class body's reference
        # to ``captured`` would resolve via the iteration variable.
        def _make_recording(buf: list[str]) -> Console:
            class _Recording(Console):
                def print(self, *args: object, **_kw: object) -> None:  # type: ignore[override]
                    buf.append(" ".join(str(a) for a in args))

            return _Recording()

        for garbage in (42, [], (None, None), ("", ""), "claude-opus-4-7"):
            captured: list[str] = []
            ctx = SlashContext(
                console=_make_recording(captured),
                session_id="t",
                config=cfg,
                on_clear=lambda: None,
                get_cost_summary=lambda: {"in": 0, "out": 0},
                get_session_list=lambda: [],
                get_active_model_info=lambda g=garbage: g,  # type: ignore[arg-type,return-value]
            )
            result = _handle_model(ctx, [])
            assert result.handled is True
            assert any(
                "claude-sonnet-4-6" in c for c in captured
            ), f"fallback failed for garbage={garbage!r}"
