"""Idle-gap-aware TTL selection: 1h kicks in only when supported AND idle > 4min."""

from opencomputer.agent.prompt_caching import select_cache_ttl


def test_short_gap_default_ttl():
    assert select_cache_ttl(supports_long_ttl=True, idle_seconds=60.0) == "5m"


def test_long_gap_long_ttl_when_supported():
    assert select_cache_ttl(supports_long_ttl=True, idle_seconds=300.0) == "1h"


def test_long_gap_default_when_unsupported():
    assert select_cache_ttl(supports_long_ttl=False, idle_seconds=600.0) == "5m"


def test_threshold_boundary():
    # 4 minutes = 240s — exactly at threshold rounds DOWN to 5m to be conservative.
    assert select_cache_ttl(supports_long_ttl=True, idle_seconds=240.0) == "5m"
    assert select_cache_ttl(supports_long_ttl=True, idle_seconds=240.1) == "1h"


def test_negative_idle_falls_back_to_default():
    """Clock skew or reset → negative idle. Should not flip to 1h."""
    assert select_cache_ttl(supports_long_ttl=True, idle_seconds=-100.0) == "5m"


def test_zero_idle_first_turn():
    """First call (no previous turn) → idle=0 → default TTL."""
    assert select_cache_ttl(supports_long_ttl=True, idle_seconds=0.0) == "5m"


def test_anthropic_provider_passes_long_ttl_when_idle(monkeypatch):
    """End-to-end: Anthropic provider's _apply_cache_control with
    idle_seconds=600 produces a wire payload carrying ttl='1h'."""
    import importlib.util
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    mod_name = "_anth_idle_ttl_test"
    if mod_name in sys.modules:
        mod = sys.modules[mod_name]
    else:
        spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    provider = mod.AnthropicProvider()

    # A long enough message to clear the 4096-token threshold for Opus.
    big = "x" * (5 * 4096 * 4)
    anth_msgs = [{"role": "user", "content": big}]
    sys_for_sdk, msgs_for_sdk = provider._apply_cache_control(
        anth_msgs, system="", model="claude-opus-4-7", idle_seconds=600.0
    )
    payload = repr(msgs_for_sdk) + repr(sys_for_sdk)
    assert "'1h'" in payload or '"1h"' in payload, payload[:500]


def test_anthropic_provider_default_5m_when_short_gap(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    mod_name = "_anth_idle_ttl_test_short"
    if mod_name in sys.modules:
        mod = sys.modules[mod_name]
    else:
        spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    provider = mod.AnthropicProvider()

    big = "x" * (5 * 4096 * 4)
    anth_msgs = [{"role": "user", "content": big}]
    sys_for_sdk, msgs_for_sdk = provider._apply_cache_control(
        anth_msgs, system="", model="claude-opus-4-7", idle_seconds=30.0
    )
    payload = repr(msgs_for_sdk) + repr(sys_for_sdk)
    # No 1h marker should appear with a short gap.
    assert "'1h'" not in payload and '"1h"' not in payload
