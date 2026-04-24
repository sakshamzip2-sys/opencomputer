"""Task I.6 — Provider config-schema validation at registration.

Providers may declare an optional ``config_schema: type[BaseModel]``
class attribute. When set, the plugin registry validates the provider's
``config`` against that schema at ``register_provider`` time, raising
``ValueError`` on mismatch instead of failing at first-use.

Matches OpenClaw's pattern in
``sources/openclaw/src/plugins/provider-validation.ts`` —
``normalizeRegisteredProvider`` normalizes/validates provider shape at
registration rather than waiting for the first request.

Backwards compat: providers without ``config_schema`` (the default
``None``) skip validation entirely — existing providers continue to work.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from opencomputer.agent.injection import InjectionEngine
from opencomputer.hooks.engine import HookEngine
from opencomputer.plugins.loader import PluginAPI
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage

# ─── helpers ────────────────────────────────────────────────────────────


def _isolated_api(tmp_path: Path) -> PluginAPI:
    """Fresh PluginAPI with isolated registries."""
    return PluginAPI(
        tool_registry=ToolRegistry(),
        hook_engine=HookEngine(),
        provider_registry={},
        channel_registry={},
        injection_engine=InjectionEngine(),
        doctor_contributions=[],
        session_db_path=tmp_path / "session.sqlite",
    )


class _StubProvider(BaseProvider):
    """Minimal BaseProvider used to exercise registration paths.

    Provider with no ``config_schema`` — the default case. Registration
    must succeed without any validation.
    """

    name = "stub"
    default_model = "stub-1"

    def __init__(self, config=None) -> None:
        self.config = config

    async def complete(self, **kwargs):  # pragma: no cover
        return ProviderResponse(
            message=Message(role="assistant", content=""),
            stop_reason="end_turn",
            usage=Usage(),
        )

    async def stream_complete(self, **kwargs):  # pragma: no cover
        yield  # type: ignore[misc]


class _TestProviderConfig(BaseModel):
    """Schema used by tests."""

    api_key: str
    timeout: int = 30


class _SchemaProvider(_StubProvider):
    """Provider with a ``config_schema`` declared — the opt-in case."""

    name = "schema-provider"
    config_schema = _TestProviderConfig  # type: ignore[assignment]


# ─── backwards-compat: provider without config_schema ──────────────────


def test_provider_without_config_schema_registers_without_validation(
    tmp_path: Path,
) -> None:
    """Backwards compat: provider with ``config_schema = None`` skips validation.

    Previously-built providers (anthropic, openai) continue to work
    unchanged. The registry accepts whatever config they carry.
    """
    api = _isolated_api(tmp_path)
    provider = _StubProvider(config={"any": "config"})
    # No exception — plain registration, no schema to check.
    api.register_provider("stub", provider)
    assert api.providers["stub"] is provider


def test_class_registration_still_works_no_schema(tmp_path: Path) -> None:
    """Registering a PROVIDER CLASS (not instance) without schema works.

    Existing plugins register the class, not an instance — validation
    must only run when an instance is passed.
    """
    api = _isolated_api(tmp_path)
    api.register_provider("stub-cls", _StubProvider)
    assert api.providers["stub-cls"] is _StubProvider


# ─── config_schema set + valid config → clean registration ────────────


def test_provider_with_schema_and_valid_config_registers_cleanly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Provider with ``config_schema`` and matching ``config`` → registers OK."""
    api = _isolated_api(tmp_path)
    good_cfg = _TestProviderConfig(api_key="k", timeout=10)
    provider = _SchemaProvider(config=good_cfg)

    caplog.set_level(logging.DEBUG, logger="opencomputer.plugins.loader")
    api.register_provider("schema-provider", provider)

    assert api.providers["schema-provider"] is provider


def test_schema_provider_with_dict_config_is_parsed_and_validated(
    tmp_path: Path,
) -> None:
    """When ``provider.config`` is a dict, it's parsed via ``config_schema(**config)``.

    Some providers may carry their config as a dict rather than as a
    ``BaseModel`` instance. The registry normalizes that.
    """
    api = _isolated_api(tmp_path)
    provider = _SchemaProvider(config={"api_key": "k", "timeout": 5})
    api.register_provider("schema-provider", provider)
    assert api.providers["schema-provider"] is provider


# ─── config_schema set + invalid config → raises ValueError ───────────


