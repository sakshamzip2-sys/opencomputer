"""Wire RPCs for the write side of settings — ``model.set`` + ``config.set``.

TUI-parity Milestone 1, batch 3 (spec:
``docs/superpowers/specs/2026-05-17-tui-parity/TUI.md``; mapping:
``docs/refs/hermes-tui-protocol-vs-oc-wire.md``).

The write counterparts of batch 2's ``model.options`` / ``config.get`` —
together they complete the backend a model-picker overlay and a settings
panel need. Both mutate the profile ``config.yaml``, so they go through a
single write+validate+rollback harness (``_persist_config_mutation``)
mirroring the dashboard ``PUT /api/v1/config`` route: back up to ``.bak``,
write, re-load to validate, restore on failure.

* ``model.set`` — persist a new default provider+model.
* ``config.set`` — persist one config value by dotted key.

Both are **persist-only** for v1 (matches the dashboard route) — the
running session is unaffected until restart; live model swap is the
separate ``/model`` slash command's job.

Every test isolates the write via the ``isolated_config`` fixture, which
monkeypatches ``config_store.config_file_path`` to a tmp file — the real
profile ``config.yaml`` is never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

# ─── isolation fixture ─────────────────────────────────────────────


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every config read/write to a tmp config.yaml.

    ``load_config`` / ``save_config`` resolve their default path through
    ``config_store.config_file_path()``; patching that one function
    isolates the whole write path from the real profile.
    """
    from opencomputer.agent import config_store

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model:\n  provider: oldprov\n  model: oldmodel\n")
    monkeypatch.setattr(config_store, "config_file_path", lambda: cfg_path)
    return cfg_path


# ─── Protocol surface ──────────────────────────────────────────────


class TestSettingsWriteProtocol:
    def test_method_constants(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_CONFIG_SET,
            METHOD_MODEL_SET,
        )

        assert METHOD_MODEL_SET == "model.set"
        assert METHOD_CONFIG_SET == "config.set"

    def test_methods_in_protocol_all(self) -> None:
        from opencomputer.gateway import protocol

        assert "METHOD_MODEL_SET" in protocol.__all__
        assert "METHOD_CONFIG_SET" in protocol.__all__

    def test_schemas_in_protocol_v2_all(self) -> None:
        from opencomputer.gateway import protocol_v2

        for name in (
            "METHOD_MODEL_SET",
            "METHOD_CONFIG_SET",
            "ModelSetParams",
            "ModelSetResult",
            "ConfigSetParams",
            "ConfigSetResult",
        ):
            assert name in protocol_v2.__all__, f"missing __all__: {name}"

    def test_method_schemas_registry(self) -> None:
        from opencomputer.gateway.protocol import (
            METHOD_CONFIG_SET,
            METHOD_MODEL_SET,
        )
        from opencomputer.gateway.protocol_v2 import (
            METHOD_SCHEMAS,
            ConfigSetParams,
            ConfigSetResult,
            ModelSetParams,
            ModelSetResult,
        )

        assert METHOD_SCHEMAS[METHOD_MODEL_SET] == (ModelSetParams, ModelSetResult)
        assert METHOD_SCHEMAS[METHOD_CONFIG_SET] == (
            ConfigSetParams,
            ConfigSetResult,
        )

    def test_result_round_trips(self) -> None:
        from opencomputer.gateway.protocol_v2 import (
            ConfigSetResult,
            ModelSetResult,
        )

        m = ModelSetResult(provider="anthropic", model="claude-opus-4-7", ok=True)
        assert ModelSetResult.model_validate_json(m.model_dump_json()) == m
        c = ConfigSetResult(key="model.provider", value="anthropic", ok=True)
        assert ConfigSetResult.model_validate_json(c.model_dump_json()) == c

    def test_params_reject_unknown_field(self) -> None:
        from opencomputer.gateway.protocol_v2 import ModelSetParams

        with pytest.raises(Exception):  # pydantic.ValidationError
            ModelSetParams(provider="x", model="y", bogus="surprise")


# ─── helper unit tests ─────────────────────────────────────────────


