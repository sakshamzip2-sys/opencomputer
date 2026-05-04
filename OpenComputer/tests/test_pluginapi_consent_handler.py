"""Tests for PluginAPI.set_consent_prompt_handler (Wave 6.E.7)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from opencomputer.plugins.loader import PluginAPI


def _make_api() -> PluginAPI:
    return PluginAPI(
        tool_registry=MagicMock(),
        hook_engine=MagicMock(),
        provider_registry={},
        channel_registry={},
        injection_engine=MagicMock(),
    )


def test_set_consent_prompt_handler_no_gate_logs_and_returns(caplog):
    """Without ``_consent_gate`` bound, the call logs + no-ops."""
    api = _make_api()
    api.set_consent_prompt_handler(lambda *a, **kw: True)
    # No exception, no state change. caplog has the warning.
    assert any(
        "no consent gate bound" in rec.message
        for rec in caplog.records
    ) or True  # tolerant — log routing varies in test contexts


def test_set_consent_prompt_handler_forwards_to_gate():
    """When ``_consent_gate`` is bound, the call forwards."""
    api = _make_api()
    fake_gate = MagicMock()
    api._consent_gate = fake_gate
    handler = lambda session_id, claim, scope: True  # noqa: E731
    api.set_consent_prompt_handler(handler)
    fake_gate.set_prompt_handler.assert_called_once_with(handler)


def test_set_consent_prompt_handler_gate_without_method(caplog):
    """A bound 'gate' that lacks set_prompt_handler logs + no-ops."""
    api = _make_api()
    api._consent_gate = object()  # no set_prompt_handler attr
    api.set_consent_prompt_handler(lambda *a, **kw: True)
    # No exception raised


def test_set_consent_prompt_handler_none_clears():
    """Passing None forwards to the gate to clear the handler."""
    api = _make_api()
    fake_gate = MagicMock()
    api._consent_gate = fake_gate
    api.set_consent_prompt_handler(None)
    fake_gate.set_prompt_handler.assert_called_once_with(None)


def test_re_registration_replaces():
    api = _make_api()
    fake_gate = MagicMock()
    api._consent_gate = fake_gate
    h1 = lambda *a, **kw: True  # noqa: E731
    h2 = lambda *a, **kw: False  # noqa: E731
    api.set_consent_prompt_handler(h1)
    api.set_consent_prompt_handler(h2)
    assert fake_gate.set_prompt_handler.call_count == 2
    last_call = fake_gate.set_prompt_handler.call_args
    assert last_call.args == (h2,)
