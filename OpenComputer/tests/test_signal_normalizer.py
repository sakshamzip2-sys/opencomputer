"""
Phase 3.A — :class:`plugin_sdk.ingestion.SignalNormalizer` behaviour.

Normalizers adapt raw inputs into typed :class:`SignalEvent` values so
that publishers can stay simple. Tests pin the registry + the identity
pass-through + the skip-semantics (returning ``None``).
"""

from __future__ import annotations

import pytest

from plugin_sdk.ingestion import (
    IdentityNormalizer,
    SignalEvent,
    SignalNormalizer,
    ToolCallEvent,
    clear_normalizers,
    get_normalizer,
    register_normalizer,
)


class _UppercaseToolNameNormalizer(SignalNormalizer):
    """Test-only normalizer that upper-cases any raw dict into a ToolCallEvent."""

    def normalize(self, raw):  # noqa: ANN001 — Any
        if not isinstance(raw, dict):
            return None
        return ToolCallEvent(
            tool_name=str(raw.get("tool_name", "")).upper(),
            duration_seconds=float(raw.get("duration", 0.0)),
        )


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the module-level normalizer registry between tests."""
    clear_normalizers()
    yield
    clear_normalizers()


def test_identity_normalizer_passes_through() -> None:
    """IdentityNormalizer returns a SignalEvent unchanged."""
    ident = IdentityNormalizer()
    evt = ToolCallEvent(tool_name="Read")
    assert ident.normalize(evt) is evt


def test_identity_normalizer_returns_none_for_non_events() -> None:
    """Anything that isn't a SignalEvent becomes None (skip)."""
    ident = IdentityNormalizer()
    assert ident.normalize("not an event") is None
    assert ident.normalize({"role": "user"}) is None
    assert ident.normalize(None) is None


def test_register_and_lookup_normalizer() -> None:
    """A registered normalizer is retrievable by event_type."""
    n = _UppercaseToolNameNormalizer()
    register_normalizer("tool_call", n)

    got = get_normalizer("tool_call")
    assert got is n

    # Retrieve-then-use round trip.
    evt = got.normalize({"tool_name": "read", "duration": 0.05})
    assert isinstance(evt, ToolCallEvent)
    assert evt.tool_name == "READ"


def test_register_normalizer_rejects_non_normalizer() -> None:
    """Passing a non-normalizer object raises TypeError."""
    with pytest.raises(TypeError):
        register_normalizer("tool_call", object())  # type: ignore[arg-type]


def test_lookup_returns_none_for_unknown_event_type() -> None:
    """Unknown event_type → None; callers expected to tolerate."""
    assert get_normalizer("never_registered") is None


def test_normalizer_returning_none_means_skip() -> None:
    """Normalizer returning None signals 'skip this raw input'.

    The contract is explicit in the SignalNormalizer docstring.
    """
    ident = IdentityNormalizer()
    # Any non-SignalEvent returns None.
    assert ident.normalize(42) is None


def test_signal_normalizer_is_abstract() -> None:
    """Subclasses must implement ``normalize`` — base class cannot instantiate."""
    with pytest.raises(TypeError):
        SignalNormalizer()  # type: ignore[abstract]


def test_register_normalizer_last_write_wins() -> None:
    """Re-registering the same event_type overwrites the prior entry."""

    class _A(SignalNormalizer):
        def normalize(self, raw):  # noqa: ANN001
            return None

    class _B(SignalNormalizer):
        def normalize(self, raw):  # noqa: ANN001
            if isinstance(raw, SignalEvent):
                return raw
            return None

    a = _A()
    b = _B()
    register_normalizer("tool_call", a)
    assert get_normalizer("tool_call") is a
    register_normalizer("tool_call", b)
    assert get_normalizer("tool_call") is b
