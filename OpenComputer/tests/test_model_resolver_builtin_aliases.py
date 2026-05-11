"""Tests for built-in short-name aliases + strict-mode rejection.

Regression: 2026-05-11 — Saksham typed ``/model opus`` in oc and got::

    error: NotFoundError: Error code: 404
    'message': 'model: opus'

Root cause: ``resolve_model("opus", {})`` returned the literal ``"opus"``
because the default ``model_aliases`` was empty. The swap "succeeded",
``loop.config.model.model`` became ``"opus"``, and the next API call
forwarded that string to Anthropic which (correctly) 404'd. The status
bar dutifully showed ``"opus"`` as the active model — visible to the
user as "swap did SOMETHING but every call breaks."

Two fixes lock this down:

1. **Built-in short aliases** for the canonical Anthropic families so
   ``opus`` / ``sonnet`` / ``haiku`` resolve out of the box without
   requiring the user to configure ``model_aliases`` in config.yaml.

2. **Rejection of bare-short unknowns.** A name like ``"opuse"`` (typo)
   that's not in any alias map AND doesn't contain ``-`` / ``/`` /
   ``:`` is almost certainly a typo or a swap target the user expected
   the resolver to recognise. Raising loudly turns a silent 404 into
   a slash-command error the user can SEE and correct.

User-defined aliases STILL win over builtins so power users can remap
``opus`` to whatever endpoint they want.
"""
from __future__ import annotations

import pytest

from opencomputer.agent.model_resolver import (
    _BUILTIN_SHORT_ALIASES,
    resolve_model,
)


class TestBuiltinShortAliases:
    def test_opus_resolves_without_user_config(self) -> None:
        """The original bug. /model opus from a vanilla user must work."""
        assert resolve_model("opus", {}) == "claude-opus-4-7"

    def test_sonnet_resolves_without_user_config(self) -> None:
        assert resolve_model("sonnet", {}) == "claude-sonnet-4-6"

    def test_haiku_resolves_without_user_config(self) -> None:
        assert resolve_model("haiku", {}) == "claude-haiku-4-5"

    def test_none_aliases_falls_back_to_builtins(self) -> None:
        """A ``None`` aliases arg (legacy callers) still gets the builtin
        fallback. Pre-fix this short-circuited at the very top of
        ``resolve_model`` and returned the input unchanged."""
        assert resolve_model("opus", None) == "claude-opus-4-7"

    def test_builtin_table_pinned_to_current_canonical_ids(self) -> None:
        """Locks the table contents so anyone who edits the file does
        so deliberately. If you bump Anthropic's canonical id (e.g.
        claude-opus-4-7 → claude-opus-4-8), update BOTH the table
        AND this test together."""
        assert _BUILTIN_SHORT_ALIASES == {
            "opus": "claude-opus-4-7",
            "sonnet": "claude-sonnet-4-6",
            "haiku": "claude-haiku-4-5",
        }


class TestUserAliasesWinOverBuiltins:
    def test_user_alias_overrides_builtin_opus(self) -> None:
        """A power user who maps ``opus`` to a custom endpoint gets
        their endpoint, not the canonical Anthropic id."""
        out = resolve_model(
            "opus",
            {"opus": "openrouter/anthropic/claude-opus-4-7:nitro"},
        )
        assert out == "openrouter/anthropic/claude-opus-4-7:nitro"

    def test_user_alias_chain_through_builtin_target(self) -> None:
        """User alias points to a short name that the builtin map can
        resolve further. The chain follows: foo → opus → claude-opus-4-7."""
        out = resolve_model("foo", {"foo": "opus"})
        assert out == "claude-opus-4-7"


class TestUnknownShortNamesRejected:
    """Bare-short rejection lives behind ``strict=True`` so the user-facing
    swap path catches typos while the hot path in
    ``AgentLoop._call_provider`` (loop.py:4524) keeps the legacy lenient
    behavior — test stubs and third-party model ids need to pass through
    unchanged.
    """

    def test_strict_bare_short_typo_raises(self) -> None:
        """``/model opuse`` (typo) used to silently store ``"opuse"`` and
        404 on the next API call. swap_model now calls resolve_model
        with strict=True so the slash handler surfaces the error
        before persisting garbage."""
        with pytest.raises(ValueError, match="unknown model alias 'opuse'"):
            resolve_model("opuse", {}, strict=True)

    def test_non_strict_bare_short_passes_through(self) -> None:
        """Lenient mode (default) preserves the legacy behavior: bare
        short names pass through unchanged. This matters because
        loop.py:_call_provider runs resolve_model on every turn against
        whatever's in loop.config.model.model — test stubs use ``mock``,
        CI uses synthetic ids, and third-party plugins may register
        unknown short names that the loop must forward to their
        provider verbatim."""
        assert resolve_model("mock", {}) == "mock"
        assert resolve_model("custom-stub", {}) == "custom-stub"

    def test_strict_rejected_message_lists_known_short_names(self) -> None:
        """Error message must tell the user what their options ARE,
        not just that their input was bad. UX nicety: shows the
        built-in list sorted so they can scan it."""
        with pytest.raises(ValueError) as exc_info:
            resolve_model("opuse", {}, strict=True)
        msg = str(exc_info.value)
        assert "haiku" in msg
        assert "opus" in msg
        assert "sonnet" in msg
        # Also points at the escape hatch — a full id always works.
        assert "claude-opus-4-7" in msg

    def test_empty_string_raises_distinctly(self) -> None:
        """Whitespace-only / empty input is its own kind of bad — raised
        BEFORE the strict-mode branch so it fires regardless of mode.
        The swap callsite already strips whitespace before calling,
        so this is mostly defense for direct callers."""
        with pytest.raises(ValueError, match="non-empty string"):
            resolve_model("", {})

    def test_non_string_input_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            resolve_model(None, {})  # type: ignore[arg-type]


