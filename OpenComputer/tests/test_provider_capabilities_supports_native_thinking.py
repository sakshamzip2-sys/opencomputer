"""ProviderCapabilities exposes a backwards-compatible
supports_native_thinking flag (default False)."""
from __future__ import annotations

from plugin_sdk.provider_contract import ProviderCapabilities


def test_default_supports_native_thinking_is_false():
    """Conservative default — providers must opt in."""
    caps = ProviderCapabilities()
    assert caps.supports_native_thinking is False


def test_supports_native_thinking_can_be_set_true():
    caps = ProviderCapabilities(supports_native_thinking=True)
    assert caps.supports_native_thinking is True


def test_existing_capabilities_unaffected():
    """Backwards compat: existing fields keep their defaults when
    supports_native_thinking is the only argument."""
    caps = ProviderCapabilities(supports_native_thinking=True)
    assert caps.requires_reasoning_resend_in_tool_cycle is False
    assert caps.reasoning_block_kind is None
