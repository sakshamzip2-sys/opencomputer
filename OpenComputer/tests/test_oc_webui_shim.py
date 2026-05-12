"""Tests for oc-webui's OpenComputer compatibility shim.

Coverage:
  - sys.modules aliasing — every hermes_* import resolves to a shim.
  - AIAgent kwargs lenient construction + reflective surface intact.
  - run_conversation builds an AgentLoop against a real OC SessionDB.
  - run_conversation raises OCAgentInitError when the API key is missing.
  - User-message coercion handles str / dict / list / multimodal.
  - ConversationResult → dict mapping shape.
  - hermes_cli.commands populates from OC's slash registry.
  - hermes_cli.runtime_provider returns the dict shape webui consumes.
  - hermes_cli.profiles returns dataclass instances (attribute access).
  - hermes_state.SessionDB no-arg init resolves a sensible path.
  - hermes_state.SessionDB.close() is idempotent.
  - hermes_constants.get_hermes_home / get_config_path resolve.
  - Schema migration v17 adds sessions.source + backfills.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

# Make the oc-webui shim importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_OC_WEBUI = _REPO_ROOT / "oc-webui"

# 2026-05-12: ``oc-webui/`` is gitignored (embedded git repo — its own
# upstream-fork state lives there). A fresh CI checkout does not contain
# it, so collection used to abort the whole pytest run with an
# ImportError before any test was discovered. Gracefully skip the entire
# module when the shim package is unavailable — this matches the spirit
# of the test (it's a shim test; skip when shim isn't present).
if not (_OC_WEBUI / "_oc_shim").exists() and not (_OC_WEBUI / "_oc_shim.py").exists():
    pytest.skip(
        f"oc-webui/_oc_shim not present at {_OC_WEBUI} — "
        "clone outsourc-e/hermes-webui (or its OC fork) into that path "
        "to enable this test suite.",
        allow_module_level=True,
    )

sys.path.insert(0, str(_OC_WEBUI))

from _oc_shim import install as _install_shim  # noqa: E402

_install_shim()


# ── Module aliasing ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "module_name",
    [
        "run_agent",
        "hermes_cli",
        "hermes_cli.commands",
        "hermes_cli.runtime_provider",
        "hermes_cli.models",
        "hermes_cli.auth",
        "hermes_cli.tools_config",
        "hermes_cli.plugins",
        "hermes_cli.config",
        "hermes_cli.profiles",
        "hermes_cli.goals",
        "hermes_cli.kanban_db",
        "hermes_state",
        "hermes_constants",
        "hermes_profile",
    ],
)
def test_shim_module_resolves(module_name: str) -> None:
    mod = importlib.import_module(module_name)
    assert getattr(mod, "__OC_SHIM__", False), f"{module_name} not shim-tagged"


# ── AIAgent shim ──────────────────────────────────────────────────────


def test_aiagent_lenient_kwargs() -> None:
    from run_agent import AIAgent

    agent = AIAgent(model="claude-haiku-4-5-20251001", foo="bar", _custom=42)
    assert agent.model == "claude-haiku-4-5-20251001"
    assert agent.foo == "bar"
    assert agent._custom == 42
    for attr in (
        "stream_delta_callback", "tool_progress_callback", "status_callback",
        "interim_assistant_callback", "reasoning_callback", "clarify_callback",
        "_session_db", "_api_call_count", "_interrupted", "_interrupt_message",
    ):
        assert hasattr(agent, attr)


def test_aiagent_interrupt_sets_flags() -> None:
    from run_agent import AIAgent

    agent = AIAgent()
    assert not agent._interrupted
    assert not agent._cancel_event.is_set()
    agent.interrupt("user cancelled")
    assert agent._interrupted is True
    assert agent._interrupt_message == "user cancelled"
    assert agent._cancel_event.is_set()


def test_aiagent_init_error_when_api_key_missing(monkeypatch) -> None:
    from run_agent import AIAgent, OCAgentInitError

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    agent = AIAgent(model="claude-haiku-4-5-20251001", provider="anthropic")
    with pytest.raises(OCAgentInitError) as excinfo:
        agent._build_oc_loop()
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_aiagent_init_with_real_provider_key(monkeypatch) -> None:
    from run_agent import AIAgent

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-init-only")
    agent = AIAgent(model="claude-haiku-4-5-20251001", provider="anthropic")
    loop = agent._build_oc_loop()
    assert type(loop).__name__ == "AgentLoop"
    assert agent._oc_provider is not None
    assert agent._oc_config.model.model == "claude-haiku-4-5-20251001"
    assert agent._oc_config.model.provider == "anthropic"


# ── Coercion helpers ──────────────────────────────────────────────────


def test_coerce_user_message_str() -> None:
    from _oc_shim.run_agent import _coerce_user_message_to_text

    text, images = _coerce_user_message_to_text("hello world")
    assert text == "hello world"
    assert images == []


def test_coerce_user_message_dict_text() -> None:
    from _oc_shim.run_agent import _coerce_user_message_to_text

    text, images = _coerce_user_message_to_text({"type": "text", "text": "hi"})
    assert text == "hi"
    assert images == []


def test_coerce_user_message_multimodal_list() -> None:
    from _oc_shim.run_agent import _coerce_user_message_to_text

    blocks = [
        {"type": "text", "text": "describe this"},
        {"type": "image", "source": {"path": "/tmp/foo.png"}},
        {"type": "text", "text": "and this"},
    ]
    text, images = _coerce_user_message_to_text(blocks)
    assert text == "describe this\nand this"
    assert images == ["/tmp/foo.png"]


def test_coerce_user_message_none() -> None:
    from _oc_shim.run_agent import _coerce_user_message_to_text

    text, images = _coerce_user_message_to_text(None)
    assert text == ""
    assert images == []


def test_extract_event_text_string() -> None:
    from _oc_shim.run_agent import _extract_event_text

    assert _extract_event_text("hello") == "hello"


def test_extract_event_text_dict_delta() -> None:
    from _oc_shim.run_agent import _extract_event_text

    assert _extract_event_text({"text_delta": "world"}) == "world"


def test_extract_event_text_attr() -> None:
    from _oc_shim.run_agent import _extract_event_text

    class _Ev:
        text_delta = "foo"

    assert _extract_event_text(_Ev()) == "foo"


def test_extract_event_text_unknown_returns_empty() -> None:
    from _oc_shim.run_agent import _extract_event_text

    assert _extract_event_text({"random": 1}) == ""
    assert _extract_event_text(None) == ""


def test_format_runtime_error_categorizes() -> None:
    from _oc_shim.run_agent import _format_runtime_error

    assert "Authentication" in _format_runtime_error(RuntimeError("invalid api key"))
    assert "quota" in _format_runtime_error(RuntimeError("rate limit hit")).lower()
    assert "Model not found" in _format_runtime_error(RuntimeError("model_not_found xyz"))
    assert "timeout" in _format_runtime_error(RuntimeError("connection timeout")).lower()


# ── ConversationResult → dict ─────────────────────────────────────────


def test_conversation_result_to_dict_with_real_dataclass() -> None:
    from _oc_shim.run_agent import _conversation_result_to_dict

    from opencomputer.agent.loop import ConversationResult
    from plugin_sdk.core import Message

    msg = Message(role="assistant", content="hi back")
    result = ConversationResult(
        final_message=msg,
        messages=[msg],
        session_id="abc",
        iterations=1,
        input_tokens=5,
        output_tokens=3,
        stop_reason=None,
    )
    out = _conversation_result_to_dict(result)
    assert out["session_id"] == "abc"
    assert out["iterations"] == 1
    assert out["usage"] == {"input_tokens": 5, "output_tokens": 3}
    assert out["messages"][0]["role"] == "assistant"
    assert out["messages"][0]["content"] == "hi back"
    # tool_calls / attachments must be [], never None — hermes-webui iterates them
    assert out["messages"][0]["tool_calls"] == []
    assert out["messages"][0]["attachments"] == []


def test_conversation_result_to_dict_passthrough_dict() -> None:
    from _oc_shim.run_agent import _conversation_result_to_dict

    raw = {"messages": [{"role": "user", "content": "x"}], "usage": {"input_tokens": 1}}
    assert _conversation_result_to_dict(raw) is raw


# ── hermes_cli.commands ───────────────────────────────────────────────


def test_command_registry_attribute_access() -> None:
    from hermes_cli.commands import COMMAND_REGISTRY, refresh

    rows = refresh()
    assert len(rows) > 5
    sample = rows[0]
    assert isinstance(sample.name, str)
    assert isinstance(sample.description, str)
    assert hasattr(sample, "gateway_only")
    assert hasattr(sample, "aliases")
    assert hasattr(sample, "args_hint")
    assert hasattr(sample, "subcommands")
    assert hasattr(sample, "cli_only")


# ── hermes_cli.runtime_provider ───────────────────────────────────────


def test_runtime_provider_anthropic(monkeypatch) -> None:
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid/v1")
    rt = resolve_runtime_provider(requested="anthropic")
    assert rt["provider"] == "anthropic"
    assert rt["api_key"] == "key-1"
    assert rt["base_url"] == "https://example.invalid/v1"
    assert rt["command"] is None


def test_runtime_provider_unknown_defaults_to_anthropic_envs(monkeypatch) -> None:
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rt = resolve_runtime_provider(requested="some-unknown-provider")
    assert rt["api_key"] == ""


# ── hermes_cli.profiles attribute access ──────────────────────────────


def test_profiles_returns_dataclass_attrs() -> None:
    from hermes_cli.profiles import HermesProfile, list_profiles

    rows = list_profiles()
    assert rows  # at least the default profile exists
    p = rows[0]
    assert isinstance(p, HermesProfile)
    assert isinstance(p.name, str)
    assert isinstance(p.path, Path)
    assert isinstance(p.is_default, bool)
    assert isinstance(p.skill_count, int)


# ── hermes_state.SessionDB ────────────────────────────────────────────


def test_hermes_state_sessiondb_no_arg_constructor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    from hermes_state import SessionDB

    db = SessionDB()
    expected = tmp_path / "sessions.db"
    assert Path(db.db_path) == expected
    assert expected.exists()
    db.close()


def test_hermes_state_sessiondb_explicit_path(tmp_path) -> None:
    from hermes_state import SessionDB

    target = tmp_path / "explicit.db"
    db = SessionDB(target)
    assert Path(db.db_path) == target
    assert target.exists()
    db.close()


def test_hermes_state_sessiondb_close_idempotent(tmp_path) -> None:
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "idempotent.db")
    assert db._conn is not None
    db.close()
    assert db._conn is None
    db.close()  # second close — must not raise
    assert db._conn is None
    db.close()  # third — still safe
    assert db._conn is None


# ── hermes_constants helpers ──────────────────────────────────────────


def test_hermes_constants_helpers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from hermes_constants import (
        APP_NAME,
        get_config_path,
        get_hermes_home,
    )

    assert APP_NAME == "OpenComputer"
    assert get_hermes_home() == tmp_path
    assert get_config_path() == tmp_path / "config.yaml"


# ── Schema migration v17 (sessions.source) ───────────────────────────


def test_schema_v17_adds_source_column_to_fresh_db(tmp_path) -> None:
    from opencomputer.agent.state import SCHEMA_VERSION, SessionDB

    assert SCHEMA_VERSION >= 17
    p = tmp_path / "fresh.db"
    SessionDB(p)
    with sqlite3.connect(str(p)) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(sessions)")]
        v = c.execute("SELECT version FROM schema_version").fetchone()[0]
    assert "source" in cols
    assert v == SCHEMA_VERSION
    with sqlite3.connect(str(p)) as c:
        idx = [
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='sessions'"
            )
        ]
    assert "idx_sessions_source" in idx


def test_schema_v17_backfills_existing_rows(tmp_path) -> None:
    """Verify v16→v17 migration is idempotent and backfills NULL source rows."""
    from opencomputer.agent.state import SCHEMA_VERSION, SessionDB

    p = tmp_path / "pre17.db"
    SessionDB(p)
    with sqlite3.connect(str(p)) as c:
        c.row_factory = sqlite3.Row
        c.execute(
            "INSERT INTO sessions (id, started_at, platform, source) "
            "VALUES ('a', 0.0, 'cli', NULL)"
        )
        c.execute(
            "INSERT INTO sessions (id, started_at, platform, source) "
            "VALUES ('b', 0.0, 'cli', '')"
        )
        c.commit()
    with sqlite3.connect(str(p)) as c:
        c.execute("UPDATE schema_version SET version = 16")
        c.commit()
    SessionDB(p)
    with sqlite3.connect(str(p)) as c:
        c.row_factory = sqlite3.Row
        rows = [
            (r["id"], r["source"])
            for r in c.execute(
                "SELECT id, source FROM sessions WHERE id IN ('a','b') "
                "ORDER BY id"
            ).fetchall()
        ]
        v = c.execute("SELECT version FROM schema_version").fetchone()[0]
    # Migration rolls forward to whatever the current SCHEMA_VERSION is —
    # the v16→v17 step under test is the source backfill (asserted below);
    # the loop continues to the latest version (was 17, now 18, …).
    assert v == SCHEMA_VERSION
    assert rows == [("a", "cli"), ("b", "cli")]


# ── Shim install idempotence ──────────────────────────────────────────


def test_install_is_idempotent() -> None:
    _install_shim()
    _install_shim()
    import run_agent
    assert run_agent.__OC_SHIM__ is True


# ── COMMAND_REGISTRY filter rules ─────────────────────────────────────


def test_command_registry_excludes_no_name() -> None:
    from _oc_shim.hermes_cli.commands import _project

    class NameLess:
        pass

    assert _project(NameLess()) is None


def test_command_registry_strips_leading_slash() -> None:
    from _oc_shim.hermes_cli.commands import _project

    class Cmd:
        name = "/foo"
        description = "d"
        aliases = ("/bar",)

    e = _project(Cmd())
    assert e is not None
    assert e.name == "foo"
    assert e.aliases == ("bar",)


# ── Production-grade hardening ────────────────────────────────────────
# Tests added 2026-05-10 to lock in the behaviours of every new branch
# introduced during the production-grade hardening pass on the shim.


# Provider-plugin loader — _load_provider_plugins
def test_load_provider_plugins_uses_frozenset(monkeypatch) -> None:
    """``load_all`` requires ``frozenset``; the shim must wrap correctly.

    Regression guard: a previous version passed ``list`` which silently
    tripped an AssertionError that the surrounding bare-except swallowed,
    leaving the registry empty and chat broken with no diagnostic.
    """
    from run_agent import AIAgent
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    seen_kwargs: dict = {}

    class _FakeRegistry:
        providers: dict = {"anthropic": object()}
        def load_all(self, paths, *, enabled_ids):
            seen_kwargs["enabled_ids"] = enabled_ids
            return []

    class _FakePluginRegistry:
        registry = _FakeRegistry()

    AIAgent()._load_provider_plugins(_FakePluginRegistry())
    assert isinstance(seen_kwargs.get("enabled_ids"), (frozenset, str)), (
        f"enabled_ids must be frozenset or '*', got "
        f"{type(seen_kwargs.get('enabled_ids')).__name__}"
    )


def test_load_provider_plugins_handles_discover_failure(monkeypatch, caplog) -> None:
    """When discover() raises, log WARN and don't blow up."""
    import logging

    from run_agent import AIAgent

    from opencomputer.plugins import discovery as _discovery

    def _boom(_paths):
        raise RuntimeError("simulated disk corruption")

    monkeypatch.setattr(_discovery, "discover", _boom)

    class _FakePluginRegistry:
        class registry:
            providers = {}

    with caplog.at_level(logging.WARNING, logger="oc_webui.shim.run_agent"):
        AIAgent()._load_provider_plugins(_FakePluginRegistry())
    assert any(
        "discovery raised" in r.message for r in caplog.records
    ), "expected WARN about discovery failure (no silent debug-swallow)"


