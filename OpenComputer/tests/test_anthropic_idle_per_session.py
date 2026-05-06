"""V2 — idle tracker is per-session, not per-provider-instance."""
from __future__ import annotations

import importlib.util
import sys
import time as _time_module
from pathlib import Path

import pytest


def _load_provider_module():
    repo = Path(__file__).resolve().parent.parent
    plugin_path = repo / "extensions" / "anthropic-provider" / "provider.py"
    name = "_anth_provider_idle_per_session_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, plugin_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    mod = _load_provider_module()
    return mod.AnthropicProvider()


def test_idle_tracker_is_dict_not_float(provider):
    """The idle tracker should be a dict keyed by session_id."""
    assert isinstance(provider._last_call_ts, dict)


def test_idle_tracker_isolated_per_session(provider, monkeypatch):
    """Two sessions must produce independent idle_seconds calculations."""
    fake_now = [1000.0]

    def _fake_monotonic():
        return fake_now[0]

    monkeypatch.setattr(_time_module, "monotonic", _fake_monotonic)

    # Session A first call at t=1000 — initial idle is 0 (no prior).
    idle_A_first = provider._record_call_get_idle("A")
    assert idle_A_first == 0.0
    fake_now[0] = 1010.0

    # Session B first call at t=1010 — also 0.
    idle_B_first = provider._record_call_get_idle("B")
    assert idle_B_first == 0.0
    fake_now[0] = 1310.0  # +300s

    # Session A second call: 1310 - 1000 = 310s.
    idle_A_second = provider._record_call_get_idle("A")
    assert idle_A_second == pytest.approx(310.0)

    # Session B second call from this same point: 1310 - 1010 = 300s.
    idle_B_second = provider._record_call_get_idle("B")
    assert idle_B_second == pytest.approx(300.0)


def test_idle_tracker_lru_bound(provider):
    """When the tracker exceeds the cap, the oldest-write entry is evicted."""
    cap = provider._last_call_ts_max
    # Populate cap+5 entries via the helper so we exercise the bound logic.
    fake_now = [1.0]

    def _fake_monotonic():
        return fake_now[0]

    import time as _time
    _orig = _time.monotonic
    _time.monotonic = _fake_monotonic
    try:
        for i in range(cap + 5):
            fake_now[0] = float(i + 1)
            provider._record_call_get_idle(f"sid-{i:04d}")
    finally:
        _time.monotonic = _orig

    assert len(provider._last_call_ts) == cap
    # Earliest entries should be gone
    assert "sid-0000" not in provider._last_call_ts
    assert "sid-0004" not in provider._last_call_ts
    # Most recent should be there
    assert f"sid-{cap + 4:04d}" in provider._last_call_ts


def test_idle_tracker_session_id_none_uses_default_key(provider):
    """``session_id=None`` falls back to a single default key."""
    assert provider._record_call_get_idle(None) == 0.0
    assert "_default" in provider._last_call_ts
