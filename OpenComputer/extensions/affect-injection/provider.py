"""AffectInjectionProvider — per-turn user-state surface.

Implements the DynamicInjectionProvider contract from
``plugin_sdk/injection.py``. Returns a structured ``<user-state>`` block
on each turn that carries up to three signals:

  vibe          — most recent regex-classified vibe over the last
                  1-2 user messages (per-turn, computed in-memory).
  recent_arc    — transition between the previous turn's vibe and the
                  current one, only when they differ.
  active_pattern — pattern_id of the most recent life-event firing,
                  only when ``surfacing == "hint"`` (silent firings
                  stay out of the chat surface).

When all three signals are absent or carry no information (vibe ==
"calm" with no transition and no pattern), ``collect`` returns ``None``
so the injection engine emits nothing.

Read-only: the provider does not mutate sessions.db, vibe_log, the F4
graph, or the life-event registry. No LLM calls. The vibe classifier
itself is pure regex.

Prompt B contract (2026-04-28). See A-CONTRACT.md for the upstream
vibe-classification contract this plugin layers on top of.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext

_log = logging.getLogger("opencomputer.affect_injection")


class AffectInjectionProvider(DynamicInjectionProvider):
    """Surface vibe + arc + life-event pattern as a <user-state> block."""

    #: Lower runs first. Plan mode = 10, yolo = 20, user-modes = 50+,
    #: this provider sits above generic user providers but below the
    #: cross-cutting safety / mode injectors.
    priority: int = 60

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        min_turns: int = 2,
    ) -> None:
        self._db_path = db_path
        self._db: SessionDB | None = None
        self._min_turns = max(0, int(min_turns))
        # Per-session state: maps session_id → previous-turn vibe label.
        # In-memory only; lost on process restart, which is fine — the
        # arc detector just falls through to "no arc" on the first turn
        # of a new run.
        self._prev_turn_vibe: dict[str, str] = {}

    @property
    def provider_id(self) -> str:
        return "affect-injection:v1"

    def _ensure_db(self) -> SessionDB | None:
        """Lazily resolve a SessionDB instance.

        Returns None when no path is available (the provider degrades to
        no session_vibe rather than raising — every read is wrapped in
        try/except so the prompt assembly never breaks over a missing
        DB).
        """
        if self._db is not None:
            return self._db
        if self._db_path is None:
            return None
        try:
            self._db = SessionDB(self._db_path)
        except Exception:  # noqa: BLE001 — defensive: never break collect()
            _log.debug("SessionDB construction failed", exc_info=True)
            return None
        return self._db

    def _read_session_vibe(self, session_id: str) -> str | None:
        db = self._ensure_db()
        if db is None or not session_id:
            return None
        try:
            vibe, _ts = db.get_session_vibe(session_id)
            return vibe
        except Exception:  # noqa: BLE001 — defensive
            _log.debug("get_session_vibe failed", exc_info=True)
            return None

    def _per_turn_vibe(self, messages: tuple[Message, ...]) -> str:
        """Run the regex classifier on the last 1-2 user messages.

        Pure function; same classifier used at session-start by
        ``_build_persona_overlay``. Falls back to ``"calm"`` on empty
        input to match :func:`opencomputer.agent.vibe_classifier.classify_vibe`.
        """
        from opencomputer.agent.vibe_classifier import classify_vibe

        recent: list[str] = []
        for m in reversed(messages):
            if m.role == "user" and isinstance(m.content, str):
                recent.append(m.content)
                if len(recent) >= 2:
                    break
        recent.reverse()
        try:
            return classify_vibe(recent) if recent else "calm"
        except Exception:  # noqa: BLE001 — defensive: classifier must never raise
            _log.debug("classify_vibe raised; defaulting to calm", exc_info=True)
            return "calm"

    def _active_hint_pattern(self) -> str | None:
        """Peek the most-recent life-event firing if surfacing == 'hint'.

        Non-destructive read via ``LifeEventRegistry.peek_most_recent_firing``.
        Silent surfacings stay out of the chat surface — they exist so
        the F4 graph can record HealthEvent / RelationshipShift etc.
        without forcing an explicit chat reference.
        """
        try:
            from opencomputer.awareness.life_events.registry import (
                get_global_registry,
            )

            firing = get_global_registry().peek_most_recent_firing()
        except Exception:  # noqa: BLE001 — defensive
            _log.debug("life-event peek failed", exc_info=True)
            return None
        if firing is None:
            return None
        if getattr(firing, "surfacing", None) != "hint":
            return None
        pattern_id = getattr(firing, "pattern_id", None)
        return str(pattern_id) if pattern_id else None

    async def collect(self, ctx: InjectionContext) -> str | None:
        # Cron / flush / review batch contexts don't surface affect — the
        # background lanes shouldn't drag user-state framing into outputs
        # they're not delivering to a user. Mirrors MemoryBridge.prefetch
        # at memory_bridge.py:233-234.
        if getattr(ctx.runtime, "agent_context", "chat") != "chat":
            return None

        # Silent for the first ``min_turns`` of the session — there is
        # almost no signal yet, and emitting "vibe: calm" on turn 1 just
        # adds noise. Default min_turns=2.
        # turn_index == 0 is the SDK's "caller did not thread the counter"
        # sentinel — treat it as "always emit" rather than "always silent"
        # to match the contract on InjectionContext.turn_index.
        if (
            ctx.turn_index > 0
            and self._min_turns > 0
            and ctx.turn_index < self._min_turns
        ):
            return None

        session_id = ctx.session_id or ""
        session_vibe = self._read_session_vibe(session_id)
        per_turn_vibe = self._per_turn_vibe(ctx.messages)
        prev_turn_vibe = self._prev_turn_vibe.get(session_id)

        # Update in-memory arc state for next call. We update before the
        # emit gate so even when we skip the block (e.g. calm with no
        # signal), the next turn can still detect a transition.
        self._prev_turn_vibe[session_id] = per_turn_vibe

        active_pattern = self._active_hint_pattern()

        # Decide what to emit. Per-turn vibe is the primary signal; fall
        # back to the session-level vibe when the per-turn classifier
        # returned the calm default (no per-turn signal but a non-calm
        # session-level marker is still useful).
        primary_vibe: str | None = per_turn_vibe
        if primary_vibe == "calm" and session_vibe and session_vibe != "calm":
            primary_vibe = session_vibe

        has_arc = (
            prev_turn_vibe is not None
            and per_turn_vibe is not None
            and prev_turn_vibe != per_turn_vibe
        )

        # Skip-gate: nothing to say.
        no_vibe_signal = primary_vibe in (None, "", "calm")
        if no_vibe_signal and not has_arc and active_pattern is None:
            return None

        # Format the block. Always-fixed XML-style tags so downstream
        # consumers can parse without depending on field order.
        lines = ["<user-state>"]
        if primary_vibe and primary_vibe != "calm":
            lines.append(f"vibe: {primary_vibe}")
        if has_arc:
            lines.append(f"recent_arc: {prev_turn_vibe} -> {per_turn_vibe}")
        if active_pattern:
            lines.append(f"active_pattern: {active_pattern}")
        lines.append("</user-state>")
        return "\n".join(lines)


def affect_injection_provider_from_env(
    *,
    db_path: Path | None = None,
) -> AffectInjectionProvider:
    """Construct the provider with config sourced from env vars.

    Env vars:

    - ``AFFECT_INJECTION_MIN_TURNS`` — int, default 2. Number of opening
      turns to stay silent on so the provider doesn't emit "vibe: calm"
      when there's no signal yet.
    """
    raw = os.environ.get("AFFECT_INJECTION_MIN_TURNS", "2")
    try:
        min_turns = int(raw)
    except (TypeError, ValueError):
        min_turns = 2
    return AffectInjectionProvider(db_path=db_path, min_turns=min_turns)