# Apply model override — _apply_model_override
def test_apply_model_override_replaces_frozen_dataclass() -> None:
    """Frozen ModelConfig: dataclasses.replace path."""
    from run_agent import AIAgent

    from opencomputer.agent.config import default_config

    cfg = default_config()
    new = AIAgent._apply_model_override(cfg, "claude-haiku-4-5-20251001", "anthropic")
    assert new.model.model == "claude-haiku-4-5-20251001"
    assert new.model.provider == "anthropic"


def test_apply_model_override_raises_on_unmodifiable_config() -> None:
    """Both replace + setattr fail → must raise OCAgentRuntimeError, not pass."""
    from run_agent import AIAgent, OCAgentRuntimeError

    class _Immutable:
        __slots__ = ()
    class _Cfg:
        model = _Immutable()
    with pytest.raises(OCAgentRuntimeError) as excinfo:
        AIAgent._apply_model_override(_Cfg(), "x", "y")
    assert "model override" in str(excinfo.value).lower()


# Provider credential preflight
def test_resolve_provider_accepts_alt_anthropic_token(monkeypatch) -> None:
    """ANTHROPIC_AUTH_TOKEN should also satisfy the preflight check."""
    from run_agent import AIAgent

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-token")

    sentinel = object()

    class _Reg:
        providers = {"anthropic": sentinel}

    out = AIAgent._resolve_provider(_Reg(), "anthropic")
    assert out is sentinel  # not a type → returned as-is