def test_provider_with_schema_and_invalid_config_raises_value_error(
    tmp_path: Path,
) -> None:
    """Provider with ``config_schema`` + missing/wrong-type config → ValueError."""
    api = _isolated_api(tmp_path)
    # Missing required api_key — should fail schema validation.
    provider = _SchemaProvider(config={"timeout": 5})

    with pytest.raises(ValueError) as exc_info:
        api.register_provider("schema-provider", provider)

    msg = str(exc_info.value)
    assert "schema-provider" in msg or "config" in msg.lower()


def test_provider_with_schema_and_wrong_type_raises(tmp_path: Path) -> None:
    """Provider config with wrong field type → ValueError on register."""
    api = _isolated_api(tmp_path)
    # timeout must be int; "not-an-int" fails.
    provider = _SchemaProvider(config={"api_key": "k", "timeout": "not-an-int"})

    with pytest.raises(ValueError):
        api.register_provider("schema-provider", provider)


def test_value_error_message_names_the_provider(tmp_path: Path) -> None:
    """The ValueError must identify WHICH provider failed + WHY.

    Without this, debugging a bad manifest is a needle-in-haystack.
    """
    api = _isolated_api(tmp_path)
    provider = _SchemaProvider(config={"timeout": 5})  # missing api_key

    with pytest.raises(ValueError) as exc_info:
        api.register_provider("named-xyz", provider)

    msg = str(exc_info.value)
    # The provider NAME (the key) must appear so the user knows which
    # plugin to go fix.
    assert "named-xyz" in msg


def test_registration_failure_does_not_poison_registry(tmp_path: Path) -> None:
    """Validation failure must NOT leave a partial entry in providers{}."""
    api = _isolated_api(tmp_path)
    bad_provider = _SchemaProvider(config={"timeout": 5})

    with pytest.raises(ValueError):
        api.register_provider("bad", bad_provider)

    assert "bad" not in api.providers


# ─── AnthropicProvider integration ─────────────────────────────────────


def _import_anthropic_provider():
    """Load the provider module directly (matches test_provider_auth.py)."""
    repo_root = Path(__file__).resolve().parent.parent
    provider_path = repo_root / "extensions" / "anthropic-provider" / "provider.py"
    spec = importlib.util.spec_from_file_location(
        "anthropic_provider_test_config_schema_only", provider_path
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["anthropic_provider_test_config_schema_only"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_anthropic_provider_has_config_schema_attribute() -> None:
    """AnthropicProvider declares ``config_schema`` per the I.6 demonstration."""
    mod = _import_anthropic_provider()
    assert mod.AnthropicProvider.config_schema is not None


def test_anthropic_provider_construction_still_works_env_vars() -> None:
    """Existing env-var construction is unchanged after adding config_schema."""
    mod = _import_anthropic_provider()
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-123"}, clear=False):
        os.environ.pop("ANTHROPIC_AUTH_MODE", None)
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        p = mod.AnthropicProvider()
        assert p.client is not None
        # The provider stores its validated config so register_provider
        # can re-check it.
        assert p.config is not None


def test_anthropic_provider_construction_accepts_kwargs() -> None:
    """Kwargs still override env vars — existing behavior preserved."""
    mod = _import_anthropic_provider()
    with patch.dict(os.environ, {}, clear=True):
        p = mod.AnthropicProvider(
            api_key="pk-123",
            base_url="https://example.test",
            auth_mode="bearer",
        )
        assert p.client is not None
        assert p.config.api_key == "pk-123"
        assert p.config.base_url == "https://example.test"
        assert p.config.auth_mode == "bearer"


def test_anthropic_provider_registers_with_api_key(tmp_path: Path) -> None:
    """Wired instance of AnthropicProvider registers without validation errors."""
    mod = _import_anthropic_provider()
    api = _isolated_api(tmp_path)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-456"}, clear=False):
        os.environ.pop("ANTHROPIC_AUTH_MODE", None)
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        provider = mod.AnthropicProvider()
        api.register_provider("anthropic", provider)
        assert api.providers["anthropic"] is provider


def test_anthropic_provider_config_schema_fields() -> None:
    """The AnthropicProviderConfig schema declares the expected fields."""
    mod = _import_anthropic_provider()
    schema = mod.AnthropicProvider.config_schema
    fields = schema.model_fields
    assert "api_key" in fields
    assert "base_url" in fields
    assert "auth_mode" in fields
    # auth_mode is Literal["api_key", "bearer"] with default.
    cfg = schema(api_key="k")
    assert cfg.auth_mode in ("api_key", "bearer")
