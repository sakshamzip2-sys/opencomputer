"""Compose context (system prompt, tool list, prior turns) for a realtime
voice session.

Pulled out so ``cli_voice.py`` stays focused on transport wiring. Each
helper returns a self-contained piece the CLI can plug into the bridge:

* :func:`registered_tools_for_realtime` — converts OC's ``ToolRegistry``
  into the ``RealtimeVoiceTool[]`` shape both bridges accept. Without this
  the model never sees that any tools exist and acts like a dumb chatbot.

* :func:`compose_system_prompt` — builds a voice-tailored system prompt:
  identity preamble (so the model knows it's "OpenComputer"), tool
  guidance ("you have these tools available, prefer brevity"), optional
  resumed-session summary, and finally the user's ``--instructions``
  (which take precedence over everything else).

* :func:`load_recent_messages` — pulls the last N messages from a
  named session for the resumed-session preamble.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from plugin_sdk.realtime_voice import RealtimeVoiceTool

if TYPE_CHECKING:
    from opencomputer.tools.registry import ToolRegistry

DEFAULT_RESUME_MESSAGES = 8


def _sanitize_schema_for_realtime(node: object) -> object:
    """Recursively strip JSON-Schema features the realtime providers reject.

    Concrete cases caught in the wild (Gemini Live's schema parser is
    strictest):

    * ``type: integer`` / ``type: number`` / ``type: boolean`` with an
      ``enum`` — Gemini's parser tries to read enum values as TYPE_STRING
      and rejects with ``invalid frame payload data``. We drop the
      ``enum`` from non-string-typed properties; the ``type`` and
      ``description`` survive so the model still understands the shape.
    * ``additionalProperties`` and unsupported JSON-Schema keywords
      pass through harmlessly today, but easy to extend here if a future
      provider chokes on them.

    Returns a new dict (or list, or scalar) — does not mutate input.
    Works on arbitrarily nested schemas (recurses into ``properties``,
    ``items``, ``oneOf`` etc.).
    """
    if isinstance(node, dict):
        out: dict = {}
        node_type = node.get("type")
        for key, value in node.items():
            if key == "enum" and node_type and node_type != "string":
                # Drop the enum; integer / number / boolean enums break
                # Gemini's parser. Keep the type so the model still
                # knows the shape.
                continue
            out[key] = _sanitize_schema_for_realtime(value)
        return out
    if isinstance(node, list):
        return [_sanitize_schema_for_realtime(v) for v in node]
    return node


def registered_tools_for_realtime(
    tool_registry: ToolRegistry,
) -> tuple[RealtimeVoiceTool, ...]:
    """Snapshot OC's tool registry as a ``RealtimeVoiceTool`` tuple.

    Each ``BaseTool`` exposes a ``schema`` (``ToolSchema(name, description,
    parameters)``); we map name+description+parameters into the realtime
    shape. Parameters get sanitized first via
    :func:`_sanitize_schema_for_realtime` to strip JSON-Schema features
    the realtime providers reject (e.g. integer enums on Gemini).

    The returned tuple is a snapshot — if tools register after this call,
    the realtime session won't see them. Voice sessions are short-lived
    so that's fine.
    """
    return tuple(
        RealtimeVoiceTool(
            type="function",
            name=schema.name,
            description=schema.description,
            parameters=_sanitize_schema_for_realtime(schema.parameters),
        )
        for schema in tool_registry.schemas()
    )


def _identity_preamble(tool_count: int) -> str:
    """Short voice-style preamble so the model knows it's OC, not raw Gemini."""
    return (
        "You are OpenComputer — a personal AI assistant the user is talking "
        f"to via voice. You have {tool_count} tools available (Bash, file edit, "
        "screenshot, web fetch, browser control, and more). Use them when the "
        "user asks for actions; speak naturally and concisely otherwise. "
        "Voice responses must be SHORT — aim for one or two sentences unless "
        "the user explicitly asks for detail. Never read code or long output "
        "verbatim; summarize."
    )


def compose_system_prompt(
    *,
    tool_count: int,
    user_instructions: str | None = None,
    resumed_session_summary: str | None = None,
    profile_persona: str | None = None,
) -> str | None:
    """Combine identity preamble, persona, resumed-session summary, and the
    user's ``--instructions`` into one system prompt string.

    Order: identity → persona → resumed summary → user instructions. The
    user's explicit ``--instructions`` always come LAST so they override
    or extend earlier sections rather than getting buried.

    Returns ``None`` when every input is empty (so the bridge can omit
    the ``systemInstruction`` field entirely on the wire). Otherwise
    returns a single newline-separated string.
    """
    parts: list[str] = []
    parts.append(_identity_preamble(tool_count))
    if profile_persona:
        parts.append(profile_persona.strip())
    if resumed_session_summary:
        parts.append(resumed_session_summary.strip())
    if user_instructions:
        parts.append(user_instructions.strip())
    if not parts:
        return None
    return "\n\n".join(parts)


def load_recent_messages(
    session_id: str,
    *,
    limit: int = DEFAULT_RESUME_MESSAGES,
) -> str:
    """Format the last ``limit`` messages of ``session_id`` as a brief
    "previous context" preamble for ``compose_system_prompt``.

    Returns ``""`` if the session is unknown / empty / DB unavailable —
    voice sessions degrade gracefully when context can't be loaded.
    """
    try:
        from opencomputer.agent.config import _home  # noqa: PLC0415
        from opencomputer.agent.state import SessionDB  # noqa: PLC0415
    except ImportError:
        return ""

    db_path = _home() / "sessions.db"
    if not db_path.exists():
        return ""
    try:
        db = SessionDB(db_path)
        messages = db.get_messages(session_id) or []
    except Exception:  # noqa: BLE001 — degrade quietly
        return ""

    if not messages:
        return ""

    tail = messages[-limit:]
    lines = ["Recent context from a prior chat session (continue the thread):"]
    for m in tail:
        role = getattr(m, "role", "") or ""
        content = getattr(m, "content", "") or ""
        if not content or role not in ("user", "assistant"):
            continue
        if not isinstance(content, str):
            continue
        # Truncate over-long lines so the system prompt stays reasonable.
        snippet = content.strip()
        if len(snippet) > 280:
            snippet = snippet[:280] + "…"
        speaker = "User" if role == "user" else "Assistant"
        lines.append(f"  {speaker}: {snippet}")
    if len(lines) == 1:
        return ""  # only the header, no actual messages
    return "\n".join(lines)


def load_profile_persona() -> str:
    """Load the active profile's ``SOUL.md`` (or equivalent) as a
    voice-friendly persona block.

    OC profiles can declare a ``SOUL.md`` (Hermes-parity) with a short
    persona description. Voice-friendly because it's typically written
    as instructions for the agent's personality, not as a coding-style
    system prompt. Falls back to ``""`` when missing.
    """
    try:
        from opencomputer.agent.config import _home  # noqa: PLC0415
    except ImportError:
        return ""

    soul_path = _home() / "SOUL.md"
    if not soul_path.exists():
        return ""
    try:
        text = soul_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    # Strip trailing/leading markdown headers if present — keep the body only.
    return text


__all__ = [
    "compose_system_prompt",
    "load_profile_persona",
    "load_recent_messages",
    "registered_tools_for_realtime",
]