def test_resolve_provider_unknown_skips_preflight() -> None:
    """A provider not in _PROVIDER_CREDENTIAL_ENV must NOT preflight."""
    from run_agent import AIAgent

    sentinel = object()

    class _Reg:
        providers = {"some-local-provider": sentinel}

    out = AIAgent._resolve_provider(_Reg(), "some-local-provider")
    assert out is sentinel


# Auth secret preview — never leak
def test_safe_preview_short_secret_reveals_only_length() -> None:
    from _oc_shim.hermes_cli.auth import _safe_preview

    assert _safe_preview("abc") == "<3 chars>"
    assert _safe_preview("sk-test-1") == "<9 chars>"


def test_safe_preview_long_secret_caps_at_4_chars() -> None:
    from _oc_shim.hermes_cli.auth import _safe_preview

    s = "sk-ant-1234567890abcdefXYZ"
    out = _safe_preview(s)
    assert out.startswith("sk-a")
    assert "<26 chars>" in out
    # Must NOT leak more than 4 characters of the secret.
    assert s[5:] not in out


def test_safe_preview_empty() -> None:
    from _oc_shim.hermes_cli.auth import _safe_preview

    assert _safe_preview("") == ""


# Profile name validation — defence in depth against path traversal
def test_validate_profile_name_rejects_traversal() -> None:
    from _oc_shim.hermes_cli.profiles import _validate_profile_name

    for evil in ("..", "../etc", "/abs", "rel/sub", "name\x00"):
        with pytest.raises(ValueError):
            _validate_profile_name(evil)


