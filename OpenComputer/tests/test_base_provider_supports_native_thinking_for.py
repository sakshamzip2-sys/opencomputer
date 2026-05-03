"""BaseProvider.supports_native_thinking_for default impl + override
contract."""
from __future__ import annotations

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderCapabilities,
    ProviderResponse,
    Usage,
)


class _FakeProvider(BaseProvider):
    """Minimal BaseProvider concrete impl for testing the default."""

    def __init__(self, *, native: bool) -> None:
        self._native = native

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_native_thinking=self._native)

    async def complete(self, **kw):  # type: ignore[override]
        return ProviderResponse(
            message=Message(role="assistant", content=""),
            stop_reason="end_turn",
            usage=Usage(input_tokens=0, output_tokens=0),
        )

    async def stream_complete(self, **kw):  # type: ignore[override]
        # Empty async iterator.
        if False:
            yield None  # pragma: no cover


def test_default_impl_falls_back_to_capability_field_true():
    p = _FakeProvider(native=True)
    assert p.supports_native_thinking_for("any-model") is True
    assert p.supports_native_thinking_for("") is True


def test_default_impl_falls_back_to_capability_field_false():
    p = _FakeProvider(native=False)
    assert p.supports_native_thinking_for("any-model") is False


class _PerModelProvider(_FakeProvider):
    """Per-model override: True only for models starting with 'magic'."""

    def supports_native_thinking_for(self, model: str) -> bool:
        return model.lower().startswith("magic")


def test_subclass_can_override_for_per_model_decision():
    p = _PerModelProvider(native=False)  # capability=False, but override decides
    assert p.supports_native_thinking_for("magic-1") is True
    assert p.supports_native_thinking_for("magic-2") is True
    assert p.supports_native_thinking_for("regular-model") is False
