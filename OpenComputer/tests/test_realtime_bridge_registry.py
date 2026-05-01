"""Tests for the plugin-driven realtime-bridge registry on PluginAPI.

Covers:
* register_realtime_bridge(name, factory, env_var=..., audio_sink_kwargs=...)
  stores the registration under name
* get_realtime_bridge_registration(name) returns the full record (factory
  + metadata) or raises KeyError with helpful "available: [...]" message
* get_realtime_bridge_factory(name) is the factory-only convenience
* realtime_bridge_names() reports registered names sorted
* re-register overwrites the prior factory (matches register_channel
  semantics)
* audio_sink_kwargs is defensively copied — plugin mutation can't leak in
"""
from __future__ import annotations

import pytest


def _make_api():
    """Build a minimal PluginAPI for unit testing — no plugins loaded."""
    from opencomputer.plugins.loader import PluginAPI

    return PluginAPI(
        tool_registry=None,
        hook_engine=None,
        provider_registry={},
        channel_registry={},
    )


def test_register_and_get_realtime_bridge_factory() -> None:
    api = _make_api()

    sentinel = object()

    def factory(*, callbacks, api_key, model, voice, instructions, **_):
        return sentinel

    api.register_realtime_bridge("foo", factory)
    assert api.get_realtime_bridge_factory("foo") is factory
    # Calling the factory should hand back the sentinel — verifies the
    # signature shape callers commit to.
    out = factory(
        callbacks={}, api_key="k", model=None, voice="x", instructions=None,
    )
    assert out is sentinel


def test_register_with_metadata_round_trips() -> None:
    """env_var + audio_sink_kwargs are stored and surfaced via the registration."""
    api = _make_api()

    def factory(**_): return None

    api.register_realtime_bridge(
        "openai",
        factory,
        env_var="OPENAI_API_KEY",
    )
    api.register_realtime_bridge(
        "gemini",
        factory,
        env_var="GEMINI_API_KEY",
        audio_sink_kwargs={"output_sample_rate": 24_000},
    )

    openai_reg = api.get_realtime_bridge_registration("openai")
    assert openai_reg.factory is factory
    assert openai_reg.env_var == "OPENAI_API_KEY"
    assert openai_reg.audio_sink_kwargs == {}  # default empty

    gemini_reg = api.get_realtime_bridge_registration("gemini")
    assert gemini_reg.factory is factory
    assert gemini_reg.env_var == "GEMINI_API_KEY"
    assert gemini_reg.audio_sink_kwargs == {"output_sample_rate": 24_000}


def test_register_without_env_var_is_allowed() -> None:
    """Plugins that source credentials another way leave env_var=None."""
    api = _make_api()

    def factory(**_): return None

    api.register_realtime_bridge("custom", factory)  # no env_var, no kwargs
    reg = api.get_realtime_bridge_registration("custom")
    assert reg.env_var is None
    assert reg.audio_sink_kwargs == {}


def test_audio_sink_kwargs_defensively_copied_on_register() -> None:
    """Plugin mutation of its own kwargs dict must not leak into the registry."""
    api = _make_api()

    def factory(**_): return None

    plugin_kwargs = {"output_sample_rate": 24_000}
    api.register_realtime_bridge(
        "gemini", factory,
        env_var="GEMINI_API_KEY",
        audio_sink_kwargs=plugin_kwargs,
    )

    plugin_kwargs["output_sample_rate"] = 99_999  # plugin author goes off-script
    plugin_kwargs["sneaky"] = True

    reg = api.get_realtime_bridge_registration("gemini")
    assert reg.audio_sink_kwargs == {"output_sample_rate": 24_000}
    assert "sneaky" not in reg.audio_sink_kwargs


def test_registration_is_frozen() -> None:
    """The registration record is immutable after registration."""
    from dataclasses import FrozenInstanceError

    api = _make_api()
    api.register_realtime_bridge("openai", lambda **_: None, env_var="OPENAI_API_KEY")
    reg = api.get_realtime_bridge_registration("openai")

    with pytest.raises(FrozenInstanceError):
        reg.env_var = "OTHER"  # type: ignore[misc]


def test_realtime_bridge_names_sorted() -> None:
    api = _make_api()

    api.register_realtime_bridge("zeta", lambda **_: None)
    api.register_realtime_bridge("alpha", lambda **_: None)
    api.register_realtime_bridge("mu", lambda **_: None)

    assert api.realtime_bridge_names() == ["alpha", "mu", "zeta"]


def test_get_unknown_factory_raises_keyerror_with_available_list() -> None:
    """Both the factory-only convenience and the full registration getter
    surface the same helpful error when the name isn't registered."""
    api = _make_api()
    api.register_realtime_bridge("openai", lambda **_: None)
    api.register_realtime_bridge("gemini", lambda **_: None)

    for getter in (
        api.get_realtime_bridge_factory,
        api.get_realtime_bridge_registration,
    ):
        with pytest.raises(KeyError) as exc:
            getter("anthropic")
        msg = str(exc.value)
        assert "anthropic" in msg
        # Available names are surfaced so the caller knows what IS registered.
        assert "openai" in msg
        assert "gemini" in msg


def test_reregister_overwrites_prior_factory() -> None:
    api = _make_api()

    def f1(**_): return "v1"
    def f2(**_): return "v2"

    api.register_realtime_bridge("openai", f1)
    api.register_realtime_bridge("openai", f2)

    assert api.get_realtime_bridge_factory("openai") is f2
    assert api.realtime_bridge_names() == ["openai"]


def test_localaudioio_default_output_sample_rate_is_16k() -> None:
    """Default keeps backwards compat — OpenAI provider keeps working."""
    from opencomputer.voice.audio_io import LocalAudioIO

    # We don't need PortAudio just to read the field — but LocalAudioIO
    # raises in __init__ if sd is None. Skip if sounddevice can't load.
    try:
        a = LocalAudioIO(on_mic_chunk=lambda b: None)
    except RuntimeError as exc:
        pytest.skip(f"sounddevice unavailable: {exc}")

    assert a._output_sample_rate == 16_000


def test_localaudioio_accepts_custom_output_sample_rate() -> None:
    from opencomputer.voice.audio_io import LocalAudioIO

    try:
        a = LocalAudioIO(on_mic_chunk=lambda b: None, output_sample_rate=24_000)
    except RuntimeError as exc:
        pytest.skip(f"sounddevice unavailable: {exc}")

    assert a._output_sample_rate == 24_000


def test_localaudioio_rejects_zero_or_negative_rate() -> None:
    from opencomputer.voice.audio_io import LocalAudioIO

    with pytest.raises(ValueError, match="output_sample_rate"):
        LocalAudioIO(on_mic_chunk=lambda b: None, output_sample_rate=0)
    with pytest.raises(ValueError, match="output_sample_rate"):
        LocalAudioIO(on_mic_chunk=lambda b: None, output_sample_rate=-44100)
