"""T65 + T66 + T67 — Honcho-doc tool param uplift.

T65: ``honcho_reasoning`` accepts ``dialectic_depth`` (multi-pass).
T66: ``honcho_conclude`` accepts ``observation_mode`` (explicit/inferred/hypothetical).
T67: peer override, mode, tokens, identity rounded out across tools.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import ToolCall


def _load_provider_mod():
    if "memory_honcho_provider_test" in sys.modules:
        return sys.modules["memory_honcho_provider_test"]
    spec_path = (
        Path(__file__).parent.parent
        / "extensions"
        / "memory-honcho"
        / "provider.py"
    )
    spec = importlib.util.spec_from_file_location(
        "memory_honcho_provider_test", spec_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_honcho_provider_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _provider_with_mock():
    mod = _load_provider_mod()
    cfg = mod.HonchoConfig(
        workspace="ws-test",
        host_key="host-test",
        base_url="http://x",
    )
    p = mod.HonchoSelfHostedProvider(cfg)
    fake = MagicMock()
    fake.is_closed = False
    fake.get = AsyncMock(return_value=MagicMock(json=lambda: {"context": "ok"}, raise_for_status=lambda: None))
    fake.post = AsyncMock(return_value=MagicMock(json=lambda: {"answer": "ok"}, raise_for_status=lambda: None))
    p._client = fake
    return p, fake


@pytest.mark.asyncio
async def test_reasoning_passes_dialectic_depth():
    p, fake = _provider_with_mock()
    call = ToolCall(
        id="x", name="honcho_reasoning",
        arguments={"query": "what does user want", "dialectic_depth": 3},
    )
    await p.handle_tool_call(call)
    body = fake.post.call_args.kwargs["json"]
    assert body["dialectic_depth"] == 3


@pytest.mark.asyncio
async def test_reasoning_default_dialectic_depth_is_one():
    """Backwards compat: omitting the param keeps single-pass behavior."""
    p, fake = _provider_with_mock()
    call = ToolCall(id="x", name="honcho_reasoning", arguments={"query": "q"})
    await p.handle_tool_call(call)
    body = fake.post.call_args.kwargs["json"]
    assert body["dialectic_depth"] == 1


@pytest.mark.asyncio
async def test_reasoning_dialectic_depth_capped():
    """Cap at 5 to prevent runaway multi-pass costs."""
    p, fake = _provider_with_mock()
    call = ToolCall(id="x", name="honcho_reasoning", arguments={"query": "q", "dialectic_depth": 99})
    await p.handle_tool_call(call)
    body = fake.post.call_args.kwargs["json"]
    assert body["dialectic_depth"] == 5


@pytest.mark.asyncio
async def test_conclude_accepts_observation_mode():
    p, fake = _provider_with_mock()
    call = ToolCall(
        id="x", name="honcho_conclude",
        arguments={"fact": "user prefers concise", "observation_mode": "inferred"},
    )
    await p.handle_tool_call(call)
    body = fake.post.call_args.kwargs["json"]
    assert body["observation_mode"] == "inferred"


@pytest.mark.asyncio
async def test_conclude_invalid_observation_mode_falls_back_to_explicit():
    p, fake = _provider_with_mock()
    call = ToolCall(
        id="x", name="honcho_conclude",
        arguments={"fact": "x", "observation_mode": "garbage"},
    )
    await p.handle_tool_call(call)
    body = fake.post.call_args.kwargs["json"]
    assert body["observation_mode"] == "explicit"


@pytest.mark.asyncio
async def test_search_accepts_identity_param():
    p, fake = _provider_with_mock()
    call = ToolCall(
        id="x", name="honcho_search",
        arguments={"query": "q", "identity": "alice@org"},
    )
    await p.handle_tool_call(call)
    body = fake.post.call_args.kwargs["json"]
    assert body["identity"] == "alice@org"


@pytest.mark.asyncio
async def test_reasoning_accepts_mode_and_tokens():
    p, fake = _provider_with_mock()
    call = ToolCall(
        id="x", name="honcho_reasoning",
        arguments={"query": "q", "mode": "concise", "max_tokens": 400},
    )
    await p.handle_tool_call(call)
    body = fake.post.call_args.kwargs["json"]
    assert body["mode"] == "concise"
    assert body["max_tokens"] == 400


@pytest.mark.asyncio
async def test_schemas_advertise_new_params():
    p, _ = _provider_with_mock()
    schemas = p.tool_schemas()
    by_name = {s.name: s for s in schemas}

    reasoning_props = by_name["honcho_reasoning"].parameters["properties"]
    assert "dialectic_depth" in reasoning_props
    assert "mode" in reasoning_props
    assert "max_tokens" in reasoning_props

    conclude_props = by_name["honcho_conclude"].parameters["properties"]
    assert "observation_mode" in conclude_props

    search_props = by_name["honcho_search"].parameters["properties"]
    assert "identity" in search_props