class TestRealLookingIdsPassThrough:
    """Strings that LOOK like model ids (contain separators) pass through
    unchanged. This preserves legacy behavior for users with custom or
    third-party models the builtin table doesn't know about."""

    def test_full_anthropic_id_passes_through(self) -> None:
        assert resolve_model("claude-opus-4-7", {}) == "claude-opus-4-7"

    def test_unknown_dashy_id_passes_through(self) -> None:
        """User has a custom model id we don't recognise; let it through
        rather than blocking on a list we can't keep up to date."""
        assert (
            resolve_model("totally-not-real-but-has-dashes", {})
            == "totally-not-real-but-has-dashes"
        )

    def test_openrouter_slash_id_passes_through(self) -> None:
        assert (
            resolve_model("openrouter/anthropic/claude-opus-4-7", {})
            == "openrouter/anthropic/claude-opus-4-7"
        )

    def test_nitro_suffix_id_passes_through(self) -> None:
        """``:nitro`` suffix is OpenRouter routing sugar — passes
        through here; ``swap_model`` strips it later if the active
        provider isn't OR."""
        assert (
            resolve_model("claude-opus-4-7:nitro", {})
            == "claude-opus-4-7:nitro"
        )


class TestSwapModelIntegrationWithRealResolver:
    """End-to-end: confirm the original bug (``/model opus`` → 404)
    can't happen anymore. Exercises the resolver THROUGH swap_model
    without monkeypatching the resolver — the other tests in
    test_model_swap_helper.py stub the resolver, which is fine for
    THEIR swap-mechanics scope but doesn't catch this class of bug.
    """

    def test_swap_opus_resolves_canonical(self) -> None:
        """The bug: ``/model opus`` from a vanilla config must end up
        with ``loop.config.model.model == 'claude-opus-4-7'``, NOT
        the literal string ``'opus'``."""
        import dataclasses
        from types import SimpleNamespace

        from opencomputer.agent.model_swap import swap_model

        @dataclasses.dataclass(frozen=True)
        class _MC:
            model: str
            provider: str = "anthropic"
            model_aliases: dict = dataclasses.field(default_factory=dict)

        @dataclasses.dataclass(frozen=True)
        class _C:
            model: _MC

        class _P:
            def supports_native_thinking_for(self, _m: str) -> bool:
                return True

        loop = SimpleNamespace(
            config=_C(model=_MC(model="claude-sonnet-4-6")),
            provider=_P(),
        )
        runtime = SimpleNamespace(custom={})
        ok, msg = swap_model(
            loop=loop, runtime=runtime, new_model="opus"
        )
        assert ok is True, msg
        # The canonical id, NOT the literal short alias.
        assert loop.config.model.model == "claude-opus-4-7"
        # And the runtime cache reflects it.
        assert runtime.custom["model_id"] == "claude-opus-4-7"
        assert runtime.custom["active_model_id"] == "claude-opus-4-7"

    def test_swap_opuse_typo_refuses_loudly(self) -> None:
        """The other half of the fix: typos must not be persisted as
        ``loop.config.model.model``. Pre-fix the resolver returned
        the literal typo, swap_model applied it, and the user saw
        a 404 on the next message. Now: swap_model returns
        ``(False, msg)`` and ``loop.config`` is untouched."""
        import dataclasses
        from types import SimpleNamespace

        from opencomputer.agent.model_swap import swap_model

        @dataclasses.dataclass(frozen=True)
        class _MC:
            model: str
            provider: str = "anthropic"
            model_aliases: dict = dataclasses.field(default_factory=dict)

        @dataclasses.dataclass(frozen=True)
        class _C:
            model: _MC

        class _P:
            def supports_native_thinking_for(self, _m: str) -> bool:
                return True

        loop = SimpleNamespace(
            config=_C(model=_MC(model="claude-sonnet-4-6")),
            provider=_P(),
        )
        runtime = SimpleNamespace(custom={})
        ok, msg = swap_model(
            loop=loop, runtime=runtime, new_model="opuse"
        )
        assert ok is False
        assert "opuse" in msg
        # loop.config UNTOUCHED — the old model is still active.
        assert loop.config.model.model == "claude-sonnet-4-6"
        # Cache UNTOUCHED — nothing got polluted by the bad swap.
        assert "model_id" not in runtime.custom
