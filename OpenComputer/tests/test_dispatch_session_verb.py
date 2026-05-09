"""Hermes parity: gateway dispatch routes verb='session' to gate.resolve_pending."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from opencomputer.gateway.dispatch import Dispatch


def _new_dispatch_with_gate(gate):
    """Minimal Dispatch fixture for unit-testing _handle_approval_click."""
    d = Dispatch.__new__(Dispatch)
    fake_loop = MagicMock()
    fake_loop._consent_gate = gate
    fake_router = MagicMock()
    fake_router._loops = {"default": fake_loop}
    d._router = fake_router
    d._approval_tokens = {"tok1": ("sess-1", "execute_code.run")}
    d._session_profiles = {"sess-1": "default"}
    return d


@pytest.mark.asyncio
async def test_session_verb_routes_to_resolve_pending_with_session_scoped_true():
    fake_gate = MagicMock()
    fake_gate.resolve_pending = MagicMock(return_value=True)
    d = _new_dispatch_with_gate(fake_gate)

    await d._handle_approval_click(verb="session", token="tok1")

    fake_gate.resolve_pending.assert_called_once_with(
        session_id="sess-1",
        capability_id="execute_code.run",
        decision=True,
        persist=False,
        session_scoped=True,
    )


@pytest.mark.asyncio
async def test_once_verb_keeps_session_scoped_false():
    fake_gate = MagicMock()
    fake_gate.resolve_pending = MagicMock(return_value=True)
    d = _new_dispatch_with_gate(fake_gate)

    await d._handle_approval_click(verb="once", token="tok1")

    fake_gate.resolve_pending.assert_called_once_with(
        session_id="sess-1",
        capability_id="execute_code.run",
        decision=True,
        persist=False,
        session_scoped=False,
    )


@pytest.mark.asyncio
async def test_always_verb_keeps_session_scoped_false():
    fake_gate = MagicMock()
    fake_gate.resolve_pending = MagicMock(return_value=True)
    d = _new_dispatch_with_gate(fake_gate)

    await d._handle_approval_click(verb="always", token="tok1")

    fake_gate.resolve_pending.assert_called_once_with(
        session_id="sess-1",
        capability_id="execute_code.run",
        decision=True,
        persist=True,
        session_scoped=False,
    )


@pytest.mark.asyncio
async def test_unknown_verb_logs_and_returns(caplog):
    fake_gate = MagicMock()
    fake_gate.resolve_pending = MagicMock(return_value=True)
    d = _new_dispatch_with_gate(fake_gate)

    with caplog.at_level(logging.WARNING, logger="opencomputer.gateway.dispatch"):
        await d._handle_approval_click(verb="grilled-cheese", token="tok1")

    assert any("unknown verb" in r.message for r in caplog.records)
    fake_gate.resolve_pending.assert_not_called()
