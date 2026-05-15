"""Tests for §6 closure — per-channel ``auto_swap_enabled`` opt-in.

Before this fix, ``BaseChannelAdapter.auto_swap_enabled`` defaulted
to ``False`` and no concrete adapter subclass overrode it. The
gateway's ``_channel_auto_swap_enabled`` flag in runtime.custom was
therefore always False for gateway sessions, killing auto-swap on
Telegram / Discord / Slack / Matrix / Email / iMessage / IRC /
Feishu / DingTalk.

The fix exposes the attribute via channel config: a profile's YAML
can now set ``channels.<platform>.auto_swap_enabled: true`` and the
adapter respects it.

These tests assert: only the explicit bool ``True`` enables (string
"true", "yes", 1, etc. are rejected to prevent accidental enablement
from misconfigured YAML — a per-channel handoff is contract-affecting).
"""
from __future__ import annotations

from typing import Any

import pytest

from plugin_sdk.channel_contract import BaseChannelAdapter, Platform
from plugin_sdk.core import SendResult


class _TestAdapter(BaseChannelAdapter):
    """Minimal subclass for testing BaseChannelAdapter.__init__."""

    platform = Platform.TELEGRAM  # arbitrary; we never connect

    async def connect(self) -> bool:  # pragma: no cover - not exercised
        return True

    async def disconnect(self) -> None:  # pragma: no cover
        return None

    async def send(
        self, chat_id: str, text: str, **kwargs: Any
    ) -> SendResult:  # pragma: no cover
        return SendResult(success=True)


def test_default_auto_swap_is_false() -> None:
    """No config → class default ``False`` preserved."""
    a = _TestAdapter({})
    assert a.auto_swap_enabled is False


def test_explicit_true_enables() -> None:
    a = _TestAdapter({"auto_swap_enabled": True})
    assert a.auto_swap_enabled is True


def test_explicit_false_disables() -> None:
    """Even if a subclass set class default True, explicit False wins."""
    class _T2(_TestAdapter):
        auto_swap_enabled = True

    a = _T2({"auto_swap_enabled": False})
    assert a.auto_swap_enabled is False


def test_string_true_is_rejected_as_footgun() -> None:
    """YAML often parses unquoted ``true`` to True but quoted ``"true"`` to str.
    We accept only the bool — string "true" is silently ignored to avoid
    surprises.
    """
    a = _TestAdapter({"auto_swap_enabled": "true"})
    assert a.auto_swap_enabled is False  # class default unchanged


def test_int_one_is_rejected() -> None:
    a = _TestAdapter({"auto_swap_enabled": 1})
    assert a.auto_swap_enabled is False


def test_none_uses_default() -> None:
    a = _TestAdapter({"auto_swap_enabled": None})
    assert a.auto_swap_enabled is False


def test_two_instances_independent() -> None:
    """Per-instance override does not leak into siblings."""
    a = _TestAdapter({"auto_swap_enabled": True})
    b = _TestAdapter({})
    assert a.auto_swap_enabled is True
    assert b.auto_swap_enabled is False


def test_gateway_dispatch_picks_up_optin_attribute() -> None:
    """The gateway reads adapter.auto_swap_enabled at runtime context
    creation. Verify that the attribute set in __init__ flows through.
    """
    # The dispatch code uses ``getattr(adapter, "auto_swap_enabled", False)``
    # so any subclass that sets self.auto_swap_enabled=True must be visible.
    a = _TestAdapter({"auto_swap_enabled": True})
    assert getattr(a, "auto_swap_enabled", False) is True
    b = _TestAdapter({})
    assert getattr(b, "auto_swap_enabled", False) is False
