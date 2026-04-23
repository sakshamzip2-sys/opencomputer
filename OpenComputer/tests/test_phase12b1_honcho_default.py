"""Phase 12b1 / Sub-project A — cron-mode guard + three-mode Honcho provider.

Tasks covered:
  * A1 — cron/flush guard on ``MemoryBridge`` + ``RuntimeContext.agent_context``.
  * A2 — three-mode ``HonchoSelfHostedProvider`` (``context`` / ``tools`` /
    ``hybrid``). Mirrors Hermes' recall_mode at
    ``sources/hermes-agent/plugins/memory/honcho/__init__.py:155-200``.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opencomputer.agent.memory_bridge import MemoryBridge
from plugin_sdk.runtime_context import RuntimeContext

# ─── Honcho provider loader (same pattern as test_phase10f_honcho.py) ───

_HONCHO_EXT_DIR = (
    Path(__file__).resolve().parent.parent / "extensions" / "memory-honcho"
)


def _load_honcho_provider_module():
    """Load ``extensions/memory-honcho/provider.py`` under a synthetic package.

    The extension dir has a hyphen so it's not an importable package — use
    ``importlib.util`` the same way the real plugin loader does. Caches on
    ``sys.modules`` under a unique package name so repeated calls reuse the
    same module.
    """
    import sys

    pkg_name = "_honcho_a2_test_pkg"
    if f"{pkg_name}.provider" in sys.modules:
        return sys.modules[f"{pkg_name}.provider"]
    pkg_spec = importlib.machinery.ModuleSpec(
        pkg_name, loader=None, origin=str(_HONCHO_EXT_DIR), is_package=True
    )
    pkg_spec.submodule_search_locations = [str(_HONCHO_EXT_DIR)]
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules[pkg_name] = pkg
    prov_spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.provider", _HONCHO_EXT_DIR / "provider.py"
    )
    prov_mod = importlib.util.module_from_spec(prov_spec)
    sys.modules[f"{pkg_name}.provider"] = prov_mod
    prov_spec.loader.exec_module(prov_mod)
    return prov_mod


class _ExplodingProvider:
    """A fake MemoryProvider whose ``prefetch`` MUST NOT be called.

    If the bridge's cron/flush guard works correctly, this provider's
    ``prefetch`` will never be awaited, so the test can prove the guard
    short-circuited.
    """

    provider_id = "exploding-test-provider"

    async def prefetch(
        self, query: str, turn_index: int
    ) -> str | None:  # pragma: no cover
        raise AssertionError(
            "provider.prefetch should not have been called in cron/flush mode"
        )

    async def sync_turn(
        self, user: str, assistant: str, turn_index: int
    ) -> None:  # pragma: no cover
        raise AssertionError("sync_turn should not be called in this test")

    async def health_check(self) -> bool:  # pragma: no cover
        return True

    def tool_schemas(self) -> list:  # pragma: no cover
        return []

    async def handle_tool_call(self, call: Any) -> Any:  # pragma: no cover
        return None


class _FakeMemoryContext:
    """Minimal stand-in for ``MemoryContext`` used by ``MemoryBridge``.

    The bridge only reads ``.provider`` and ``._failure_state`` off the
    context — duck typing is enough.
    """

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self._failure_state: dict[str, Any] = {}


@pytest.mark.asyncio
async def test_memory_bridge_skips_provider_in_cron_context() -> None:
    """When ``agent_context="cron"``, the bridge returns None without
    calling the provider."""
    provider = _ExplodingProvider()
    ctx = _FakeMemoryContext(provider)
    bridge = MemoryBridge(ctx)

    runtime = RuntimeContext(agent_context="cron")
    result = await bridge.prefetch("any query", turn_index=0, runtime=runtime)

    assert result is None


@pytest.mark.asyncio
async def test_memory_bridge_skips_provider_in_flush_context() -> None:
    """When ``agent_context="flush"``, the bridge returns None without
    calling the provider."""
    provider = _ExplodingProvider()
    ctx = _FakeMemoryContext(provider)
    bridge = MemoryBridge(ctx)

    runtime = RuntimeContext(agent_context="flush")
    result = await bridge.prefetch("any query", turn_index=0, runtime=runtime)

    assert result is None


@pytest.mark.asyncio
async def test_memory_bridge_calls_provider_in_default_chat_context() -> None:
    """Default ``agent_context="chat"`` (and runtime=None) still hits the
    provider — the guard must not over-reach."""

    class _RecordingProvider:
        provider_id = "recording-test-provider"

        def __init__(self) -> None:
            self.prefetch_mock = AsyncMock(return_value="from-provider")

        async def prefetch(self, query: str, turn_index: int) -> str | None:
            return await self.prefetch_mock(query, turn_index)

    # Case A: explicit chat runtime
    provider_a = _RecordingProvider()
    bridge_a = MemoryBridge(_FakeMemoryContext(provider_a))
    result_a = await bridge_a.prefetch(
        "hello", turn_index=0, runtime=RuntimeContext(agent_context="chat")
    )
    assert result_a == "from-provider"
    provider_a.prefetch_mock.assert_awaited_once_with("hello", 0)

    # Case B: no runtime at all (backwards compat — existing callers)
    provider_b = _RecordingProvider()
    bridge_b = MemoryBridge(_FakeMemoryContext(provider_b))
    result_b = await bridge_b.prefetch("hi", turn_index=1)
    assert result_b == "from-provider"
    provider_b.prefetch_mock.assert_awaited_once_with("hi", 1)


@pytest.mark.asyncio
async def test_memory_bridge_sync_turn_skips_provider_in_cron_context() -> None:
    """Symmetric with prefetch: cron turns that complete must not
    ``provider.sync_turn`` on the way out — otherwise the guard only covers
    read and leaks on write."""
    provider = _ExplodingProvider()
    ctx = _FakeMemoryContext(provider)
    bridge = MemoryBridge(ctx)

    runtime = RuntimeContext(agent_context="cron")
    # Must not raise — _ExplodingProvider.sync_turn would AssertionError if called.
    await bridge.sync_turn("user msg", "assistant reply", turn_index=0, runtime=runtime)


@pytest.mark.asyncio
async def test_memory_bridge_sync_turn_calls_provider_in_chat_context() -> None:
    """Default chat context still syncs — the guard must not over-reach."""

    class _RecordingProvider:
        provider_id = "recording-test-provider"

        def __init__(self) -> None:
            self.sync_mock = AsyncMock(return_value=None)

        async def prefetch(
            self, query: str, turn_index: int
        ) -> str | None:  # pragma: no cover
            return None

        async def sync_turn(self, user: str, assistant: str, turn_index: int) -> None:
            await self.sync_mock(user, assistant, turn_index)

    provider = _RecordingProvider()
    bridge = MemoryBridge(_FakeMemoryContext(provider))
    await bridge.sync_turn(
        "u", "a", turn_index=3, runtime=RuntimeContext(agent_context="chat")
    )
    provider.sync_mock.assert_awaited_once_with("u", "a", 3)


# ─── Phase 12b1 Task A4 — default-on flag in plugin manifest + config ──


def test_memory_config_defaults_provider_to_memory_honcho() -> None:
    """A4: MemoryConfig().provider defaults to "memory-honcho" (not "")
    so a fresh install tries Honcho first; wizard downgrades to "" only
    when Docker is confirmed absent."""
    from opencomputer.agent.config import MemoryConfig

    assert MemoryConfig().provider == "memory-honcho"


_HONCHO_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "extensions" / "memory-honcho" / "plugin.json"
)


def test_memory_honcho_plugin_manifest_has_enabled_by_default_true() -> None:
    """A4: extensions/memory-honcho/plugin.json now has
    enabled_by_default=True, which the loader surfaces to the wizard."""
    import json

    data = json.loads(_HONCHO_MANIFEST_PATH.read_text())
    assert data.get("enabled_by_default") is True


def test_plugin_manifest_schema_accepts_enabled_by_default() -> None:
    """A4: both the pydantic schema and the frozen dataclass honour the
    new field. Parsing memory-honcho's manifest returns a manifest with
    enabled_by_default=True and no validation error."""
    from opencomputer.plugins.discovery import _parse_manifest

    manifest = _parse_manifest(_HONCHO_MANIFEST_PATH)
    assert manifest is not None
    assert manifest.enabled_by_default is True


# ─── Phase 12b1 Task A2 — three-mode HonchoSelfHostedProvider ───────────


def test_honcho_provider_defaults_to_context_mode() -> None:
    """No ``mode`` kwarg → provider defaults to ``"context"`` (back-compat
    for existing loaders / configs that don't know about the flag)."""
    prov_mod = _load_honcho_provider_module()
    prov = prov_mod.HonchoSelfHostedProvider()
    assert prov.mode == "context"


def test_honcho_provider_accepts_all_three_valid_modes() -> None:
    """Each of the three documented modes round-trips onto ``self.mode``."""
    prov_mod = _load_honcho_provider_module()
    for mode in ("context", "tools", "hybrid"):
        prov = prov_mod.HonchoSelfHostedProvider(mode=mode)
        assert prov.mode == mode, f"mode={mode!r} did not round-trip"


def test_honcho_provider_rejects_unknown_mode() -> None:
    """Unknown mode string → ``ValueError`` at construction time, with a
    message that names the field and lists the valid set."""
    prov_mod = _load_honcho_provider_module()
    with pytest.raises(ValueError) as excinfo:
        prov_mod.HonchoSelfHostedProvider(mode="bogus")
    msg = str(excinfo.value)
    assert "mode" in msg
    # All three valid values must appear in the error so the user can fix it.
    for valid in ("context", "tools", "hybrid"):
        assert valid in msg, f"error message should list {valid!r}: {msg!r}"


# ─── Phase 12b1 Task A5 — silent-on-Docker wizard activation ────────────


def _make_recording_console(monkeypatch):
    """Swap ``setup_wizard.console`` for a Console writing into an in-memory
    buffer. Returns the buffer — callers read ``buf.getvalue()``.

    Needed because rich.Console auto-detects terminal width / stream and
    pytest's ``capsys`` sometimes races with that detection; a dedicated
    buffer is deterministic.
    """
    import io

    from rich.console import Console as _RichConsole

    from opencomputer import setup_wizard as _wiz

    buf = io.StringIO()
    recording = _RichConsole(file=buf, force_terminal=False, width=200, record=True)
    monkeypatch.setattr(_wiz, "console", recording)
    return buf


class _FakeBootstrap:
    """Stand-in for the memory-honcho bootstrap module used by the wizard.

    The real loader reaches into ``extensions/memory-honcho/bootstrap.py``
    via importlib — here we just expose the two attributes the wizard
    currently cares about (``detect_docker`` + ``ensure_started``) so a
    monkeypatch on ``_load_honcho_bootstrap`` can feed in pre-scripted
    return values without doing any real subprocess work.
    """

    def __init__(self, *, detect_return, ensure_return) -> None:
        self._detect_return = detect_return
        self._ensure_return = ensure_return
        self.detect_calls = 0
        self.ensure_calls = 0
        self.honcho_up_calls = 0

    def detect_docker(self):
        self.detect_calls += 1
        return self._detect_return

    def ensure_started(self, timeout_s: int = 60):
        self.ensure_calls += 1
        self._ensure_timeout = timeout_s
        return self._ensure_return

    def honcho_up(self):  # pragma: no cover — must NOT be called anymore
        self.honcho_up_calls += 1
        raise AssertionError(
            "honcho_up() must no longer be called from the wizard — "
            "A5 replaced it with ensure_started()."
        )


def _install_fake_bootstrap(monkeypatch, fake):
    """Point both the wizard's bootstrap loader and cli_memory's loader at
    ``fake`` so the test doesn't depend on which one the wizard imports."""
    from opencomputer import cli_memory, setup_wizard

    monkeypatch.setattr(cli_memory, "_load_honcho_bootstrap", lambda: fake)
    # The wizard may have its own loader symbol after A5 — patch both
    # names defensively so the test is resilient to the refactor choosing
    # either name.
    if hasattr(setup_wizard, "_load_honcho_bootstrap"):
        monkeypatch.setattr(setup_wizard, "_load_honcho_bootstrap", lambda: fake)


def test_wizard_starts_honcho_when_docker_present(monkeypatch) -> None:
    """Docker + compose v2 present → wizard silently auto-starts Honcho via
    ``bootstrap.ensure_started`` (NOT the old ``honcho_up``) and never
    asks the user to confirm."""
    from unittest.mock import patch

    from opencomputer import setup_wizard

    fake = _FakeBootstrap(
        detect_return=(True, True),
        ensure_return=(True, "Honcho stack already running and healthy."),
    )
    _install_fake_bootstrap(monkeypatch, fake)
    buf = _make_recording_console(monkeypatch)

    with patch("opencomputer.setup_wizard.Confirm.ask") as mock_confirm:
        setup_wizard._optional_honcho()

    assert fake.ensure_calls == 1, "ensure_started should be called exactly once"
    assert fake.honcho_up_calls == 0, "old honcho_up path must not be taken"
    assert mock_confirm.call_count == 0, "A5 removed the Confirm.ask prompt"

    out = buf.getvalue()
    assert "Honcho memory running" in out, f"expected success banner, got {out!r}"


def test_wizard_silent_fallback_when_docker_absent(monkeypatch) -> None:
    """No Docker → print non-alarming notice, skip ensure_started entirely,
    and persist ``provider=""`` to config so the next run doesn't retry."""
    from unittest.mock import patch

    from opencomputer import setup_wizard

    fake = _FakeBootstrap(
        detect_return=(False, False),
        ensure_return=(True, "unreachable"),  # must NOT be used
    )
    _install_fake_bootstrap(monkeypatch, fake)
    buf = _make_recording_console(monkeypatch)

    saved_cfgs: list = []

    def _capture_save(cfg, path=None):
        saved_cfgs.append(cfg)
        return path

    monkeypatch.setattr(setup_wizard, "save_config", _capture_save)

    # Wizard reads current config via load_config — return defaults so the
    # write path has something to mutate.
    from opencomputer.agent.config import default_config

    monkeypatch.setattr(setup_wizard, "load_config", lambda: default_config())

    with patch("opencomputer.setup_wizard.Confirm.ask") as mock_confirm:
        setup_wizard._optional_honcho()

    assert fake.ensure_calls == 0, "ensure_started must not be called without Docker"
    assert mock_confirm.call_count == 0, "A5 removed the Confirm.ask prompt"

    out = buf.getvalue()
    assert "baseline memory" in out, f"expected baseline notice, got {out!r}"
    assert "docs.docker.com/get-docker" in out, (
        f"expected docker install URL in notice, got {out!r}"
    )

    assert saved_cfgs, "save_config should have been called to persist provider=''"
    assert saved_cfgs[-1].memory.provider == "", (
        "config should be updated with provider='' when Docker absent"
    )


def test_wizard_silent_fallback_when_ensure_started_fails(monkeypatch) -> None:
    """Docker present but stack-start fails → surface the error, persist
    ``provider=""``, and do NOT propagate an exception."""
    from unittest.mock import patch

    from opencomputer import setup_wizard

    fake = _FakeBootstrap(
        detect_return=(True, True),
        ensure_return=(False, "Port 8000 already in use by another process"),
    )
    _install_fake_bootstrap(monkeypatch, fake)
    buf = _make_recording_console(monkeypatch)

    saved_cfgs: list = []
    monkeypatch.setattr(
        setup_wizard, "save_config", lambda cfg, path=None: saved_cfgs.append(cfg)
    )

    from opencomputer.agent.config import default_config

    monkeypatch.setattr(setup_wizard, "load_config", lambda: default_config())

    with patch("opencomputer.setup_wizard.Confirm.ask") as mock_confirm:
        # Must not raise — failure is handled gracefully.
        setup_wizard._optional_honcho()

    assert fake.ensure_calls == 1, "ensure_started should be attempted once"
    assert mock_confirm.call_count == 0, "A5 removed the Confirm.ask prompt"

    out = buf.getvalue()
    assert "Port 8000" in out or "port conflict" in out.lower() or "already in use" in out, (
        f"error message should surface to stdout, got {out!r}"
    )

    assert saved_cfgs, "save_config should be called to persist provider=''"
    assert saved_cfgs[-1].memory.provider == "", (
        "failed start should downgrade provider to '' so next run doesn't retry"
    )


def test_wizard_no_confirm_prompt_anymore(monkeypatch) -> None:
    """Regardless of Docker state, ``_optional_honcho`` must not invoke
    ``Confirm.ask``. Silent activation is the whole contract of A5."""
    from unittest.mock import patch

    from opencomputer import setup_wizard
    from opencomputer.agent.config import default_config

    monkeypatch.setattr(setup_wizard, "load_config", lambda: default_config())
    monkeypatch.setattr(setup_wizard, "save_config", lambda cfg, path=None: None)

    scenarios = [
        ((True, True), (True, "running")),
        ((True, True), (False, "broke")),
        ((False, False), (True, "n/a")),
        ((True, False), (True, "n/a")),
    ]

    for detect, ensure in scenarios:
        fake = _FakeBootstrap(detect_return=detect, ensure_return=ensure)
        _install_fake_bootstrap(monkeypatch, fake)
        _make_recording_console(monkeypatch)

        with patch("opencomputer.setup_wizard.Confirm.ask") as mock_confirm:
            setup_wizard._optional_honcho()
        assert mock_confirm.call_count == 0, (
            f"Confirm.ask was called with detect={detect}, ensure={ensure} — "
            f"A5 forbids any user prompt in _optional_honcho"
        )
