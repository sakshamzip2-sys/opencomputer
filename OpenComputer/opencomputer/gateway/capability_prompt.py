"""B3 — render a channel adapter's capabilities into a prompt block.

Gateway-vs-CLI parity. ``ChannelCapabilities`` already records what each
adapter can do (edit messages, send photos, react, …) and
``oc adapter capabilities`` lists it — but the agent's system prompt
never saw it, so the model produced format-agnostic output: Markdown on
SMS, no images on platforms that support them, no emoji reactions when
reactions are available.

:func:`describe_channel_capabilities` turns an adapter's flags +
``max_message_length`` into a short natural-language block. The gateway
dispatcher threads it onto ``RuntimeContext.custom["channel_capabilities"]``;
the agent loop appends it to the per-turn system prompt (mirroring the
existing ``channel_prompt`` injection).
"""

from __future__ import annotations

from typing import Any

from plugin_sdk.channel_contract import ChannelCapabilities

#: Capability flag → the action phrase shown to the model. Ordered so the
#: rendered sentence reads naturally.
_CAPABILITY_PHRASES: tuple[tuple[ChannelCapabilities, str], ...] = (
    (ChannelCapabilities.EDIT_MESSAGE, "edit messages you have already sent"),
    (ChannelCapabilities.PHOTO_OUT, "send images"),
    (ChannelCapabilities.DOCUMENT_OUT, "send file attachments"),
    (ChannelCapabilities.VOICE_OUT, "send voice messages"),
    (ChannelCapabilities.REACTIONS, "react to messages with emoji"),
    (ChannelCapabilities.THREADS, "reply inside threads"),
)


def describe_channel_capabilities(adapter: Any) -> str:
    """Return a prompt block describing ``adapter``'s channel, or ``""``.

    Empty string when there is no adapter or its ``capabilities`` is not
    a real :class:`ChannelCapabilities` value (e.g. a test mock) — the
    caller then injects nothing and the prompt is unchanged.
    """
    if adapter is None:
        return ""
    caps = getattr(adapter, "capabilities", None)
    if not isinstance(caps, ChannelCapabilities):
        return ""

    platform = getattr(adapter, "platform", None)
    platform_name = getattr(platform, "value", None) or str(
        platform or "this",
    )

    lines = [f"You are replying on the **{platform_name}** channel."]

    supported = [phrase for flag, phrase in _CAPABILITY_PHRASES if caps & flag]
    if supported:
        lines.append(
            "This channel can " + _join_phrases(supported)
            + " — use those affordances when they serve the reply."
        )

    cap_max = getattr(adapter, "max_message_length", 0) or 0
    if cap_max:
        lines.append(
            f"Replies longer than {cap_max} characters are split into "
            "multiple messages; prefer a concise, well-structured answer."
        )

    return "\n".join(lines)


def _join_phrases(phrases: list[str]) -> str:
    """Join with commas and a trailing ``and`` — ``a, b and c``."""
    if len(phrases) == 1:
        return phrases[0]
    return ", ".join(phrases[:-1]) + " and " + phrases[-1]


__all__ = ["describe_channel_capabilities"]
