"""Capability struct defaults + per-provider declarations."""

from plugin_sdk import CacheTokens, ProviderCapabilities


def test_capabilities_defaults_are_safe():
    caps = ProviderCapabilities()
    assert caps.requires_reasoning_resend_in_tool_cycle is False
    assert caps.reasoning_block_kind is None
    assert caps.supports_long_ttl is False
    # Defaults must yield zero cache tokens for any synthetic usage object.
    assert caps.extracts_cache_tokens(object()) == CacheTokens(read=0, write=0)
    # Default min-cache-tokens is 0 (no filter).
    assert caps.min_cache_tokens("any-model") == 0


def test_cache_tokens_default_zero():
    ct = CacheTokens()
    assert ct.read == 0
    assert ct.write == 0


def test_cache_tokens_explicit_values():
    ct = CacheTokens(read=1234, write=200)
    assert ct.read == 1234
    assert ct.write == 200