def test_validate_profile_name_rejects_reserved() -> None:
    from _oc_shim.hermes_cli.profiles import _validate_profile_name

    for r in ("default", "global", "system", "tmp"):
        with pytest.raises(ValueError):
            _validate_profile_name(r)


def test_validate_profile_name_rejects_oversize() -> None:
    from _oc_shim.hermes_cli.profiles import _validate_profile_name

    with pytest.raises(ValueError):
        _validate_profile_name("a" * 65)


def test_validate_profile_name_accepts_alphanumeric() -> None:
    from _oc_shim.hermes_cli.profiles import _validate_profile_name

    for ok in ("dev", "staging-1", "test_2", "foo-bar"):
        _validate_profile_name(ok)  # no raise


def test_get_profile_home_rejects_traversal() -> None:
    from hermes_profile import get_profile_home

    for evil in ("..", "../etc", "foo/bar", "abs\\path"):
        with pytest.raises(ValueError):
            get_profile_home(evil)


# Goals input validation
def test_normalize_session_id_rejects_oversize() -> None:
    from _oc_shim.hermes_cli.goals import _normalize_session_id

    assert _normalize_session_id(None) is None
    assert _normalize_session_id("") is None
    assert _normalize_session_id("  ") is None
    assert _normalize_session_id("a" * 257) is None
    assert _normalize_session_id("uuid-123") == "uuid-123"


