"""End-to-end orchestration for auto-swap and provider adapters.

This module is the glue between the agent loop and the rest of the
handoff subsystem. It exposes:

  - :class:`ProviderAdapter` — duck-typed wrapper around the active
    provider that exposes the ``complete_text`` shape :class:`HandoffGenerator`
    expects, without coupling to any specific provider implementation.
  - :func:`run_auto_swap_pipeline` — the one function the agent loop
    calls per turn. Runs the classifier, evaluates the trigger, and if
    swap is warranted generates the handoff + writes the inbox + queues
    the swap + writes the audit row.

All failure modes are caught at this boundary so the loop never breaks
on a handoff-subsystem issue.
"""
from __future__ import annotations

import datetime as _dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from opencomputer.agent.handoff.audit import HandoffAuditLogger, SwapAuditEvent
from opencomputer.agent.handoff.auto_swap import (
    AutoSwapTrigger,
    SwapDecision,
    SwapDecisionReason,
)
from opencomputer.agent.handoff.generator import (
    GeneratorInput,
    HandoffGenerationError,
    HandoffGenerator,
    collect_recent_messages,
)
from opencomputer.agent.handoff.inbox import HandoffInbox, InboxIOError
from opencomputer.agent.handoff.models import HandoffDocument
from opencomputer.awareness.personas.classifier import (
    ClassificationContext,
    ClassificationResult,
    classify,
)

_log = logging.getLogger("opencomputer.agent.handoff.orchestrator")


# Tool input keys that hold a file path. Covers built-in tools
# (Read/Edit/Write/MultiEdit/Glob/NotebookRead/NotebookEdit) plus
# common coding-harness variants.
_FILE_PATH_KEYS: frozenset[str] = frozenset(
    {
        "file_path", "filePath", "path", "notebook_path", "notebookPath",
        "absolute_path", "absolutePath",
    }
)

_MAX_RECENT_FILE_PATHS: int = 32


def _extract_recent_file_paths(messages: Sequence[Any]) -> tuple[str, ...]:
    """Pull file paths off recent tool_use blocks for the classifier.

    The classifier weights extension-frequency heuristics (.py for
    coding, .md for learning) — see
    ``opencomputer/awareness/personas/classifier.py:161-172``. Without
    real paths these signals are dead.

    Strategy: scan recent assistant turns for content blocks shaped
    like ``{"type": "tool_use", "input": {"file_path": "..."}}``,
    de-dupe while preserving order, cap at
    ``_MAX_RECENT_FILE_PATHS``.

    Args:
        messages: The same iterable handed to the streak check. Can
            be Anthropic-shaped dicts, OAI-shaped dicts, or
            ``RuntimeContext.ChatMessage``-ish objects with ``.role`` /
            ``.content``. We only look at ``role == "assistant"``.

    Returns:
        Tuple of file paths in oldest-to-newest order. Empty if the
        input is malformed, empty, or contains no tool_use blocks.
    """
    seen: dict[str, None] = {}  # ordered set
    for msg in messages or ():
        try:
            role = (
                msg.get("role") if isinstance(msg, dict)
                else getattr(msg, "role", "")
            ) or ""
            if role != "assistant":
                continue
            content = (
                msg.get("content") if isinstance(msg, dict)
                else getattr(msg, "content", None)
            )
            # Content can be a string (no tools) or list of blocks.
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                tool_input = block.get("input")
                if not isinstance(tool_input, dict):
                    continue
                for key in _FILE_PATH_KEYS:
                    val = tool_input.get(key)
                    if isinstance(val, str) and val.strip():
                        seen[val] = None
                        if len(seen) >= _MAX_RECENT_FILE_PATHS:
                            return tuple(seen)
        except Exception:  # noqa: BLE001 — never break classifier on extraction
            continue
    return tuple(seen)


