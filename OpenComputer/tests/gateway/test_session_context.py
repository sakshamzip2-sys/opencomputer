"""Tests for opencomputer.gateway.session_context (Task B8).

Verifies the contextvars-based per-task session state replaces the previous
``os.environ``-based approach so concurrent asyncio tasks no longer clobber
each other's session values.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from opencomputer.gateway import session_context as sc


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip any ambient OPENCOMPUTER_SESSION_* env vars between tests."""
    for key in list(os.environ):
        if key.startswith("OPENCOMPUTER_SESSION_") or key.startswith(
            "OPENCOMPUTER_CRON_AUTO_DELIVER_"
        ):
            monkeypatch.delenv(key, raising=False)
    yield


def _run(coro):
    """Drive ``coro`` to completion in a private event loop."""
    return asyncio.run(coro)


def test_get_session_env_returns_default_when_unset():
    """With no contextvar set and no env var, default is returned."""
    assert sc.get_session_env("OPENCOMPUTER_SESSION_PLATFORM") == ""
    assert sc.get_session_env("OPENCOMPUTER_SESSION_PLATFORM", "fallback") == "fallback"


def test_set_session_vars_then_get_session_env():
    """After ``set_session_vars`` the contextvar is visible via ``get_session_env``."""

    async def _main():
        sc.set_session_vars(platform="telegram", chat_id="123")
        assert sc.get_session_env("OPENCOMPUTER_SESSION_PLATFORM") == "telegram"
        assert sc.get_session_env("OPENCOMPUTER_SESSION_CHAT_ID") == "123"
        # Other vars default to "" (set, but to empty string).
        assert sc.get_session_env("OPENCOMPUTER_SESSION_USER_ID") == ""

    _run(_main())


def test_contextvar_falls_back_to_os_environ_when_unset(monkeypatch):
    """When the contextvar holds the _UNSET sentinel, env var is consulted."""
    monkeypatch.setenv("OPENCOMPUTER_SESSION_PLATFORM", "from_env")
    # Brand-new event loop — nothing has been ``set_session_vars``-ed.
    assert sc.get_session_env("OPENCOMPUTER_SESSION_PLATFORM") == "from_env"


def test_contextvar_overrides_os_environ(monkeypatch):
    """When both are set, the contextvar wins."""
    monkeypatch.setenv("OPENCOMPUTER_SESSION_PLATFORM", "from_env")

    async def _main():
        sc.set_session_vars(platform="from_ctxvar")
        assert sc.get_session_env("OPENCOMPUTER_SESSION_PLATFORM") == "from_ctxvar"

    _run(_main())


def test_clear_session_vars_returns_empty_not_env(monkeypatch):
    """``clear_session_vars`` sets to "" — explicitly empty, no env fallback."""
    monkeypatch.setenv("OPENCOMPUTER_SESSION_PLATFORM", "from_env")

    async def _main():
        sc.set_session_vars(platform="telegram")
        assert sc.get_session_env("OPENCOMPUTER_SESSION_PLATFORM") == "telegram"
        sc.clear_session_vars()
        # After clear, contextvar is "" — explicit empty, do NOT fall back to env.
        assert sc.get_session_env("OPENCOMPUTER_SESSION_PLATFORM") == ""

    _run(_main())


def test_concurrency_safety_two_tasks_isolated():
    """Two concurrent tasks must each see only their own session vars.

    This is THE point of contextvars: with ``os.environ`` the second task
    would clobber the first. With ContextVar each task gets its own copy.
    """
    saw_a: dict = {}
    saw_b: dict = {}

    async def _task(label: str, platform: str, chat_id: str, store: dict, gate: asyncio.Event):
        sc.set_session_vars(platform=platform, chat_id=chat_id)
        # Yield to the scheduler so the other task gets to run between
        # set_session_vars and our read. If contextvars are truly task-local,
        # the other task's writes must not affect what we observe.
        await gate.wait()
        store["platform"] = sc.get_session_env("OPENCOMPUTER_SESSION_PLATFORM")
        store["chat_id"] = sc.get_session_env("OPENCOMPUTER_SESSION_CHAT_ID")

    async def _main():
        gate = asyncio.Event()
        task_a = asyncio.create_task(_task("A", "telegram", "111", saw_a, gate))
        task_b = asyncio.create_task(_task("B", "slack", "222", saw_b, gate))
        # Let both tasks reach ``gate.wait()`` after writing their vars.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(task_a, task_b)

    _run(_main())

    assert saw_a == {"platform": "telegram", "chat_id": "111"}
    assert saw_b == {"platform": "slack", "chat_id": "222"}


def test_cron_delivery_independent_of_session_vars():
    """Cron auto-deliver vars don't pollute session vars or vice versa."""

    async def _main():
        sc.set_session_vars(platform="telegram", chat_id="111")
        sc.set_cron_delivery(platform="slack", chat_id="999", thread_id="t1")

        # Session vars unchanged
        assert sc.get_session_env("OPENCOMPUTER_SESSION_PLATFORM") == "telegram"
        assert sc.get_session_env("OPENCOMPUTER_SESSION_CHAT_ID") == "111"

        # Cron vars set
        assert sc.get_session_env("OPENCOMPUTER_CRON_AUTO_DELIVER_PLATFORM") == "slack"
        assert sc.get_session_env("OPENCOMPUTER_CRON_AUTO_DELIVER_CHAT_ID") == "999"
        assert sc.get_session_env("OPENCOMPUTER_CRON_AUTO_DELIVER_THREAD_ID") == "t1"

        # Clear cron only — session vars untouched
        sc.clear_cron_delivery()
        assert sc.get_session_env("OPENCOMPUTER_SESSION_PLATFORM") == "telegram"
        assert sc.get_session_env("OPENCOMPUTER_CRON_AUTO_DELIVER_PLATFORM") == ""

    _run(_main())