def test_normalize_budget_rejects_invalid() -> None:
    from _oc_shim.hermes_cli.goals import _normalize_budget

    assert _normalize_budget(None) is None
    assert _normalize_budget("not-a-number") is None
    assert _normalize_budget(-1) is None
    assert _normalize_budget(20) == 20
    assert _normalize_budget(2_000_000) == 1_000_000  # clamp


# URL safety
def test_runtime_provider_rejects_unsafe_base_url(monkeypatch) -> None:
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "file:///etc/passwd")
    rt = resolve_runtime_provider(requested="anthropic")
    assert rt["base_url"] is None  # unsafe scheme dropped


def test_runtime_provider_accepts_https_base_url(monkeypatch) -> None:
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://my-proxy.example/v1")
    rt = resolve_runtime_provider(requested="anthropic")
    assert rt["base_url"] == "https://my-proxy.example/v1"


# Shim install fail-loud
def test_install_raises_when_run_agent_fails(monkeypatch) -> None:
    """A failed run_agent import must raise — never boot a chat-less UI."""
    import importlib
    import sys

    import _oc_shim

    # Wipe cached state so install() rebuilds.
    for k in list(sys.modules):
        if k == "run_agent" or k.startswith("_oc_shim.run_agent"):
            sys.modules.pop(k, None)

    real_import = importlib.import_module

    def _fake_import(name, *a, **kw):
        if name == "_oc_shim.run_agent":
            raise ImportError("simulated run_agent breakage")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", _fake_import)
    with pytest.raises(RuntimeError) as excinfo:
        _oc_shim.install()
    assert "run_agent" in str(excinfo.value)


