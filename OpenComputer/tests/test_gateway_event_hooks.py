"""Tests for the gateway file-discovery hook system (Hermes Doc-2 parity, 2026-05-08).

Covers:
* Discovery: a valid hook directory yields a GatewayHook with the
  declared events + loaded handler.
* Discovery: invalid manifests / broken handlers are skipped, not raised.
* Wildcard matching: ``command:*`` subscribers receive ``command:run`` etc.
* Dispatch: ``engine.fire`` invokes every subscribed handler concurrently.
* Dispatch: a handler that raises is logged but does not break siblings.
* Module-cache safety: two hook directories with ``handler.py`` filenames
  load as distinct modules (not the same handler twice).
* BOOT.md handler: silent no-op when BOOT.md is absent; runs aux LLM
  when present + logs response.

Tests use a per-test ``OPENCOMPUTER_HOME`` so they don't touch the
developer's real ``~/.opencomputer`` directory.
"""
from __future__ import annotations

import asyncio
import os
import textwrap
from pathlib import Path

import pytest

from opencomputer.gateway import boot_md
from opencomputer.gateway.event_hooks import (
    GATEWAY_STARTUP,
    GatewayHook,
    GatewayHookEngine,
    discover_hooks,
)


@pytest.fixture()
def tmp_oc_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "hooks").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _make_hook(
    home: Path,
    name: str,
    *,
    events: list[str],
    handler_src: str | None = None,
) -> Path:
    """Drop a HOOK.yaml + handler.py into ``home/hooks/<name>/``."""
    hook_dir = home / "hooks" / name
    hook_dir.mkdir(parents=True, exist_ok=True)
    yaml_lines = ["events:"]
    yaml_lines.extend(f"  - {e}" for e in events)
    (hook_dir / "HOOK.yaml").write_text("\n".join(yaml_lines), encoding="utf-8")
    if handler_src is None:
        handler_src = textwrap.dedent("""
            CALLS = []
            async def handle(event_type, context):
                CALLS.append((event_type, dict(context)))
        """).strip()
    (hook_dir / "handler.py").write_text(handler_src, encoding="utf-8")
    return hook_dir


# ─── Discovery ─────────────────────────────────────────────────────────


def test_discovery_returns_valid_hook(tmp_oc_home: Path) -> None:
    _make_hook(tmp_oc_home, "log-startups", events=[GATEWAY_STARTUP])
    hooks = discover_hooks(tmp_oc_home / "hooks")
    assert len(hooks) == 1
    assert hooks[0].name == "log-startups"
    assert hooks[0].events == [GATEWAY_STARTUP]
    assert callable(hooks[0].handler)


def test_discovery_skips_directory_missing_manifest(tmp_oc_home: Path) -> None:
    """A directory with handler.py but no HOOK.yaml is silently skipped."""
    rogue = tmp_oc_home / "hooks" / "no-manifest"
    rogue.mkdir(parents=True)
    (rogue / "handler.py").write_text(
        "async def handle(et, ctx): pass", encoding="utf-8",
    )
    hooks = discover_hooks(tmp_oc_home / "hooks")
    assert hooks == []


def test_discovery_skips_directory_with_broken_handler(
    tmp_oc_home: Path, caplog,
) -> None:
    _make_hook(
        tmp_oc_home, "broken",
        events=[GATEWAY_STARTUP],
        handler_src="this is not valid python",
    )
    with caplog.at_level("WARNING"):
        hooks = discover_hooks(tmp_oc_home / "hooks")
    assert hooks == []
    assert any("failed to import" in r.message for r in caplog.records)


def test_discovery_skips_handler_missing_handle_function(
    tmp_oc_home: Path, caplog,
) -> None:
    _make_hook(
        tmp_oc_home, "no-handle",
        events=[GATEWAY_STARTUP],
        handler_src="def something_else(): pass",
    )
    with caplog.at_level("WARNING"):
        hooks = discover_hooks(tmp_oc_home / "hooks")
    assert hooks == []
    assert any("failed to import" in r.message for r in caplog.records)


def test_discovery_skips_synchronous_handle_function(
    tmp_oc_home: Path, caplog,
) -> None:
    _make_hook(
        tmp_oc_home, "sync-handle",
        events=[GATEWAY_STARTUP],
        handler_src="def handle(event_type, context): pass",
    )
    with caplog.at_level("WARNING"):
        hooks = discover_hooks(tmp_oc_home / "hooks")
    assert hooks == []
    assert any("async def" in r.message for r in caplog.records)


# ─── Module-cache safety ───────────────────────────────────────────────


def test_two_hook_directories_with_same_filename_are_isolated(
    tmp_oc_home: Path,
) -> None:
    """Two separate hooks must NOT share the same loaded handler.py."""
    _make_hook(
        tmp_oc_home, "first",
        events=[GATEWAY_STARTUP],
        handler_src=textwrap.dedent("""
            ID = 'first'
            async def handle(et, ctx): pass
        """).strip(),
    )
    _make_hook(
        tmp_oc_home, "second",
        events=[GATEWAY_STARTUP],
        handler_src=textwrap.dedent("""
            ID = 'second'
            async def handle(et, ctx): pass
        """).strip(),
    )
    hooks = discover_hooks(tmp_oc_home / "hooks")
    assert len(hooks) == 2
    # Their handlers' module objects must be distinct.
    mod_one = hooks[0].handler.__module__  # type: ignore[union-attr]
    mod_two = hooks[1].handler.__module__  # type: ignore[union-attr]
    assert mod_one != mod_two


