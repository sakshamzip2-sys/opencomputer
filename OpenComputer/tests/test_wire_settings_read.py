"""Wire RPCs for the read side of settings — ``model.options`` + ``config.get``.

TUI-parity Milestone 1, batch 2 (spec:
``docs/superpowers/specs/2026-05-17-tui-parity/TUI.md``; mapping:
``docs/refs/hermes-tui-protocol-vs-oc-wire.md``).

These are the two methods a model-picker overlay and a settings panel need
to *render* — the read half. The write half (``model.set`` / ``config.set``)
is batch 3, kept separate because it mutates the profile config and needs
its own write-isolation harness.

* ``model.options`` — enumerate every provider→model pairing in the
  registry plus the currently-bound default. Powers a model-picker overlay.
* ``config.get`` — fetch one config value by dotted key (``model.provider``,
  ``loop.max_iterations``, …). Powers a settings panel.

Coverage mirrors ``test_wire_session_lifecycle.py``: protocol surface +
graceful-degradation helper units + end-to-end RPC over a real WS.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from unittest.mock import MagicMock

import pytest

# ─── Protocol surface ──────────────────────────────────────────────


class TestSettingsReadProtocol:
    def test_method_constants(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_CONFIG_GET,
            METHOD_MODEL_OPTIONS,
        )

        assert METHOD_MODEL_OPTIONS == "model.options"
        assert METHOD_CONFIG_GET == "config.get"

    def test_methods_in_protocol_all(self) -> None:
        from opencomputer.gateway import protocol

        assert "METHOD_MODEL_OPTIONS" in protocol.__all__
        assert "METHOD_CONFIG_GET" in protocol.__all__

    def test_schemas_in_protocol_v2_all(self) -> None:
        from opencomputer.gateway import protocol_v2

        for name in (
            "METHOD_MODEL_OPTIONS",
            "METHOD_CONFIG_GET",
            "ModelOptionsParams",
            "ModelOptionsResult",
            "ModelProviderOption",
            "ConfigGetParams",
            "ConfigGetResult",
        ):
            assert name in protocol_v2.__all__, f"missing __all__: {name}"

    def test_method_schemas_registry(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_CONFIG_GET,
            METHOD_MODEL_OPTIONS,
        )
        from opencomputer.gateway.protocol_v2 import (
            METHOD_SCHEMAS,
            ConfigGetParams,
            ConfigGetResult,
            ModelOptionsParams,
            ModelOptionsResult,
        )

        assert METHOD_SCHEMAS[METHOD_MODEL_OPTIONS] == (
            ModelOptionsParams,
            ModelOptionsResult,
        )
        assert METHOD_SCHEMAS[METHOD_CONFIG_GET] == (
            ConfigGetParams,
            ConfigGetResult,
        )

    def test_model_options_result_round_trip(self) -> None:
        from opencomputer.gateway.protocol_v2 import (
            ModelOptionsResult,
            ModelProviderOption,
        )

        result = ModelOptionsResult(
            model="claude-opus-4-7",
            provider="anthropic",
            providers=(
                ModelProviderOption(
                    name="anthropic",
                    models=("claude-opus-4-7", "claude-sonnet-4-6"),
                    is_current=True,
                ),
                ModelProviderOption(name="openai", models=("gpt-5",), is_current=False),
            ),
        )
        restored = ModelOptionsResult.model_validate_json(result.model_dump_json())
        assert restored == result

    def test_config_get_result_round_trip(self) -> None:
        from opencomputer.gateway.protocol_v2 import ConfigGetResult

        result = ConfigGetResult(key="model.provider", value="anthropic", found=True)
        restored = ConfigGetResult.model_validate_json(result.model_dump_json())
        assert restored == result

    def test_config_get_params_reject_unknown_field(self) -> None:
        from opencomputer.gateway.protocol_v2 import ConfigGetParams

        with pytest.raises(Exception):  # pydantic.ValidationError
            ConfigGetParams(key="x", bogus="surprise")


# ─── helper unit tests ─────────────────────────────────────────────


class TestCollectModelOptionsHelper:
    """``_collect_model_options`` never raises — registry failure → empty."""

    def test_returns_expected_shape(self) -> None:
        from opencomputer.gateway.wire_server import WireServer

        out = WireServer._collect_model_options()
        assert set(out.keys()) == {"model", "provider", "providers"}
        assert isinstance(out["providers"], list)
        for prov in out["providers"]:
            assert set(prov.keys()) == {"name", "models", "is_current"}
            assert isinstance(prov["models"], list)

    def test_registry_failure_degrades_to_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the model registry blows up, the helper still returns a valid
        (empty-providers) payload rather than raising."""
        import opencomputer.cli_model_picker as cmp
        from opencomputer.gateway.wire_server import WireServer

        def boom() -> dict:
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(cmp, "_grouped_models", boom)
        out = WireServer._collect_model_options()
        assert out["providers"] == []


