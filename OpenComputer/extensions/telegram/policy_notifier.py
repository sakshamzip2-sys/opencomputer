"""Phase 2 v0: Telegram notification on pending_approval policy changes.

Subscribes to ``PolicyChangeEvent`` on the typed bus. When a change lands
with ``status == 'pending_approval'``, sends a DM to the admin chat with
the engine recommendation + an /policy-approve <id> hint.

Independent of any specific Telegram client object — pass in a
``send_fn(chat_id, text)`` callable that handles the actual API call.
This keeps the subscriber testable without firing real Telegram traffic.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("memory-honcho.policy_notifier")


def register_policy_notifier(
    *,
    bus,
    admin_chat_id: str,
    send_fn: Callable[[str, str], Awaitable[Any]],
):
    """Register a subscriber that DMs the admin on every pending_approval
    policy_change event. Returns the Subscription handle so callers can
    tear down cleanly.

    ``send_fn`` is called as ``await send_fn(chat_id, text)``. Errors are
    swallowed inside the handler — a failed send must not propagate
    back to the bus or break the engine cron.
    """

    async def _handler(evt) -> None:
        try:
            if evt.status != "pending_approval":
                return
            short = evt.change_id[:8] if evt.change_id else "??"
            text = (
                "🤖 Policy engine recommends a change:\n\n"
                f"  knob:    {evt.knob_kind}\n"
                f"  target:  {evt.target_id}\n"
                f"  engine:  {evt.engine_version}\n\n"
                f"  reason:  {evt.reason}\n\n"
                f"Approve: /policy-approve {short}\n"
                "Or ignore (auto-discard in 7 days)."
            )
            await send_fn(admin_chat_id, text)
        except Exception as e:  # noqa: BLE001
            logger.warning("policy notifier send failed: %s", e)

    return bus.subscribe("policy_change", _handler)


def register_revert_notifier(
    *,
    bus,
    admin_chat_id: str,
    send_fn: Callable[[str, str], Awaitable[Any]],
):
    """Register a subscriber that DMs the admin on policy_reverted events.

    Lets the user see when a previously-applied change rolled back so
    they're not surprised by the agent's behaviour shifting.
    """

    async def _handler(evt) -> None:
        try:
            short = evt.change_id[:8] if evt.change_id else "??"
            text = (
                "↩️ Policy change rolled back:\n\n"
                f"  change:   {short}\n"
                f"  knob:     {evt.knob_kind}\n"
                f"  target:   {evt.target_id}\n\n"
                f"  reason:   {evt.reverted_reason}"
            )
            await send_fn(admin_chat_id, text)
        except Exception as e:  # noqa: BLE001
            logger.warning("revert notifier send failed: %s", e)

    return bus.subscribe("policy_reverted", _handler)