class _AnyProvider(Protocol):
    """The duck-typed shape we need from the active provider.

    Real providers in this codebase expose ``complete`` (async) returning
    a structured response. The adapter wraps that into the simpler
    ``complete_text`` shape the generator wants.
    """

    async def complete(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class ProviderAdapter:
    """Wraps any provider with a ``complete`` method into the ``complete_text``
    shape ``HandoffGenerator`` expects.

    Constructed once per AgentLoop and shared across turns. If the loop
    swaps the underlying provider (``/model`` slash, Alt+M cycle, etc.)
    a fresh adapter is built — adapters are cheap.
    """

    provider: _AnyProvider
    model_id: str = ""

    async def complete_text(
        self, *, system: str, user: str, timeout_s: float,
    ) -> str:
        """Single-shot text completion: 2 messages, no tools, no streaming.

        Adapts to whichever ``complete`` signature the wrapped provider
        exposes. Most OC providers accept (messages, system, model, tools,
        ...) kwargs — we try the canonical shape first, then a permissive
        fallback. Errors propagate to the generator's retry loop.
        """
        # The minimal message list we send. Note: some providers require
        # the user role to be in the ``messages`` and ``system`` to live
        # outside it — the canonical OC shape.
        messages = [{"role": "user", "content": user}]

        # Try the canonical opencomputer provider shape first.
        try:
            resp = await self.provider.complete(  # type: ignore[call-arg]
                messages=messages,
                system=system,
                tools=(),
                stream=False,
                timeout_s=timeout_s,
            )
        except TypeError:
            # Fallback: minimal positional+kwarg shape.
            resp = await self.provider.complete(  # type: ignore[call-arg]
                messages, system=system,
            )

        return _extract_text(resp)


def _extract_text(resp: Any) -> str:
    """Pull a plain text string out of whatever the provider returned.

    Recognised shapes (canonical OC ``ProviderResponse`` first):

    1. ``ProviderResponse(message=Message(content=str|list))``
       The canonical OC shape (``plugin_sdk.provider_contract``).
    2. Object with ``.text`` (str) or ``.content`` (str|list of typed blocks).
    3. Plain ``str``.
    4. ``dict`` with ``text`` / ``content`` / ``output_text`` keys.
    """
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp

    # Canonical OC ``ProviderResponse`` shape — wrap a Message
    message = getattr(resp, "message", None)
    if message is not None:
        msg_content = getattr(message, "content", None)
        extracted = _extract_text_from_content(msg_content)
        if extracted:
            return extracted

    # Direct ``.text`` (e.g. OpenAI response objects)
    text = getattr(resp, "text", None)
    if isinstance(text, str):
        return text

    # Direct ``.content`` on the response object
    direct_content = getattr(resp, "content", None)
    direct_text = _extract_text_from_content(direct_content)
    if direct_text:
        return direct_text

    # Dict shape (Anthropic raw, etc.)
    if isinstance(resp, dict):
        for key in ("text", "content", "output_text"):
            v = resp.get(key)
            if isinstance(v, str):
                return v
            extracted = _extract_text_from_content(v)
            if extracted:
                return extracted
    return ""


def _extract_text_from_content(content: Any) -> str:
    """Render a ``Message.content`` field — str or list of typed blocks — to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for chunk in content:
            chunk_type = getattr(chunk, "type", None) or (
                chunk.get("type") if isinstance(chunk, dict) else None
            )
            if chunk_type == "text":
                txt = getattr(chunk, "text", None) or (
                    chunk.get("text") if isinstance(chunk, dict) else None
                )
                if isinstance(txt, str):
                    out.append(txt)
        if out:
            return "\n".join(out)
    return ""


# ─── auto-swap pipeline ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AutoSwapResult:
    """Outcome of the per-turn auto-swap pipeline."""
    decision: SwapDecision
    handoff_path: Path | None = None
    error: str | None = None
    queued: bool = False
    notification: str = ""


async def run_auto_swap_pipeline(
    *,
    trigger: AutoSwapTrigger,
    runtime: Any,
    session_id: str,
    current_profile: str,
    available_profiles: tuple[str, ...],
    last_user_messages: Sequence[str],
    recent_messages: Sequence[Any],
    plan_mode: bool,
    auto_off: bool,
    is_gateway_session: bool,
    gateway_optin: bool,
    target_profile_home_resolver: callable[[str], Path],  # noqa: UP007
    provider_adapter: ProviderAdapter | None,
    audit_logger: HandoffAuditLogger | None,
    foreground_app: str = "",
    window_title: str = "",
    profile_home: str = "",
) -> AutoSwapResult:
    """One-shot per-turn pipeline. Never raises — all errors → AutoSwapResult.

    The loop calls this BEFORE :func:`_apply_pending_profile_swap`. If
    ``decision.should_swap`` and the generator succeeds, this function
    queues the pending swap and the next call to _apply_pending will
    consume it.

    Steps:
      1. Run classifier over the last few user messages.
      2. Evaluate the trigger (advances rolling window + checks streak).
      3. If FIRED: generate handoff, write inbox, queue swap, audit ALLOW.
      4. If FIRED but generator/inbox failed: abort swap, audit ABORT.
      5. If gated (cooldown / plan / gateway / etc.): audit DEFERRED.
    """
    # Step 1 — classifier
    try:
        ctx = ClassificationContext(
            foreground_app=foreground_app,
            # v3 (2026-05-15) — was hardcoded to 12 (noon) — classifier
            # uses this for evening/morning persona routing
            # (classifier.py:167-172). With the local wall-clock hour the
            # "relaxed" (≥21 or <6) and "coding default" (9-12) buckets
            # actually fire when they should.
            time_of_day_hour=_dt.datetime.now().hour,
            # v3 (2026-05-15) — was hardcoded to (). Classifier weights
            # 3+ recent .py = coding, 3+ recent .md = learning. Pulling
            # from the same recent_messages we already feed the streak
            # check costs nothing and restores the signal.
            recent_file_paths=_extract_recent_file_paths(recent_messages),
            last_messages=tuple(last_user_messages),
            window_title=window_title,
            profile_home=profile_home,
        )
        cls: ClassificationResult = classify(ctx)
    except Exception as e:  # noqa: BLE001 — never break the turn
        _log.warning("classifier raised, skipping auto-swap eval: %s", e)
        return AutoSwapResult(
            decision=SwapDecision(
                None, SwapDecisionReason.AUTO_OFF, confidence=0.0
            ),
            error=f"classifier failed: {e}",
        )

    # Step 2 — trigger evaluation
    decision = trigger.evaluate(
        runtime=runtime,
        session_id=session_id,
        classification=cls,
        current_profile=current_profile,
        available_profiles=available_profiles,
        plan_mode=plan_mode,
        auto_off=auto_off,
        is_gateway_session=is_gateway_session,
        gateway_optin=gateway_optin,
    )

    if not decision.should_swap:
        _maybe_audit_deferred(audit_logger, decision, current_profile, session_id)
        return AutoSwapResult(decision=decision)

    # Step 3 — generate handoff
    if provider_adapter is None:
        _log.warning(
            "auto-swap eligible (%s->%s) but no provider adapter "
            "available — aborting swap",
            current_profile, decision.target_profile,
        )
        _audit_abort(
            audit_logger, decision, current_profile, session_id,
            reason="no provider adapter",
        )
        return AutoSwapResult(
            decision=decision,
            error="no provider adapter",
        )

    target = decision.target_profile  # narrowed by should_swap
    assert target is not None  # for type-checkers; should_swap guarantees

    users, assistants = collect_recent_messages(recent_messages)
    generator = HandoffGenerator(provider_adapter)
    try:
        doc: HandoffDocument | None = await generator.generate(
            GeneratorInput(
                source_profile=current_profile,
                target_profile=target,
                source_session_id=session_id,
                recent_user_messages=users,
                recent_assistant_messages=assistants,
                trigger="auto",
                classifier_confidence=decision.confidence,
                classifier_reason=decision.classifier_reason,
            ),
        )
    except HandoffGenerationError as e:
        _log.warning(
            "auto-swap %s->%s aborted: handoff generation failed: %s",
            current_profile, target, e,
        )
        _audit_abort(
            audit_logger, decision, current_profile, session_id,
            reason=f"generation failed: {e}",
        )
        return AutoSwapResult(
            decision=decision,
            error=f"handoff generation failed: {e}",
        )

    # Step 4 — write inbox if handoff was produced
    handoff_path: Path | None = None
    if doc is not None:
        try:
            target_home = target_profile_home_resolver(target)
            inbox = HandoffInbox(target_home)
            handoff_path = inbox.write(doc)
        except (InboxIOError, OSError, ValueError) as e:
            _log.warning(
                "auto-swap %s->%s aborted: inbox write failed: %s",
                current_profile, target, e,
            )
            _audit_abort(
                audit_logger, decision, current_profile, session_id,
                reason=f"inbox write failed: {e}",
            )
            return AutoSwapResult(
                decision=decision,
                error=f"inbox write failed: {e}",
            )

    # Step 5 — queue swap + audit ALLOW + notification
    runtime.custom["pending_profile_id"] = target
    trigger.mark_swapped(runtime=runtime, session_id=session_id)

    if audit_logger is not None:
        audit_logger.append(
            SwapAuditEvent(
                session_id=session_id,
                source_profile=current_profile,
                target_profile=target,
                trigger="auto",
                outcome="allow",
                reason=(
                    f"classifier sustained ≥{decision.confidence:.2f} for "
                    f"persona={decision.persona}"
                ),
                classifier_persona=decision.persona,
                classifier_confidence=decision.confidence,
                handoff_path=str(handoff_path) if handoff_path else "",
            ),
        )

    notification = _build_notification(
        current_profile, target, has_handoff=handoff_path is not None,
    )
    runtime.custom["profile_swap_notification"] = {
        "from_profile": current_profile,
        "to_profile": target,
        "trigger": "auto",
        "confidence": decision.confidence,
        "handoff_path": str(handoff_path) if handoff_path else "",
        "message": notification,
    }

    # Publish a ProfileSwapEvent to the in-process bus. WireServer
    # subscribes to this and broadcasts a typed ``profile.swap`` wire
    # event so cross-process UIs (hermes-workspace SPA, TUI, IDE
    # bridges) render the swap notification in real time without
    # polling. Best-effort: bus publish never blocks the swap.
    _publish_swap_event(
        from_profile=current_profile,
        to_profile=target,
        trigger="auto",
        confidence=decision.confidence,
        classifier_reason=decision.classifier_reason,
        has_handoff=handoff_path is not None,
    )

    _log.info(
        "auto-swap queued: %s -> %s (confidence=%.2f, handoff=%s)",
        current_profile, target, decision.confidence,
        handoff_path.name if handoff_path else "(none)",
    )

    return AutoSwapResult(
        decision=decision,
        handoff_path=handoff_path,
        queued=True,
        notification=notification,
    )


# ─── audit helpers ────────────────────────────────────────────────────


_DEFERRED_REASONS_TO_AUDIT = {
    SwapDecisionReason.COOLDOWN_ACTIVE,
    SwapDecisionReason.PLAN_MODE,
    SwapDecisionReason.GATEWAY_DISABLED,
    SwapDecisionReason.AUTO_OFF,
}


def _maybe_audit_deferred(
    audit_logger: HandoffAuditLogger | None,
    decision: SwapDecision,
    current_profile: str,
    session_id: str,
) -> None:
    """Audit a deferral ONLY if it represents a policy gate firing.

    Below-threshold / streak-incomplete / mixed-personas decisions happen
    every turn — auditing them all floods the chain. We audit only when
    a policy gate (cooldown, plan-mode, gateway, off) suppresses an
    otherwise-eligible swap.
    """
    if audit_logger is None:
        return
    if decision.reason not in _DEFERRED_REASONS_TO_AUDIT:
        return
    # Audit only if the streak conditions would have fired — otherwise
    # the gate is irrelevant. Conservative: audit when persona is real
    # AND confidence high. (We don't have the full streak here, so this
    # is best-effort.)
    if not decision.persona or decision.persona == "default":
        return
    if decision.confidence < 0.5:
        return
    audit_logger.append(
        SwapAuditEvent(
            session_id=session_id,
            source_profile=current_profile,
            target_profile=decision.target_profile or "(unresolved)",
            trigger="auto",
            outcome="deferred",
            reason=f"gate={decision.reason.value}",
            classifier_persona=decision.persona,
            classifier_confidence=decision.confidence,
        ),
    )


def _audit_abort(
    audit_logger: HandoffAuditLogger | None,
    decision: SwapDecision,
    current_profile: str,
    session_id: str,
    *,
    reason: str,
) -> None:
    if audit_logger is None:
        return
    audit_logger.append(
        SwapAuditEvent(
            session_id=session_id,
            source_profile=current_profile,
            target_profile=decision.target_profile or "(unresolved)",
            trigger="auto",
            outcome="abort",
            reason=reason,
            classifier_persona=decision.persona,
            classifier_confidence=decision.confidence,
        ),
    )


def _build_notification(
    source: str, target: str, *, has_handoff: bool,
) -> str:
    suffix = " (handoff written)" if has_handoff else " (no handoff)"
    return f"↪ @{target}{suffix}"


def _publish_swap_event(
    *,
    from_profile: str,
    to_profile: str,
    trigger: str,
    confidence: float,
    classifier_reason: str,
    has_handoff: bool,
) -> None:
    """Publish a ProfileSwapEvent to default_bus. Best-effort.

    Failure modes that MUST NOT propagate:
      - default_bus import fails (rare; constructor-time fault)
      - publish raises (bus is full, subscriber raises sync)
      - ProfileSwapEvent constructor rejects field type
    Any of the above logs at DEBUG and silently returns.
    """
    try:
        from opencomputer.ingestion.bus import default_bus
        from plugin_sdk.ingestion import ProfileSwapEvent

        default_bus.publish(
            ProfileSwapEvent(
                from_profile=from_profile,
                to_profile=to_profile,
                trigger=trigger,
                classifier_confidence=float(confidence),
                classifier_reason=classifier_reason[:200],
                has_handoff=has_handoff,
            ),
        )
    except Exception:  # noqa: BLE001 — bus publish must never block a swap
        _log.debug("ProfileSwapEvent publish failed", exc_info=True)


__all__ = [
    "AutoSwapResult",
    "ProviderAdapter",
    "run_auto_swap_pipeline",
]
