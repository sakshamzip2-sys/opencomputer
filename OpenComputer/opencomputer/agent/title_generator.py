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
import re
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

_TITLE_PROMPT = """You generate session titles for a personal AI agent's conversation log.

Given an excerpt of a conversation (a <user> turn and an <assistant> turn), output ONLY a 3-7 word title that names what the conversation is about. The title is a label for a list view — like a filename.

STRICT RULES:
- Output the title text alone. No quotes, no trailing period, no preamble ("Title:", "Here is", "Sure,").
- The title describes the TOPIC, not the participants. NEVER respond as the assistant. NEVER write "I appreciate", "I'm Claude", "I cannot", "Sure", "Hello", or any first-person sentence — that is conversation content, not a title.
- Do not include newlines or markdown.
- If the topic is unclear or the conversation is empty/greeting-only, output exactly: Untitled.

Examples:
<user>Can you help me debug this SQL query?</user>
<assistant>Sure, share the query.</assistant>
Output: SQL query debugging

<user>What's the weather like in Tokyo?</user>
<assistant>I cannot check live weather.</assistant>
Output: Weather inquiry Tokyo

<user>Hi</user>
<assistant>Hello! How can I help?</assistant>
Output: Untitled

<user>Walk me through how the OAuth flow works</user>
<assistant>Sure, let's start with the authorization request.</assistant>
Output: OAuth flow walkthrough"""


#: Patterns that mean the LLM ignored the prompt and responded AS the
#: assistant instead of generating a title. Cheap pre-cleanup gate so
#: the bad title never reaches the DB. Compared case-insensitively
#: against the cleaned, lowercased title.
_BAD_TITLE_PREFIXES = (
    "i appreciate",
    "i'm claude",
    "i am claude",
    "i'm sorry",
    "i cannot",
    "i can't",
    "i don't",
    "sure,",
    "sure!",
    "hello!",
    "hello,",
    "hey there",
    "thanks for",
    "let me ",
    "here is ",
    "here's ",
    "ok.",
    "ok,",
    "okay.",
    "okay,",
    "great!",
    "great,",
)


def _looks_like_response_not_title(candidate: str) -> bool:
    """True when ``candidate`` looks like the LLM continued the conversation
    instead of generating a title.

    Catches the failure mode where the titling LLM sees the excerpt and
    replies AS the assistant — producing strings like
    ``"I appreciate you testing my behavior, but I need to be direct…"``
    that are conversation content, not titles.

    Length capping is intentionally handled separately by the caller
    (titles up to 80 chars are clamped, not rejected); this function
    is the "is this a title at all?" gate.
    """
    if not candidate:
        return True
    lower = candidate.lower().lstrip()
    if any(lower.startswith(p) for p in _BAD_TITLE_PREFIXES):
        return True
    if "\n" in candidate:
        # Real titles are single-line. The auto-titler's confused
        # output usually breaks lines (numbered lists, paragraphs).
        return True
    return False


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
    # Hermes-followup 2026-05-07 — record cost into active session.
    try:
        from opencomputer.agent.usage_pricing import record_response_for_provider

        record_response_for_provider(provider=provider, model=model, response=response)
    except Exception:  # noqa: BLE001
        pass
    text = response.message.content if response and response.message else ""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def generate_title(
    user_message: str, assistant_response: str, timeout: float = 30.0
) -> str | None:
    """Generate a session title from the first exchange.

    Returns the title string or ``None`` on failure (network error,
    LLM ignored the prompt and responded as the assistant, etc.). The
    ``None`` return is by design — the caller (:func:`auto_title_session`)
    treats it as "leave title empty, the picker will fall back to the
    first user-message preview".
    """
    # Truncate long messages to keep the request small.
    user_snippet = user_message[:500] if user_message else ""
    assistant_snippet = assistant_response[:500] if assistant_response else ""

    # Wrap content in XML tags so the titling LLM treats them as DATA
    # rather than prompt continuation. The trailing "Output:" anchors
    # the response to the title slot, matching the few-shot examples
    # in :data:`_TITLE_PROMPT`.
    conversation = (
        f"<user>{user_snippet}</user>\n"
        f"<assistant>{assistant_snippet}</assistant>\n"
        f"Output:"
    )
    messages = [
        {"role": "system", "content": _TITLE_PROMPT},
        {"role": "user", "content": conversation},
    ]

    try:
        response = call_llm(
            messages=messages,
            max_tokens=_TITLE_MAX_TOKENS,
            temperature=0.3,
            timeout=timeout,
        )
        title = (response.choices[0].message.content or "").strip()
        # Clean up: remove quotes, trailing punctuation, prefixes.
        title = title.strip('"\'')
        for prefix in ("Title:", "Output:", "title:", "output:"):
            if title.startswith(prefix):
                title = title[len(prefix) :].strip()
        # Strip trailing period (real titles don't end with one).
        title = title.rstrip(".")
        # Validator: catch the "LLM responded AS the assistant" failure
        # mode and discard. Returning None tells the caller to leave
        # the title empty — the picker's first-user-message fallback
        # will still give a meaningful headline.
        if _looks_like_response_not_title(title):
            logger.debug(
                "Title generation rejected (looks like assistant response): %r",
                title[:80],
            )
            return None
        # "Untitled" is the explicit signal from the prompt that the
        # topic was unclear. Treat it as no-title so the fallback fires.
        if title.lower() == "untitled":
            return None
        # Enforce reasonable length cap (validator already caps at 60).
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


_LINEAGE_RE = re.compile(r"^(.+?)\s+#(\d+)$")


def next_title_in_lineage(db: Any, base: str) -> str:
    """Return the next title in *base*'s lineage (``base``, ``base #2``, …).

    Hermes-CLI parity (doc lines 442-447). Used by manual ``oc session
    fork --inherit-title`` and (future) compaction-fork hook. Best-effort:
    if querying fails, the base title is returned unchanged.

    Strategy: pick the highest existing ``#N`` in the family and return
    ``f"{base} #{N + 1}"``. If only the bare ``base`` exists (no
    numbered sibling), return ``f"{base} #2"``.
    """
    try:
        rows = db.find_sessions_by_title_lineage(base)
    except Exception:  # noqa: BLE001 — caller may pass any duck-typed db
        return base
    if not rows:
        return base
    highest = 1
    for r in rows:
        title = (
            r.get("title") if isinstance(r, dict) else getattr(r, "title", "")
        ) or ""
        if title == base:
            highest = max(highest, 1)
            continue
        m = _LINEAGE_RE.match(title)
        if m and m.group(1) == base:
            try:
                highest = max(highest, int(m.group(2)))
            except ValueError:
                continue
    return f"{base} #{highest + 1}"


__all__ = [
    "auto_title_session",
    "call_llm",
    "generate_title",
    "maybe_auto_title",
    "next_title_in_lineage",
]