class TestCollectConfigValueHelper:
    """``_collect_config_value`` resolves dotted keys, never raises."""

    def test_known_key_is_found(self) -> None:
        from opencomputer.gateway.wire_server import WireServer

        # Every OC config has a `model` section with a `provider` field.
        out = WireServer._collect_config_value("model.provider")
        assert out["key"] == "model.provider"
        assert out["found"] is True

    def test_unknown_key_reports_not_found(self) -> None:
        from opencomputer.gateway.wire_server import WireServer

        out = WireServer._collect_config_value("nonsense.key.path")
        assert out["found"] is False
        assert out["value"] is None

    def test_value_is_json_serializable(self) -> None:
        """A dataclass section (model.* etc.) must be coerced JSON-safe so
        the typed result schema can serialize it."""
        from opencomputer.gateway.wire_server import WireServer

        out = WireServer._collect_config_value("model")
        # Whatever it is, it must round-trip through json.
        json.dumps(out["value"])


# ─── End-to-end RPC over a real WS ─────────────────────────────────


@contextlib.asynccontextmanager
async def _wire_server():
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.gateway.wire_server import WireServer

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    fake_loop = MagicMock(spec=AgentLoop)
    server = WireServer(loop=fake_loop, host="127.0.0.1", port=port)
    await server.start()
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        await server.stop()


async def _rpc(url: str, method: str, params: dict) -> dict:
    import websockets

    async with websockets.connect(url) as client:
        req = {"type": "req", "id": "t-1", "method": method, "params": params}
        await client.send(json.dumps(req))
        raw = await asyncio.wait_for(client.recv(), timeout=2.0)
        return json.loads(raw)


@pytest.mark.asyncio
async def test_model_options_rpc_returns_schema_compliant_payload() -> None:
    async with _wire_server() as url:
        msg = await _rpc(url, "model.options", {})
        assert msg["type"] == "res"
        assert msg["ok"] is True
        payload = msg["payload"]
        assert "providers" in payload
        assert isinstance(payload["providers"], list)

        from opencomputer.gateway.protocol_v2 import ModelOptionsResult

        ModelOptionsResult.model_validate(payload)


@pytest.mark.asyncio
async def test_config_get_rpc_known_key() -> None:
    async with _wire_server() as url:
        msg = await _rpc(url, "config.get", {"key": "model.provider"})
        assert msg["ok"] is True
        assert msg["payload"]["key"] == "model.provider"
        assert msg["payload"]["found"] is True

        from opencomputer.gateway.protocol_v2 import ConfigGetResult

        ConfigGetResult.model_validate(msg["payload"])


@pytest.mark.asyncio
async def test_config_get_rpc_unknown_key_is_found_false() -> None:
    async with _wire_server() as url:
        msg = await _rpc(url, "config.get", {"key": "ghost.section.field"})
        # Unknown key is not an error — it's a successful "not found".
        assert msg["ok"] is True
        assert msg["payload"]["found"] is False


@pytest.mark.asyncio
async def test_config_get_rpc_missing_key_param_is_error() -> None:
    async with _wire_server() as url:
        msg = await _rpc(url, "config.get", {})
        assert msg["ok"] is False
        assert "key" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_hello_handshake_advertises_settings_read() -> None:
    async with _wire_server() as url:
        msg = await _rpc(url, "hello", {})
        assert msg["ok"] is True
        methods = msg["payload"]["methods"]
        assert "model.options" in methods
        assert "config.get" in methods
