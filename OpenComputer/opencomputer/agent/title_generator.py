"""TS-T6 — Auto-generate short session titles from the first exchange.

Ported from ``hermes-agent-2026.4.23/agent/title_generator.py``. Runs
asynchronously (daemon thread) after the first response is delivered so
it never adds latency to the user-facing reply.

Adaptation from Hermes:

- Hermes calls ``agent.auxiliary_client.call_llm`` which returns an
  OpenAI-style response (``.choices[0].message.content``).
- OC has no monolithic ``call_llm``; the cheap-LLM path is via the
  user's configured provider plugin (same pattern as
  :mod:`opencomputer.agent.recall_synthesizer`). We expose a
  module-level ``call_llm`` shim that dispatches to that provider and
  re-wraps the response in the OpenAI shape, so the rest of the
  algorithm (title cleaning, length cap, etc.) is byte-for-byte
  identical to Hermes.
- ``call_llm`` is module-level so tests can patch
  ``opencomputer.agent.title_generator.call_llm`` directly.
"""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger("opencomputer.title_generator")

#: Cheap model used for title generation. Mirrors
#: :mod:`opencomputer.agent.recall_synthesizer` — same Haiku tier so
#: titling stays fast + cheap regardless of the user's primary model.
_TITLE_MODEL = "claude-haiku-4-5"

#: Hard cap on title-generation output. Titles are 3–7 words; 50 tokens
#: is generous headroom for any tokenizer.
_TITLE_MAX_TOKENS = 50

_TITLE_PROMPT = (
    "Generate a short, descriptive title (3-7 words) for a conversation that starts with the "
    "following exchange. The title should capture the main topic or intent. "
    "Return ONLY the title text, nothing else. No quotes, no punctuation at the end, no prefixes."
)


def _resolve_cheap_provider() -> Any:
    """Resolve the user's configured provider plugin for title generation.

    Mirrors :func:`opencomputer.agent.recall_synthesizer._resolve_cheap_provider`
    — we deliberately reuse the user's provider so the cheap-LLM call
    inherits their auth + base URL config (Anthropic native, Claude Router
    proxy, OpenAI-compatible endpoint, etc.) without new setup.
    """
    from opencomputer.agent.config import default_config
    from opencomputer.plugins.registry import registry as plugin_registry

    cfg = default_config()
    provider_cls = plugin_registry.providers.get(cfg.model.provider)
    if provider_cls is None:
        raise RuntimeError(
            f"provider {cfg.model.provider!r} not registered; cannot title"
        )
    return provider_cls() if isinstance(provider_cls, type) else provider_cls


def call_llm(
    *,
    messages: list[dict[str, str]],
    max_tokens: int = _TITLE_MAX_TOKENS,
    temperature: float = 0.3,
    timeout: float = 30.0,
    model: str = _TITLE_MODEL,
) -> Any:
    """Cheap-LLM call that returns an OpenAI-shaped response.

    The return value has ``response.choices[0].message.content`` so the
    Hermes-borrowed parsing in :func:`generate_title` works unchanged.
    Tests patch this function to return a ``MagicMock`` shaped the same
    way. The ``timeout`` arg is accepted for API parity with Hermes's
    ``call_llm``; OC providers don't yet take a timeout kwarg, so we
    drop it on the floor (the per-provider HTTP client carries its own
    timeout).
    """
    del timeout  # accepted for parity; OC providers don't yet honour it

    import asyncio

    from plugin_sdk.core import Message

    provider = _resolve_cheap_provider()
    sdk_messages = [Message(role=m["role"], content=m["content"]) for m in messages]

    # Most call sites are sync (the agent loop spawns this in a daemon
    # thread). Provider.complete is async, so we drive it with
    # asyncio.run — the same pattern used by ``recall_synthesizer``.
    response = asyncio.run(
        provider.complete(
            messages=sdk_messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    )
    text = response.message.content if response and response.message else ""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def generate_title(
    user_message: str, assistant_response: str, timeout: float = 30.0
) -> str | None:
    """Generate a session title from the first exchange.

    Returns the title string or ``None`` on failure.
    """
    # Truncate long messages to keep the request small.
    user_snippet = user_message[:500] if user_message else ""
    assistant_snippet = assistant_response[:500] if assistant_response else ""

    messages = [
        {"role": "system", "content": _TITLE_PROMPT},
        {
            "role": "user",
            "content": f"User: {user_snippet}\n\nAssistant: {assistant_snippet}",
        },
    ]

    try:
        response = call_llm(
            messages=messages,
            max_tokens=_TITLE_MAX_TOKENS,
            temperature=0.3,
            timeout=timeout,
        )
        title = (response.choices[0].message.content or "").strip()
        # Clean up: remove quotes, trailing punctuation, prefixes like "Title: ".
        title = title.strip('"\'')
        if title.lower().startswith("title:"):
            title = title[6:].strip()
        # Enforce reasonable length.
        if len(title) > 80:
            title = title[:77] + "..."
        return title if title else None
    except Exception as e:  # noqa: BLE001 — caller swallows; never break a turn
        logger.debug("Title generation failed: %s", e)
        return None


def auto_title_session(
    session_db: Any,
    session_id: str,
    user_message: str,
    assistant_response: str,
) -> None:
    """Generate and set a session title if one doesn't already exist.

    Called in a background thread after the first exchange completes.
    Silently skips if:

    - ``session_db`` or ``session_id`` is missing,
    - the session already has a title (user-set or previously
      auto-generated), or
    - title generation fails.
    """
    if not session_db or not session_id:
        return

    # Check if title already exists (user may have set one via a slash
    # command before the first response landed).
    try:
        existing = session_db.get_session_title(session_id)
        if existing:
            return
    except Exception:  # noqa: BLE001
        return

    title = generate_title(user_message, assistant_response)
    if not title:
        return

    try:
        session_db.set_session_title(session_id, title)
        logger.debug("Auto-generated session title: %s", title)
    except Exception as e:  # noqa: BLE001
        logger.debug("Failed to set auto-generated title: %s", e)


def maybe_auto_title(
    session_db: Any,
    session_id: str,
    user_message: str,
    assistant_response: str,
    conversation_history: list,
) -> None:
    """Fire-and-forget title generation after the first exchange.

    Only generates a title when:

    - this appears to be the first user→assistant exchange (≤2 user
      messages in the history, counting the one that just landed), and
    - all required inputs are present.

    Runs the slow LLM call in a daemon thread so the caller's hot path
    is unaffected.
    """
    if (
        not session_db
        or not session_id
        or not user_message
        or not assistant_response
    ):
        return

    # Count user messages in history to detect the first exchange.
    # ``conversation_history`` includes the exchange that just happened,
    # so for a first exchange we expect exactly 1 user message
    # (or 2 counting an early follow-up). Be generous: title only on
    # the first 2 exchanges.
    user_msg_count = sum(
        1 for m in (conversation_history or []) if _role_of(m) == "user"
    )
    if user_msg_count > 2:
        return

    thread = threading.Thread(
        target=auto_title_session,
        args=(session_db, session_id, user_message, assistant_response),
        daemon=True,
        name="auto-title",
    )
    thread.start()


def _role_of(msg: Any) -> str | None:
    """Return ``msg.role`` whether ``msg`` is a dict or a dataclass-like Message."""
    if isinstance(msg, dict):
        return msg.get("role")
    return getattr(msg, "role", None)


__all__ = [
    "auto_title_session",
    "call_llm",
    "generate_title",
    "maybe_auto_title",
]
