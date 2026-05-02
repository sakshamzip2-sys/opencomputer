"""``/btw <question>`` — ephemeral side-question with session context.

Tier 2.A.2 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

The "by the way" pattern: ask a quick question that:
  - **uses** the current session's context (so the model knows what
    "this", "that", "earlier" refer to)
  - does **NOT** run any tools (pure-text response)
  - is **NOT persisted** to the session DB (the user's parent
    conversation history stays clean)
  - does **NOT** trigger compaction or memory writes

Architecture: fire one Anthropic Messages API call with the parent
session's recent messages as ``messages``, append the /btw text as a
fresh user turn, omit ``tools``, take the assistant text response,
return it to the user. No SessionDB writes, no memory mutation, no
hooks fired.

Reads from ``runtime.custom``:
  - session_id    (set by loop.py before slash dispatch)
  - session_db    (set by loop.py before slash dispatch)

API key from ``ANTHROPIC_API_KEY`` env var (or constructor injection
for tests). Anthropic-only for MVP — same convention as VisionAnalyzeTool.

Examples:
  /btw what was the regex on line 12?
  /btw quick — what's the difference between mTLS and standard TLS?
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TIMEOUT_S = 60.0
MAX_CONTEXT_MESSAGES = 30  # bound how much parent history we send


def _flatten_content(content) -> str:
    """Best-effort flatten of multimodal content blocks for context.

    /btw is a fast pure-text side question. We strip image / tool_use /
    tool_result blocks and keep only text — sending a vision model an
    image just for a 'btw' is wasteful, and tool blocks would confuse
    a tools-disabled side call.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            # Skip image / tool_use / tool_result entirely.
        return "\n".join(p for p in parts if p)
    return str(content)


def _build_messages_payload(parent_messages, question: str) -> list[dict]:
    """Translate session DB Message objects into Anthropic API shape."""
    out: list[dict] = []
    # Take only the last MAX_CONTEXT_MESSAGES (more is wasteful for a
    # side-question and slows down the call).
    recent = list(parent_messages)[-MAX_CONTEXT_MESSAGES:]
    for msg in recent:
        role = getattr(msg, "role", "user")
        if role not in ("user", "assistant"):
            # Anthropic only accepts user/assistant in `messages`.
            continue
        text = _flatten_content(getattr(msg, "content", "")).strip()
        if not text:
            continue
        out.append({"role": role, "content": text})
    # Append the /btw question as a fresh user turn
    out.append({"role": "user", "content": question})
    return out


class BtwCommand(SlashCommand):
    name = "btw"
    description = (
        "Ask an ephemeral side-question using session context — "
        "no tools, not persisted"
    )

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        question = (args or "").strip()
        if not question:
            return SlashCommandResult(
                output=(
                    "Usage: /btw <question>\n"
                    "Asks a quick side-question using current session context. "
                    "No tools run, not persisted to history."
                ),
                handled=True,
            )

        sid = runtime.custom.get("session_id")
        db = runtime.custom.get("session_db")

        # Read parent context if available; gracefully proceed without
        # if we're outside an agent loop turn.
        parent_messages: list = []
        if sid and db is not None:
            try:
                parent_messages = db.get_messages(sid)
            except Exception:  # noqa: BLE001
                parent_messages = []

        # Route through the user's configured provider (anthropic, openai,
        # groq, ollama, etc.) via the auxiliary-LLM shim so /btw works on
        # any provider — not just Anthropic. The shim inherits the same
        # auth + base URL config as chat (claude-router bearer mode etc).
        from opencomputer.agent.aux_llm import complete_text

        try:
            text = await complete_text(
                messages=_build_messages_payload(parent_messages, question),
                max_tokens=1024,
                model=self._model,
                # Note: no `tools` — that's the whole point of /btw.
            )
        except Exception as e:  # noqa: BLE001 — surface error as text
            return SlashCommandResult(
                output=f"/btw API call failed: {type(e).__name__}: {e}",
                handled=True,
            )

        text = (text or "").strip()
        if not text:
            return SlashCommandResult(
                output="/btw returned no text content",
                handled=True,
            )

        return SlashCommandResult(
            output=f"💭 (ephemeral, not saved)\n\n{text}",
            handled=True,
        )


__all__ = ["BtwCommand"]
