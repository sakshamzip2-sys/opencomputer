"""LLM-generated one-line summaries of reasoning turns.

Direct port of :mod:`opencomputer.agent.title_generator`'s pattern —
cheap Haiku model + module-level ``call_llm`` shim + daemon-thread
spawner for fire-and-forget post-turn processing. The summary appears
in the collapsed thinking-history line + at the top of
``/reasoning show <N>``'s tree.

Adaptation rationale: the title_generator pattern is proven, fully
test-mocked, and provider-agnostic. We deliberately reuse it instead
of reinventing.
"""
from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from typing import Any

from opencomputer.cli_ui.reasoning_store import ReasoningStore

logger = logging.getLogger("opencomputer.reasoning_summary")

#: Cheap model used for reasoning summary generation. Mirrors
#: :mod:`opencomputer.agent.title_generator` — same Haiku tier so
#: summarization stays fast + cheap regardless of the user's primary
#: model.
_SUMMARY_MODEL = "claude-haiku-4-5"

#: Hard cap on summary-generation output. One-liners are 5-12 words;
#: 50 tokens is generous headroom.
_SUMMARY_MAX_TOKENS = 50

_SUMMARY_PROMPT = (
    "Generate a short, descriptive one-line summary (5-12 words) of what "
    "an AI assistant just reasoned about. The summary should describe the "
    "TASK the assistant tackled or the conclusion it reached, in plain "
    "natural language — like a section heading. Return ONLY the summary "
    "text, nothing else. No quotes, no trailing punctuation, no prefixes "
    "like 'Summary:' or 'The assistant'."
)


def _resolve_cheap_provider() -> Any:
    """Resolve the user's configured provider plugin for summary
    generation. Mirrors :func:`opencomputer.agent.title_generator.
    _resolve_cheap_provider` — we deliberately reuse the user's
    provider so the cheap-LLM call inherits their auth + base URL
    config (Anthropic native, Claude Router proxy, OpenAI-compatible,
    etc.) without new setup.
    """
    from opencomputer.agent.config import default_config
    from opencomputer.plugins.registry import registry as plugin_registry

    cfg = default_config()
    provider_cls = plugin_registry.providers.get(cfg.model.provider)
    if provider_cls is None:
        raise RuntimeError(
            f"provider {cfg.model.provider!r} not registered; cannot summarize"
        )
    return provider_cls() if isinstance(provider_cls, type) else provider_cls


def call_llm(
    *,
    messages: list[dict[str, str]],
    max_tokens: int = _SUMMARY_MAX_TOKENS,
    temperature: float = 0.3,
    timeout: float = 15.0,
    model: str = _SUMMARY_MODEL,
) -> Any:
    """Cheap-LLM call returning OpenAI-shaped response. Tests patch
    this function to return a mock with the same shape."""
    del timeout  # accepted for parity; OC providers don't yet honour it

    import asyncio

    from plugin_sdk.core import Message

    provider = _resolve_cheap_provider()
    sdk_messages = [Message(role=m["role"], content=m["content"]) for m in messages]

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


def _clean(raw: str) -> str:
    """Strip surrounding quotes + trailing punctuation; cap length."""
    s = (raw or "").strip().strip('"').strip("'").rstrip(".!?:; ")
    return s[:120]


def generate_summary(
    thinking_text: str, *, timeout: float = 15.0
) -> str | None:
    """Generate a one-line summary of the given thinking text.

    Returns the cleaned string (max 120 chars) or ``None`` on empty
    input or LLM failure. Errors are logged at debug — never raised —
    so the daemon-thread caller doesn't need to wrap.
    """
    snippet = (thinking_text or "")[:1500].strip()
    if not snippet:
        return None
    try:
        resp = call_llm(
            messages=[
                {"role": "user", "content": f"{_SUMMARY_PROMPT}\n\n{snippet}"},
            ],
            max_tokens=_SUMMARY_MAX_TOKENS,
            temperature=0.3,
            timeout=timeout,
        )
        raw = resp.choices[0].message.content if resp and resp.choices else ""
        cleaned = _clean(raw)
        return cleaned or None
    except Exception:  # noqa: BLE001 — never let summary failure crash the loop
        logger.debug("reasoning summary failed", exc_info=True)
        return None


def _summarize_and_store(
    store: ReasoningStore, turn_id: int, thinking_text: str
) -> None:
    summary = generate_summary(thinking_text)
    if summary:
        store.update_summary(turn_id=turn_id, summary=summary)


def maybe_summarize_turn(
    *, store: ReasoningStore, turn_id: int, thinking_text: str
) -> threading.Thread | None:
    """Spawn a daemon thread that generates the summary and writes it
    back to the store. Returns the thread (so callers may join it with
    a short timeout if they want the summary in the collapsed line) or
    ``None`` if there's nothing worth summarizing.

    The call is fire-and-forget by design — the daemon thread never
    blocks process exit, errors are swallowed, and unknown ``turn_id``
    is a no-op via :meth:`ReasoningStore.update_summary`.
    """
    if not (thinking_text or "").strip():
        return None
    thread = threading.Thread(
        target=_summarize_and_store,
        args=(store, turn_id, thinking_text),
        daemon=True,
        name=f"reason-summary-turn-{turn_id}",
    )
    thread.start()
    return thread


__all__ = [
    "call_llm",
    "generate_summary",
    "maybe_summarize_turn",
]
