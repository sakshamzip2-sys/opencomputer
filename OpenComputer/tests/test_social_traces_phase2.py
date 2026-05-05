"""Phase 2 tests for the social-traces plugin scaffold.

Coverage focus (matching ``docs/plans/social-traces-plugin.md`` §10
Phase 2 acceptance):

* Plugin manifest discovers correctly via ``plugins.discovery.discover``.
* On-disk state machine: ``read_state`` / ``set_enabled`` / ``is_enabled``
  / heartbeat round-trip cleanly with no pre-existing files.
* Per-profile ``submitter_hash`` (``identity.get_or_create_agent_id``)
  is stable across reads and unique across profiles.
* Config parser tolerates missing/partial sections and falls back to
  documented defaults.
* The BEFORE_TASK hook handler is a no-op when the on-disk flag is
  off, and emits a heartbeat (still ``pass``) when the flag is on —
  the behaviour Phase 4 will swap for real query/inject logic.
* ``runtime.custom["trace_used"] = None`` invariant holds when the
  flag is on (so post-task subscriber sees a uniform shape).

These all run without OpenHub existing — the local profile filesystem
is the only state.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# ─── alias bootstrap (test-time only) ────────────────────────────────
# Same shape as ``cli_traces._ensure_alias`` and the equivalent for
# skill-evolution. Maps the hyphenated ``extensions/social-traces/``
# directory onto a Python module path ``extensions.social_traces``.

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_ST_DIR = _EXT_DIR / "social-traces"


def _ensure_alias() -> None:
    if "extensions.social_traces.state" in sys.modules:
        return
    if "extensions" not in sys.modules:
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(_EXT_DIR)]
        ext_pkg.__package__ = "extensions"
        sys.modules["extensions"] = ext_pkg
    if "extensions.social_traces" not in sys.modules:
        mod = types.ModuleType("extensions.social_traces")
        mod.__path__ = [str(_ST_DIR)]
        mod.__package__ = "extensions.social_traces"
        sys.modules["extensions.social_traces"] = mod
        sys.modules["extensions"].social_traces = mod  # type: ignore[attr-defined]
    parent = sys.modules["extensions.social_traces"]
    for sub in ("state", "identity", "config", "prefetch", "subscriber"):
        full_name = f"extensions.social_traces.{sub}"
        if full_name in sys.modules:
            setattr(parent, sub, sys.modules[full_name])
            continue
        init = _ST_DIR / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.social_traces"
        sys.modules[full_name] = sub_mod
        spec.loader.exec_module(sub_mod)
        setattr(parent, sub, sub_mod)


_ensure_alias()

# Now the plugin modules are importable under their underscore name.
from extensions.social_traces import config as st_config  # noqa: E402
from extensions.social_traces import identity as st_identity  # noqa: E402
from extensions.social_traces import prefetch as st_prefetch  # noqa: E402
from extensions.social_traces import state as st_state  # noqa: E402


# ─── manifest discovery ──────────────────────────────────────────────


def test_plugin_manifest_discovers():
    """The plugin must show up in ``discover()`` output. Smoke-tests
    that ``plugin.json`` parses cleanly under the loader's pydantic
    validation."""
    from opencomputer.plugins.discovery import discover

    candidates = discover([_EXT_DIR])
    ids = {c.manifest.id for c in candidates}
    assert "social-traces" in ids


def test_plugin_manifest_fields():
    """Manifest fields match plan §6.1 (kind=mixed, default-disabled)."""
    from opencomputer.plugins.discovery import discover

    candidates = discover([_EXT_DIR])
    by_id = {c.manifest.id: c.manifest for c in candidates}
    m = by_id["social-traces"]
    assert m.kind == "mixed"
    assert m.enabled_by_default is False
    assert m.entry == "plugin"
    assert m.version == "0.1.0"


# ─── state.py round-trip ──────────────────────────────────────────────


def test_state_missing_file_is_disabled(tmp_path: Path):
    """Missing state file = disabled. The plugin ships opt-in."""
    assert st_state.is_enabled(tmp_path) is False
    assert st_state.read_state(tmp_path) == {}


def test_state_set_enabled_round_trip(tmp_path: Path):
    """set_enabled(True) → is_enabled True; set_enabled(False) → False."""
    st_state.set_enabled(tmp_path, True)
    assert st_state.is_enabled(tmp_path) is True
    assert st_state.state_path(tmp_path).exists()

    st_state.set_enabled(tmp_path, False)
    assert st_state.is_enabled(tmp_path) is False


def test_state_preserves_other_keys(tmp_path: Path):
    """Toggling enabled must not nuke other keys an operator may have
    written manually (forwards-compat with future state additions)."""
    st_state.write_state(tmp_path, {"enabled": True, "custom_key": "hello"})
    st_state.set_enabled(tmp_path, False)
    assert st_state.read_state(tmp_path) == {
        "enabled": False,
        "custom_key": "hello",
    }


def test_state_malformed_file_treated_as_disabled(tmp_path: Path):
    """A corrupted state.json must not crash — log + treat as disabled."""
    st_state.traces_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    st_state.state_path(tmp_path).write_text("not-json", encoding="utf-8")
    assert st_state.is_enabled(tmp_path) is False


def test_heartbeat_round_trip(tmp_path: Path):
    """write_heartbeat / read_heartbeat round-trip a float timestamp."""
    assert st_state.read_heartbeat(tmp_path) == 0.0
    st_state.write_heartbeat(tmp_path)
    ts = st_state.read_heartbeat(tmp_path)
    assert ts > 0.0


# ─── identity.py ──────────────────────────────────────────────────────


def test_agent_id_generated_on_first_call(tmp_path: Path):
    """First call writes a fresh hex id; subsequent calls return the same."""
    aid = st_identity.get_or_create_agent_id(tmp_path)
    assert len(aid) == st_identity.AGENT_ID_BYTES * 2  # hex doubles bytes
    assert all(c in "0123456789abcdef" for c in aid)
    aid2 = st_identity.get_or_create_agent_id(tmp_path)
    assert aid2 == aid


def test_agent_id_unique_per_profile(tmp_path_factory):
    """Two different profile homes get two different agent ids — the
    network can rate-limit + trust-score per agent without correlating
    them."""
    a = tmp_path_factory.mktemp("profile_a")
    b = tmp_path_factory.mktemp("profile_b")
    aid_a = st_identity.get_or_create_agent_id(a)
    aid_b = st_identity.get_or_create_agent_id(b)
    assert aid_a != aid_b


# ─── config.py parsing ───────────────────────────────────────────────


def test_config_default_when_no_section():
    """Missing or non-dict ``social_traces:`` section → all defaults."""
    cfg = st_config.from_config_dict(None)
    assert cfg.enabled is False
    assert cfg.backend == "local"
    assert cfg.endpoint == "http://localhost:8000"
    assert cfg.privacy.redact_paths is True
    assert cfg.privacy.redact_hostnames is True
    assert cfg.novelty_judge.enabled is True
    assert cfg.query.soft_timeout_s == st_config.DEFAULT_QUERY_TIMEOUT_S
    assert cfg.query.top_k == st_config.DEFAULT_TOP_K
    assert cfg.query.relevance_threshold == st_config.DEFAULT_RELEVANCE_THRESHOLD
    assert cfg.outbox.max_pending == st_config.DEFAULT_MAX_OUTBOX


def test_config_partial_section_uses_defaults_for_missing():
    raw = {"backend": "http", "endpoint": "https://hub.example"}
    cfg = st_config.from_config_dict(raw)
    assert cfg.backend == "http"
    assert cfg.endpoint == "https://hub.example"
    # everything else defaults
    assert cfg.privacy.redact_paths is True
    assert cfg.query.top_k == st_config.DEFAULT_TOP_K


def test_config_full_round_trip():
    raw = {
        "enabled": True,
        "backend": "http",
        "endpoint": "https://hub.example",
        "privacy": {
            "redact_paths": False,
            "redact_hostnames": False,
            "extra_redactors": ["my_redactor"],
        },
        "novelty_judge": {
            "enabled": False,
            "cost_guard_usd_per_session": 0.10,
        },
        "query": {
            "soft_timeout_s": 0.5,
            "top_k": 5,
            "relevance_threshold": 0.8,
        },
        "outbox": {"max_pending": 50},
    }
    cfg = st_config.from_config_dict(raw)
    assert cfg.enabled is True
    assert cfg.privacy.redact_paths is False
    assert cfg.privacy.extra_redactors == ("my_redactor",)
    assert cfg.novelty_judge.enabled is False
    assert cfg.novelty_judge.cost_guard_usd_per_session == 0.10
    assert cfg.query.soft_timeout_s == 0.5
    assert cfg.query.top_k == 5
    assert cfg.query.relevance_threshold == 0.8
    assert cfg.outbox.max_pending == 50


def test_config_dataclasses_are_frozen():
    """Plugin config is read-only — same contract as plugin_sdk types."""
    import dataclasses

    cfg = st_config.SocialTracesConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.backend = "http"  # type: ignore[misc]


# ─── prefetch handler — Phase 2 stub semantics ────────────────────────


async def test_prefetch_returns_pass_when_disabled(tmp_path: Path):
    """Stub returns pass when on-disk flag is off — the common path
    most users will be on. No heartbeat, no work."""
    from plugin_sdk.hooks import HookContext, HookEvent
    from plugin_sdk.runtime_context import RuntimeContext

    runtime = RuntimeContext(custom={"profile_home": str(tmp_path)})
    ctx = HookContext(
        event=HookEvent.BEFORE_TASK,
        session_id="sid",
        runtime=runtime,
    )

    decision = await st_prefetch.on_before_task(ctx)
    assert decision.decision == "pass"
    # Disabled → no heartbeat written.
    assert st_state.read_heartbeat(tmp_path) == 0.0


async def test_prefetch_returns_pass_when_enabled_and_writes_heartbeat(tmp_path: Path):
    """Phase 2 stub: even when enabled, the handler returns pass (no
    real query yet) but writes a heartbeat so operators can confirm
    the wiring."""
    from plugin_sdk.hooks import HookContext, HookEvent
    from plugin_sdk.runtime_context import RuntimeContext

    st_state.set_enabled(tmp_path, True)

    runtime = RuntimeContext(custom={"profile_home": str(tmp_path)})
    ctx = HookContext(
        event=HookEvent.BEFORE_TASK,
        session_id="sid",
        runtime=runtime,
    )

    decision = await st_prefetch.on_before_task(ctx)
    assert decision.decision == "pass"
    # Enabled → heartbeat now exists.
    assert st_state.read_heartbeat(tmp_path) > 0.0


async def test_prefetch_sets_trace_used_flag_to_none_when_enabled(tmp_path: Path):
    """Even though Phase 2 doesn't fetch a real trace, it must set
    ``runtime.custom['trace_used'] = None`` so the post-task
    subscriber sees a uniform shape."""
    from plugin_sdk.hooks import HookContext, HookEvent
    from plugin_sdk.runtime_context import RuntimeContext

    st_state.set_enabled(tmp_path, True)
    custom: dict = {"profile_home": str(tmp_path)}
    runtime = RuntimeContext(custom=custom)
    ctx = HookContext(
        event=HookEvent.BEFORE_TASK,
        session_id="sid",
        runtime=runtime,
    )

    await st_prefetch.on_before_task(ctx)
    assert "trace_used" in custom
    assert custom["trace_used"] is None


async def test_prefetch_no_runtime_returns_pass(tmp_path: Path):
    """Defensive: a HookContext with runtime=None must still return
    pass without crashing. (Edge case for tests / direct callers.)"""
    from plugin_sdk.hooks import HookContext, HookEvent

    ctx = HookContext(event=HookEvent.BEFORE_TASK, session_id="sid")
    decision = await st_prefetch.on_before_task(ctx)
    assert decision.decision == "pass"


# ─── plugin.register exercises the surface ───────────────────────────


def test_plugin_register_attaches_before_task_hook(tmp_path: Path):
    """Loading the plugin via PluginAPI must register exactly one
    handler against BEFORE_TASK, with the documented priority."""
    # Re-load plugin.py through the loader's path so we get a fresh
    # registration into a scratch HookEngine.
    from opencomputer.hooks.engine import HookEngine
    from plugin_sdk.hooks import HookEvent

    engine = HookEngine()

    class _StubAPI:
        def register_hook(self, spec):
            engine.register(spec)

    spec = importlib.util.spec_from_file_location(
        "extensions.social_traces._plugin_under_test",
        str(_ST_DIR / "plugin.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "extensions.social_traces"
    sys.modules["extensions.social_traces._plugin_under_test"] = mod
    spec.loader.exec_module(mod)

    mod.register(_StubAPI())

    handlers = engine._ordered_specs(HookEvent.BEFORE_TASK)
    assert len(handlers) == 1
    s = handlers[0]
    assert s.event == HookEvent.BEFORE_TASK
    assert s.fire_and_forget is False
    assert s.priority == 20
    assert s.timeout_ms == 1500
