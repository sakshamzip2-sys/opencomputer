"""Telegram adapter <-> scope-lock integration (hermes parity).

Pinned to the v2026.4.26 incident: Claude Code's Telegram adapter (PID
45409) was already polling the same bot OC tried to use, OC silently
saw zero updates. The fix takes a machine-local lock on the bot token
in ``connect()`` and refuses with a clear log line + holding PID when
the lock is held.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_lock_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENCOMPUTER_LOCK_DIR", str(tmp_path / "locks"))


def _load_telegram_adapter():
    """Load extensions/telegram/adapter.py as a unique module.

    The bundled-plugin loader uses synthetic module names to avoid the
    ``plugin.py``/``adapter.py`` collision documented in CLAUDE.md
    gotcha #1; tests that need the adapter class follow the same shape.
    """
    repo_root = Path(__file__).resolve().parent.parent
    adapter_path = repo_root / "extensions" / "telegram" / "adapter.py"
    spec = importlib.util.spec_from_file_location(
        "_test_telegram_adapter", adapter_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_connect_succeeds_when_no_other_client_holds_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path: lock is free → connect proceeds."""
    adapter_mod = _load_telegram_adapter()

    class FakeClient:
        async def get(self, url, params=None):
            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {
                        "ok": True,
                        "result": {"id": 12345, "username": "test_bot"},
                    }

                @staticmethod
                def raise_for_status():
                    return None

            return _R()

        async def aclose(self):
            return None

    monkeypatch.setattr(adapter_mod.httpx, "AsyncClient", lambda **k: FakeClient())

    # Stub out the polling task — return a real cancelled Task so the
    # adapter's disconnect() can call .cancel() / await on it cleanly.
    def _stub_create_task(coro):
        coro.close()  # we don't want the real polling loop running
        async def _noop():
            return None
        return adapter_mod.asyncio.ensure_future(_noop())

    monkeypatch.setattr(adapter_mod.asyncio, "create_task", _stub_create_task)

    adapter = adapter_mod.TelegramAdapter({"bot_token": "fake-token-success-path"})
    ok = await adapter.connect()

    assert ok is True
    assert adapter._lock_held is True

    # Cleanup so the lock doesn't survive the test.
    await adapter.disconnect()
    assert adapter._lock_held is False


@pytest.mark.asyncio
async def test_connect_refuses_when_lock_held_by_another_pid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reproduces the v2026.4.26 incident: lock held by another live PID
    → connect returns False with a log line naming the holding PID."""
    import json

    from opencomputer.security import scope_lock

    fake_pid = os.getpid() + 999_500
    token = "another-process-already-has-this-token"
    lock_path = scope_lock._get_scope_lock_path("telegram-bot-token", token)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "pid": fake_pid,
                "start_time": None,
                "scope": "telegram-bot-token",
                "metadata": {"adapter": "imaginary-other-bot"},
            }
        )
    )
    # Pretend the holder PID is alive so the lock is not stale.
    monkeypatch.setattr(scope_lock, "_is_pid_alive", lambda pid: pid == fake_pid)

    adapter_mod = _load_telegram_adapter()
    adapter = adapter_mod.TelegramAdapter({"bot_token": token})

    with caplog.at_level("ERROR"):
        ok = await adapter.connect()

    assert ok is False
    assert getattr(adapter, "_lock_held", False) is False
    joined = " ".join(rec.message for rec in caplog.records)
    assert str(fake_pid) in joined, (
        f"expected the holding PID {fake_pid} in the error log; got: {joined!r}"
    )
    assert "telegram bot token already in use" in joined.lower()


@pytest.mark.asyncio
async def test_disconnect_releases_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a successful connect → disconnect, the lock file is gone."""
    adapter_mod = _load_telegram_adapter()
    from opencomputer.security.scope_lock import _get_scope_lock_path

    class FakeClient:
        async def get(self, url, params=None):
            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {"ok": True, "result": {"id": 1, "username": "x"}}

                @staticmethod
                def raise_for_status():
                    return None

            return _R()

        async def aclose(self):
            return None

    monkeypatch.setattr(adapter_mod.httpx, "AsyncClient", lambda **k: FakeClient())

    def _stub_create_task(coro):
        coro.close()
        async def _noop():
            return None
        return adapter_mod.asyncio.ensure_future(_noop())

    monkeypatch.setattr(adapter_mod.asyncio, "create_task", _stub_create_task)

    token = "release-on-disconnect-token"
    adapter = adapter_mod.TelegramAdapter({"bot_token": token})
    await adapter.connect()

    lock_path = _get_scope_lock_path("telegram-bot-token", token)
    assert lock_path.exists(), "connect should have created the lock"

    await adapter.disconnect()
    assert not lock_path.exists(), "disconnect must release the lock"


@pytest.mark.asyncio
async def test_failed_getme_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If getMe fails after we took the lock, we still release it so a
    subsequent retry isn't refused by our own stale lock."""
    adapter_mod = _load_telegram_adapter()
    from opencomputer.security.scope_lock import _get_scope_lock_path

    class FakeClient:
        async def get(self, url, params=None):
            raise RuntimeError("network down")

        async def aclose(self):
            return None

    monkeypatch.setattr(adapter_mod.httpx, "AsyncClient", lambda **k: FakeClient())

    token = "getme-fails-token"
    adapter = adapter_mod.TelegramAdapter({"bot_token": token})
    ok = await adapter.connect()
    assert ok is False

    lock_path = _get_scope_lock_path("telegram-bot-token", token)
    assert not lock_path.exists(), (
        "lock must be released when connect fails post-acquire — "
        "otherwise a retry hits our own stale lock"
    )
