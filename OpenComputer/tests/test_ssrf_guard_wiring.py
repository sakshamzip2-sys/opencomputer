"""PR #221 O3 — verify SSRF redirect guard is wired into adapter clients.

Adapters that fetch user-supplied URLs (attachment downloads, image
caches) should attach ``plugin_sdk.network_utils.ssrf_redirect_guard``
to their ``httpx.AsyncClient.event_hooks["response"]`` so any 3xx whose
``Location`` points at a loopback/private/link-local host is rejected
before bytes flow.

The guard itself is tested in PR 1; here we just confirm the wiring
exists where it should — and document the adapters where it's
intentionally NOT wired (only POST to fixed vendor-controlled URLs).
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest

from plugin_sdk.network_utils import ssrf_redirect_guard


def _load_adapter_module(plugin_dir: str):
    spec = importlib.util.spec_from_file_location(
        f"_adapter_o3_{plugin_dir.replace('-', '_')}",
        Path(__file__).resolve().parent.parent / "extensions" / plugin_dir / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_telegram_connect_wires_ssrf_redirect_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: monkeypatch ``httpx.AsyncClient`` to capture the kwargs
    the adapter constructs it with, then run ``connect()`` and assert the
    SSRF guard is in ``event_hooks['response']``."""
    adapter_mod = _load_adapter_module("telegram")

    captured_kwargs: dict = {}

    real_async_client = adapter_mod.httpx.AsyncClient

    class CapturingClient(real_async_client):
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            # Replace the transport so getMe doesn't hit the real network.
            kwargs["transport"] = httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={"ok": True, "result": {"id": 42, "username": "tb"}},
                )
            )
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(adapter_mod.httpx, "AsyncClient", CapturingClient)

    # Stub out the polling task so connect doesn't spawn a real loop.
    real_create_task = adapter_mod.asyncio.create_task

    def _stub_create_task(coro):
        coro.close()

        async def _noop():
            return None

        return real_create_task(_noop())

    monkeypatch.setattr(adapter_mod.asyncio, "create_task", _stub_create_task)

    adapter = adapter_mod.TelegramAdapter({"bot_token": "ssrf-wire-test-token"})
    try:
        ok = await adapter.connect()
        assert ok is True

        # The kwargs captured at AsyncClient construction time should
        # include event_hooks with the SSRF guard.
        event_hooks = captured_kwargs.get("event_hooks", {})
        response_hooks = event_hooks.get("response", [])
        assert ssrf_redirect_guard in response_hooks, (
            f"expected ssrf_redirect_guard in telegram client's response "
            f"hooks; got event_hooks={event_hooks!r}"
        )
    finally:
        await adapter.disconnect()


def test_ssrf_guard_is_async_callable() -> None:
    """Sanity: the guard must be a coroutine function (httpx event hooks
    must be async). Catches a regression if someone replaces the export
    with a sync stub."""
    assert asyncio.iscoroutinefunction(ssrf_redirect_guard)


def test_ssrf_guard_skipped_for_fixed_url_only_adapters() -> None:
    """Document why slack / matrix / discord don't wire the guard.

    - **slack**: only POSTs to ``https://slack.com/api/...`` (a single
      fixed vendor-controlled host). No user-supplied download URL,
      no redirect-following. Guard would be a no-op.
    - **matrix**: only PUTs to ``<homeserver>/_matrix/client/v3/...``.
      Same shape as slack — fixed destination, no attachment download.
    - **discord**: uses ``discord.py``'s gateway client, not raw httpx.
      No httpx ``AsyncClient`` for us to wire ``event_hooks`` on. Any
      future raw-HTTP attachment fetch should add the guard at that
      construction site.

    This test just imports the modules to confirm they load cleanly,
    documenting the intentional skip in the adversarial review.
    """
    slack = _load_adapter_module("slack")
    matrix = _load_adapter_module("matrix")
    discord_mod = _load_adapter_module("discord")
    assert hasattr(slack, "SlackAdapter")
    assert hasattr(matrix, "MatrixAdapter")
    assert hasattr(discord_mod, "DiscordAdapter")