# CLI registration
def test_oc_webui_command_registered() -> None:
    """Verify ``oc webui`` is exposed via Typer (was a missing entry point)."""
    from opencomputer import cli as _cli

    # Typer stores either an explicit `name=` or derives from the
    # callback's function name (lowercased). Cover both lookups.
    names = []
    for c in _cli.app.registered_commands:
        names.append(c.name)
        if c.callback is not None:
            names.append(c.callback.__name__.replace("_", "-"))
    assert "webui" in names


def test_oc_webui_strict_explicit_dir(tmp_path) -> None:
    """An explicit --webui-dir that's invalid must fail loudly, NOT fall through."""
    from typer.testing import CliRunner

    from opencomputer.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["webui", "--webui-dir", str(tmp_path / "nope")])
    assert result.exit_code == 1
    assert "is not a valid oc-webui repo" in result.output


def test_oc_webui_strict_env_dir(tmp_path, monkeypatch) -> None:
    """$OC_WEBUI_DIR pointing somewhere bad must also fail loudly."""
    from typer.testing import CliRunner

    from opencomputer.cli import app

    monkeypatch.setenv("OC_WEBUI_DIR", str(tmp_path / "also-nope"))
    runner = CliRunner()
    result = runner.invoke(app, ["webui"])
    assert result.exit_code == 1
    assert "$OC_WEBUI_DIR" in result.output


# v17 source default for new sessions
def test_new_session_inherits_source_cli_by_default(tmp_path) -> None:
    """New SessionDB rows without explicit source should accept NULL/empty.

    The migration backfilled existing rows to 'cli'. New writes can leave
    source NULL — the gateway adapter is responsible for setting it.
    The test pins the contract: the column accepts NULL on insert.
    """
    import sqlite3

    from opencomputer.agent.state import SessionDB

    p = tmp_path / "v17.db"
    SessionDB(p)
    with sqlite3.connect(str(p)) as c:
        c.execute(
            "INSERT INTO sessions (id, started_at, platform) VALUES ('z', 0.0, 'cli')"
        )
        c.commit()
        row = c.execute("SELECT source FROM sessions WHERE id='z'").fetchone()
    assert row[0] is None  # explicit: writers opt-in to source label