class TestApplyModelSetHelper:
    def test_persists_new_model(self, isolated_config: Path) -> None:
        from opencomputer.gateway.wire_server import WireServer

        out = WireServer._apply_model_set("newprov", "new-model-x")
        assert out == {"provider": "newprov", "model": "new-model-x", "ok": True}
        on_disk = yaml.safe_load(isolated_config.read_text())
        assert on_disk["model"]["provider"] == "newprov"
        assert on_disk["model"]["model"] == "new-model-x"

    def test_returns_error_string_on_load_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely broken config.yaml → the helper returns an error
        string, not a dict, and never raises."""
        from opencomputer.agent import config_store
        from opencomputer.gateway.wire_server import WireServer

        bad = tmp_path / "config.yaml"
        bad.write_text("model: [this is not\n  valid: yaml: : :\n")
        monkeypatch.setattr(config_store, "config_file_path", lambda: bad)

        out = WireServer._apply_model_set("p", "m")
        assert isinstance(out, str)
        assert "model.set" in out


class TestApplyConfigSetHelper:
    def test_persists_dotted_key(self, isolated_config: Path) -> None:
        from opencomputer.gateway.wire_server import WireServer

        out = WireServer._apply_config_set("model.provider", "viaconfig")
        assert out == {"key": "model.provider", "value": "viaconfig", "ok": True}
        on_disk = yaml.safe_load(isolated_config.read_text())
        assert on_disk["model"]["provider"] == "viaconfig"

    def test_unknown_key_returns_error_string(self, isolated_config: Path) -> None:
        from opencomputer.gateway.wire_server import WireServer

        out = WireServer._apply_config_set("ghost.field", "x")
        assert isinstance(out, str)
        assert "config.set" in out
        # The bad write never reached disk — original value intact.
        on_disk = yaml.safe_load(isolated_config.read_text())
        assert on_disk["model"]["provider"] == "oldprov"

    def test_top_level_key_rejected(self, isolated_config: Path) -> None:
        from opencomputer.gateway.wire_server import WireServer

        out = WireServer._apply_config_set("model", "x")
        assert isinstance(out, str)


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
async def test_model_set_rpc_persists(isolated_config: Path) -> None:
    async with _wire_server() as url:
        msg = await _rpc(
            url, "model.set", {"provider": "anthropic", "model": "claude-opus-4-7"}
        )
        assert msg["ok"] is True
        assert msg["payload"]["ok"] is True
        assert msg["payload"]["model"] == "claude-opus-4-7"

        from opencomputer.gateway.protocol_v2 import ModelSetResult

        ModelSetResult.model_validate(msg["payload"])
        # Side effect verified on disk.
        on_disk = yaml.safe_load(isolated_config.read_text())
        assert on_disk["model"]["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_model_set_rpc_missing_params_is_error(isolated_config: Path) -> None:
    async with _wire_server() as url:
        msg = await _rpc(url, "model.set", {"provider": "anthropic"})
        assert msg["ok"] is False
        assert "model" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_config_set_rpc_persists(isolated_config: Path) -> None:
    async with _wire_server() as url:
        msg = await _rpc(
            url, "config.set", {"key": "model.model", "value": "set-via-wire"}
        )
        assert msg["ok"] is True
        assert msg["payload"]["ok"] is True

        from opencomputer.gateway.protocol_v2 import ConfigSetResult

        ConfigSetResult.model_validate(msg["payload"])
        on_disk = yaml.safe_load(isolated_config.read_text())
        assert on_disk["model"]["model"] == "set-via-wire"


@pytest.mark.asyncio
async def test_config_set_rpc_unknown_key_is_error(isolated_config: Path) -> None:
    async with _wire_server() as url:
        msg = await _rpc(url, "config.set", {"key": "ghost.field", "value": "x"})
        assert msg["ok"] is False
        assert "config.set" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_config_set_rpc_missing_key_is_error(isolated_config: Path) -> None:
    async with _wire_server() as url:
        msg = await _rpc(url, "config.set", {"value": "x"})
        assert msg["ok"] is False
        assert "key" in (msg.get("error") or "")


@pytest.mark.asyncio
async def test_hello_handshake_advertises_settings_write(
    isolated_config: Path,
) -> None:
    async with _wire_server() as url:
        msg = await _rpc(url, "hello", {})
        assert msg["ok"] is True
        methods = msg["payload"]["methods"]
        assert "model.set" in methods
        assert "config.set" in methods