# ─── Matching ──────────────────────────────────────────────────────────


def test_wildcard_command_event_matches_specific_commands() -> None:
    h = GatewayHook(
        name="any-command", path=Path("/tmp"), events=["command:*"], handler=None,
    )
    assert h.matches("command:run") is True
    assert h.matches("command:status") is True
    assert h.matches("session:start") is False  # different prefix


def test_exact_event_matches() -> None:
    h = GatewayHook(
        name="startup-only", path=Path("/tmp"),
        events=[GATEWAY_STARTUP], handler=None,
    )
    assert h.matches(GATEWAY_STARTUP) is True
    assert h.matches("session:start") is False


# ─── Dispatch ──────────────────────────────────────────────────────────


def test_engine_fire_invokes_subscribed_handlers(tmp_oc_home: Path) -> None:
    received: list[tuple[str, dict]] = []

    async def _h1(event_type: str, context: dict) -> None:
        received.append(("h1", dict(context)))

    async def _h2(event_type: str, context: dict) -> None:
        received.append(("h2", dict(context)))

    engine = GatewayHookEngine()
    engine._hooks = [
        GatewayHook(name="h1", path=Path("/x"), events=[GATEWAY_STARTUP], handler=_h1),
        GatewayHook(name="h2", path=Path("/y"), events=[GATEWAY_STARTUP], handler=_h2),
        GatewayHook(name="h3", path=Path("/z"), events=["unrelated"], handler=_h1),
    ]
    asyncio.run(engine.fire(GATEWAY_STARTUP, {"platforms": ["telegram"]}))
    names = {n for n, _ in received}
    assert names == {"h1", "h2"}  # h3 not subscribed
    for _, ctx in received:
        assert ctx["platforms"] == ["telegram"]
        assert ctx["event"] == GATEWAY_STARTUP


def test_handler_exception_does_not_break_siblings(
    tmp_oc_home: Path, caplog,
) -> None:
    received: list[str] = []

    async def _broken(event_type: str, context: dict) -> None:
        raise RuntimeError("boom")

    async def _ok(event_type: str, context: dict) -> None:
        received.append(event_type)

    engine = GatewayHookEngine()
    engine._hooks = [
        GatewayHook(
            name="broken", path=Path("/x"), events=[GATEWAY_STARTUP],
            handler=_broken,
        ),
        GatewayHook(
            name="ok", path=Path("/y"), events=[GATEWAY_STARTUP], handler=_ok,
        ),
    ]
    with caplog.at_level("WARNING"):
        asyncio.run(engine.fire(GATEWAY_STARTUP))
    assert received == [GATEWAY_STARTUP]
    assert any("raised" in r.message for r in caplog.records)


def test_fire_no_subscribers_is_a_noop() -> None:
    engine = GatewayHookEngine()
    # Empty engine — no hooks at all. Should not raise.
    asyncio.run(engine.fire(GATEWAY_STARTUP, {}))


# ─── BOOT.md ───────────────────────────────────────────────────────────


def test_boot_md_handler_no_op_when_file_absent(tmp_oc_home: Path) -> None:
    # Path is in tmp_oc_home, BOOT.md not created — handler should silent return.
    asyncio.run(boot_md.boot_md_handler(GATEWAY_STARTUP, {}))


def test_boot_md_handler_runs_aux_llm_when_file_present(
    tmp_oc_home: Path, monkeypatch, caplog,
) -> None:
    boot_path = tmp_oc_home / "BOOT.md"
    boot_path.write_text("Check that everything is online.", encoding="utf-8")

    captured: list[str] = []

    async def _fake_complete_text(**kwargs):
        captured.append(kwargs["messages"][0]["content"])
        return "everything online; nothing to do."

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text", _fake_complete_text,
    )

    with caplog.at_level("INFO"):
        asyncio.run(boot_md.boot_md_handler(GATEWAY_STARTUP, {}))
    assert captured == ["Check that everything is online."]
    assert any("BOOT.md ran" in r.message for r in caplog.records)


def test_boot_md_handler_silent_marker_suppresses_log_output(
    tmp_oc_home: Path, monkeypatch, caplog,
) -> None:
    (tmp_oc_home / "BOOT.md").write_text("noop please", encoding="utf-8")

    async def _fake(**kwargs):
        return boot_md.SILENT_MARKER

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text", _fake,
    )
    with caplog.at_level("INFO"):
        asyncio.run(boot_md.boot_md_handler(GATEWAY_STARTUP, {}))
    # The silent path logs "ran silently"; the response text never logged.
    info_messages = [r.message for r in caplog.records]
    assert any("ran silently" in m for m in info_messages)


def test_boot_md_handler_handles_provider_failure_gracefully(
    tmp_oc_home: Path, monkeypatch, caplog,
) -> None:
    (tmp_oc_home / "BOOT.md").write_text("provider down test", encoding="utf-8")

    async def _broken(**kwargs):
        raise RuntimeError("provider 503")

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text", _broken,
    )
    with caplog.at_level("WARNING"):
        # Must not raise, must not crash the gateway.
        asyncio.run(boot_md.boot_md_handler(GATEWAY_STARTUP, {}))
    assert any(
        "BOOT.md model call failed" in r.message for r in caplog.records
    )
