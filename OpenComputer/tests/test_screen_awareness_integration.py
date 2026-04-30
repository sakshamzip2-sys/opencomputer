"""End-to-end integration: run a real AgentLoop turn with screen-
awareness wired in. Assert the recording provider sees the
``<screen_context>`` overlay in its system prompt.

Mocks mss + OCR + lock-detect so the test is host-independent.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from extensions.screen_awareness.plugin import register as register_screen_awareness
from extensions.screen_awareness.state import ScreenAwarenessState, save_state

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage
from plugin_sdk.tool_contract import ToolSchema


class _RecordingProvider(BaseProvider):
    """Captures the system prompt of every complete() call."""

    name = "recording"
    default_model = "test"

    def __init__(self) -> None:
        self.captured_systems: list[str] = []

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stream: bool = False,
        runtime_extras: dict | None = None,
    ) -> ProviderResponse:
        self.captured_systems.append(system)
        return ProviderResponse(
            message=Message(role="assistant", content="ack"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    async def stream_complete(self, *args, **kwargs):
        raise NotImplementedError


def _mk_api(tmp_path: Path, injection_engine):
    """Lightweight PluginAPI shim that registers our plugin's
    injection provider into the SHARED engine singleton (so the
    AgentLoop's module-level injection_engine sees it).

    profile_home discovery still happens from tmp_path (state.json).
    """
    class _Api:
        def __init__(self) -> None:
            self.profile_home = tmp_path
            self.injection = injection_engine

        def register_tool(self, tool):
            # We don't need to actually register the tool for this test —
            # the assertion is on the system prompt, not on tool dispatch.
            pass

        def register_hook(self, spec):
            # Hooks aren't needed for this test (we pre-seed the ring
            # buffer directly to avoid timing flakes from fire-and-forget
            # captures).
            pass

        def register_injection_provider(self, provider):
            self.injection.register(provider)

    return _Api()


@pytest.mark.asyncio
async def test_user_turn_emits_screen_context_overlay(tmp_path: Path):
    """When screen_awareness is enabled and user submits a turn, the
    recorded provider's system prompt contains a <screen_context>
    overlay with the mocked OCR text."""
    from dataclasses import replace

    from opencomputer.agent.config import default_config
    from opencomputer.agent.injection import engine as global_injection_engine
    from opencomputer.agent.loop import AgentLoop

    # Snapshot the global injection engine's providers so we can clean
    # up after the test (don't pollute other tests).
    pre_existing_providers = set(global_injection_engine._providers.keys())  # noqa: SLF001

    # Enable the plugin via state file.
    save_state(tmp_path, ScreenAwarenessState(enabled=True))

    # Wire the plugin INTO THE GLOBAL singleton AgentLoop sees.
    api = _mk_api(tmp_path, global_injection_engine)
    register_screen_awareness(api)

    new_providers = [
        pid for pid in global_injection_engine._providers  # noqa: SLF001
        if pid not in pre_existing_providers
    ]
    assert "screen_context" in new_providers, (
        f"ScreenContextProvider not registered; new_providers={new_providers}"
    )

    # Pre-seed the ring buffer with a capture so the injection provider
    # has something to emit on the first turn (otherwise the sensor's
    # async fire-and-forget hook may not have completed by the time
    # the model is called — we sidestep timing by injecting directly).
    from extensions.screen_awareness.ring_buffer import (
        ScreenCapture,
        ScreenRingBuffer,
    )

    # Reach into the registered provider to access its ring.
    provider = global_injection_engine._providers["screen_context"]  # noqa: SLF001
    ring: ScreenRingBuffer = provider._ring  # noqa: SLF001 — test access
    import time as _time

    ring.append(ScreenCapture(
        captured_at=_time.time(),
        text="HELLO FROM MOCK SCREEN",
        sha256="mock_sha",
        trigger="user_message",
        session_id="s1",
    ))

    cfg = default_config()
    cfg = replace(
        cfg,
        memory=replace(
            cfg.memory,
            declarative_path=tmp_path / "MEMORY.md",
            skills_path=tmp_path / "skills",
        ),
        session=replace(cfg.session, db_path=tmp_path / "sessions.db"),
    )

    rec_provider = _RecordingProvider()
    loop = AgentLoop(provider=rec_provider, config=cfg)

    # Run a single user turn. The InjectionEngine queries our provider
    # which returns the <screen_context> overlay; AgentLoop appends it
    # to the system prompt fed to the recording provider.
    try:
        with mock.patch(
            "extensions.screen_awareness.ocr_inline.ocr_text_from_screen",
            return_value="HELLO FROM MOCK SCREEN",
        ):
            await loop.run_conversation("hello", session_id="s1")

        assert rec_provider.captured_systems, "provider was never called"
        sys_prompt = rec_provider.captured_systems[0]
        assert "<screen_context>" in sys_prompt, (
            f"<screen_context> missing from system prompt:\n{sys_prompt[:500]}"
        )
        assert "HELLO FROM MOCK SCREEN" in sys_prompt, (
            f"OCR text missing from <screen_context>:\n{sys_prompt[:500]}"
        )
    finally:
        # Cleanup — unregister our provider from the global singleton
        # so other tests don't see screen_context unexpectedly.
        global_injection_engine.unregister("screen_context")
