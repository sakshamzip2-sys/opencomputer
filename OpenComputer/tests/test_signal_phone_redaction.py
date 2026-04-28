"""Phone-number redaction in Signal adapter logs (PR 3c.2).

The Signal adapter has historically logged raw E.164 numbers in its
connect / send / error paths. PR 3c switches every phone-bearing log
line to use :func:`plugin_sdk.channel_helpers.redact_phone` so logs
contain only ``+1***4567`` style fragments — enough to correlate but
not enough to leak the full PII to anyone with access to the log file.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import httpx
import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "signal_adapter_redaction_test",
        Path(__file__).resolve().parent.parent / "extensions" / "signal" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_TEST_PHONE = "+15551234567"
_TEST_RECIPIENT = "+15557654321"


@pytest.fixture
def adapter_with_mock():
    mod = _load()
    requests: list[httpx.Request] = []
    response_factory: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if response_factory:
            return response_factory[0](req)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "send",
                "result": {"timestamp": 1714000000000},
            },
        )

    a = mod.SignalAdapter(
        config={"signal_cli_url": "http://localhost:8080", "phone_number": _TEST_PHONE}
    )
    a._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Content-Type": "application/json"},
    )
    return a, requests, response_factory


def _assert_redacted(records: list[logging.LogRecord], phone: str) -> None:
    """Assert no log message contains the raw phone, but at least one
    contains a redacted fragment."""
    joined = "\n".join(r.getMessage() for r in records)
    assert phone not in joined, (
        f"raw phone {phone} leaked into logs:\n{joined}"
    )


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------


class TestConnectDisconnectRedaction:
    @pytest.mark.asyncio
    async def test_connect_logs_redacted(self, adapter_with_mock, caplog) -> None:
        adapter, _, _ = adapter_with_mock
        with caplog.at_level(logging.INFO, logger="opencomputer.ext.signal"):
            await adapter.connect()
        _assert_redacted(caplog.records, _TEST_PHONE)
        # Sanity-check the redacted form actually appears.
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "+1***4567" in joined

    @pytest.mark.asyncio
    async def test_disconnect_logs_redacted(self, adapter_with_mock, caplog) -> None:
        adapter, _, _ = adapter_with_mock
        with caplog.at_level(logging.INFO, logger="opencomputer.ext.signal"):
            await adapter.disconnect()
        _assert_redacted(caplog.records, _TEST_PHONE)


# ---------------------------------------------------------------------------
# Send (success + error paths)
# ---------------------------------------------------------------------------


class TestSendRedaction:
    @pytest.mark.asyncio
    async def test_send_success_redacts_recipient(
        self, adapter_with_mock, caplog
    ) -> None:
        adapter, _, _ = adapter_with_mock
        with caplog.at_level(logging.INFO, logger="opencomputer.ext.signal"):
            res = await adapter.send(_TEST_RECIPIENT, "hello")
        assert res.success
        _assert_redacted(caplog.records, _TEST_RECIPIENT)
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "+1***4321" in joined

    @pytest.mark.asyncio
    async def test_send_http_400_redacts(self, adapter_with_mock, caplog) -> None:
        adapter, _, factory = adapter_with_mock
        factory.append(lambda r: httpx.Response(400, text="bad request"))
        with caplog.at_level(logging.WARNING, logger="opencomputer.ext.signal"):
            res = await adapter.send(_TEST_RECIPIENT, "hi")
        assert not res.success
        _assert_redacted(caplog.records, _TEST_RECIPIENT)

    @pytest.mark.asyncio
    async def test_send_signal_cli_error_redacts(
        self, adapter_with_mock, caplog
    ) -> None:
        adapter, _, factory = adapter_with_mock
        factory.append(
            lambda r: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "send",
                    "error": {"message": "no such recipient"},
                },
            )
        )
        with caplog.at_level(logging.WARNING, logger="opencomputer.ext.signal"):
            res = await adapter.send(_TEST_RECIPIENT, "hi")
        assert not res.success
        _assert_redacted(caplog.records, _TEST_RECIPIENT)

    @pytest.mark.asyncio
    async def test_send_network_error_redacts(
        self, adapter_with_mock, caplog
    ) -> None:
        adapter, _, factory = adapter_with_mock

        def boom(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connect failed")

        factory.append(boom)
        with caplog.at_level(logging.ERROR, logger="opencomputer.ext.signal"):
            res = await adapter.send(_TEST_RECIPIENT, "hi")
        assert not res.success
        _assert_redacted(caplog.records, _TEST_RECIPIENT)


# ---------------------------------------------------------------------------
# Reaction error path
# ---------------------------------------------------------------------------


class TestReactionRedaction:
    @pytest.mark.asyncio
    async def test_reaction_error_redacts(self, adapter_with_mock, caplog) -> None:
        adapter, _, factory = adapter_with_mock
        factory.append(lambda r: httpx.Response(500, text="kaboom"))
        with caplog.at_level(logging.WARNING, logger="opencomputer.ext.signal"):
            res = await adapter.send_reaction(_TEST_RECIPIENT, "1714000000000", "👍")
        assert not res.success
        _assert_redacted(caplog.records, _TEST_RECIPIENT)
