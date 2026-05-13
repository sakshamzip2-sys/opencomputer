"""HandoffGenerator — produces a HandoffDocument from session context.

Calls the configured provider with the handoff-protocol v2.0 prompt,
parses the response, and returns a complete :class:`HandoffDocument`
ready for inbox write. Fail-closed: any provider error, timeout, or
parse failure raises :class:`HandoffGenerationError` so the caller can
abort the profile swap.

Retries: a single retry on transient errors (network glitch, rate-limit
that signals retry-after <= 5s). Non-transient errors fail fast. The
classifier WOULD trigger again next turn anyway, so aggressive retry
just burns tokens.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from opencomputer.agent.handoff.models import (
    HandoffDocument,
    HandoffMetadata,
    HandoffWarranted,
)
from opencomputer.agent.handoff.protocol_v2 import (
    PROTOCOL_VERSION,
    parse_handoff_response,
    render_handoff_prompt,
)

_log = logging.getLogger("opencomputer.agent.handoff.generator")

#: Hard cap for the provider call. Handoffs are short and cheap; if the
#: provider can't return in this window the swap is aborted.
_GENERATION_TIMEOUT_S: float = 30.0

#: Single retry on transient failures (timeout, 5xx, rate-limit-retry-after).
_MAX_RETRIES: int = 1

#: Backoff before the single retry.
_RETRY_BACKOFF_S: float = 2.0


class HandoffGenerationError(RuntimeError):
    """Generator failed and the swap MUST be aborted.

    Sub-classified via ``cause`` (the underlying exception, if any) for
    callers that want to telemetry-bucket failures. Otherwise treated as
    a single fail-closed signal.
    """

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class _ProviderProtocol(Protocol):
    """Minimal duck-typed shape for the provider call.

    Decouples the generator from ``opencomputer.providers.BaseProvider``
    so tests can mock with a 20-line fake. The real call site uses the
    existing ``provider.complete(...)`` method via an adapter — see
    :func:`generate_handoff` for the wiring.
    """

    async def complete_text(
        self, *, system: str, user: str, timeout_s: float
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class GeneratorInput:
    """Inputs to a generation call — kept frozen so the caller can't
    mutate the request between retries."""
    source_profile: str
    target_profile: str
    source_session_id: str
    recent_user_messages: tuple[str, ...]
    recent_assistant_messages: tuple[str, ...]
    trigger: str  # "auto" | "manual" | "cli"
    classifier_confidence: float | None = None
    classifier_reason: str | None = None


class HandoffGenerator:
    """Generates a :class:`HandoffDocument` from a :class:`GeneratorInput`.

    The provider call goes through a duck-typed protocol so this class
    has no import dependency on ``opencomputer.providers.*`` — it's
    constructed with a tiny adapter that exposes ``complete_text``.
    """

    def __init__(
        self,
        provider: _ProviderProtocol,
        *,
        timeout_s: float = _GENERATION_TIMEOUT_S,
        max_retries: int = _MAX_RETRIES,
        clock: callable[[], float] | None = None,  # noqa: UP007
    ) -> None:
        if timeout_s <= 0:
            raise ValueError(f"timeout_s must be > 0 (got {timeout_s})")
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0 (got {max_retries})")
        self._provider = provider
        self._timeout_s = float(timeout_s)
        self._max_retries = int(max_retries)
        self._clock = clock or time.monotonic

    async def generate(self, req: GeneratorInput) -> HandoffDocument | None:
        """Return a handoff doc, or ``None`` when Step 0 says "not warranted".

        Raises :class:`HandoffGenerationError` on any unrecoverable failure
        (timeout after retry, provider error after retry, parse failure on
        non-empty content). Callers MUST treat None as "no handoff this
        time, proceed without one" vs. exception as "abort the swap".
        """
        if not isinstance(req, GeneratorInput):
            raise TypeError(
                f"expected GeneratorInput, got {type(req).__name__}"
            )
        if req.source_profile == req.target_profile:
            raise ValueError(
                "source and target profiles are identical — generator should "
                "never have been called"
            )
        if req.trigger not in ("auto", "manual", "cli"):
            raise ValueError(f"invalid trigger: {req.trigger!r}")

        prompt = render_handoff_prompt(
            source_profile=req.source_profile,
            target_profile=req.target_profile,
            recent_user_messages=req.recent_user_messages,
            recent_assistant_messages=req.recent_assistant_messages,
        )

        raw = await self._complete_with_retry(prompt.system, prompt.user)
        parsed = parse_handoff_response(raw)

        if parsed.warranted != HandoffWarranted.YES:
            _log.info(
                "handoff %s → %s: protocol Step 0 returned %s (%s)",
                req.source_profile,
                req.target_profile,
                parsed.warranted.value,
                parsed.reason or "no reason given",
            )
            return None

        if not parsed.body.strip():
            raise HandoffGenerationError(
                "model returned non-warranted-prefix but empty body"
            )

        meta = HandoffMetadata(
            protocol_version=PROTOCOL_VERSION,
            source_profile=req.source_profile,
            target_profile=req.target_profile,
            generated_at=_iso_utc_now(),
            source_session_id=req.source_session_id,
            trigger=req.trigger,  # type: ignore[arg-type]  — narrowed above
            classifier_confidence=req.classifier_confidence,
            classifier_reason=req.classifier_reason,
        )
        return HandoffDocument(metadata=meta, body=parsed.body)

    async def _complete_with_retry(self, system: str, user: str) -> str:
        last_exc: BaseException | None = None
        for attempt in range(self._max_retries + 1):
            try:
                started = self._clock()
                raw = await asyncio.wait_for(
                    self._provider.complete_text(
                        system=system, user=user, timeout_s=self._timeout_s,
                    ),
                    timeout=self._timeout_s + 1.0,  # wait_for budget > provider budget
                )
                elapsed = self._clock() - started
                _log.debug(
                    "handoff generation: provider returned in %.2fs (attempt %d)",
                    elapsed, attempt + 1,
                )
                if raw is None:
                    raise HandoffGenerationError("provider returned None")
                if not isinstance(raw, str):
                    raise HandoffGenerationError(
                        f"provider returned non-string {type(raw).__name__}"
                    )
                return raw
            except TimeoutError as e:
                last_exc = e
                _log.warning(
                    "handoff generation timed out after %.1fs (attempt %d/%d)",
                    self._timeout_s, attempt + 1, self._max_retries + 1,
                )
            except (ConnectionError, OSError) as e:
                last_exc = e
                _log.warning(
                    "handoff generation transient error (attempt %d/%d): %s",
                    attempt + 1, self._max_retries + 1, e,
                )
            except HandoffGenerationError:
                raise
            except Exception as e:  # noqa: BLE001 — provider impls vary
                last_exc = e
                _log.warning(
                    "handoff generation provider error (attempt %d/%d): %s: %s",
                    attempt + 1, self._max_retries + 1,
                    type(e).__name__, e,
                )

            if attempt < self._max_retries:
                await asyncio.sleep(_RETRY_BACKOFF_S)

        raise HandoffGenerationError(
            f"handoff generation failed after {self._max_retries + 1} attempts",
            cause=last_exc,
        )


# ─── helpers ──────────────────────────────────────────────────────────


def _iso_utc_now() -> str:
    """ISO-8601 UTC timestamp with trailing ``Z`` (no microseconds)."""
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_recent_messages(
    messages: Sequence[object],
    *,
    max_messages: int = 24,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Walk a message history (the same kind ``InjectionContext.messages``
    carries) and pull out the last ``max_messages`` user + assistant strings.

    Tolerates any object with ``.role`` and ``.content`` attributes (the
    shape every Message dataclass in this codebase has). Non-string
    content (tool_use lists, structured tool_result) is rendered as a
    short marker so the protocol sees "[tool: Edit]" rather than a Python
    repr blob — keeps the prompt readable + portable per R10.
    """
    if max_messages < 1:
        raise ValueError(f"max_messages must be >= 1 (got {max_messages})")
    users: list[str] = []
    assistants: list[str] = []
    for m in messages[-max_messages * 2:]:
        role = getattr(m, "role", None)
        content = getattr(m, "content", None)
        text = _render_content(content)
        if not text.strip():
            continue
        if role == "user":
            users.append(text)
        elif role == "assistant":
            assistants.append(text)
        # other roles (system, tool) intentionally skipped
    return tuple(users[-max_messages:]), tuple(assistants[-max_messages:])


def _render_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, Iterable):
        parts: list[str] = []
        for chunk in content:
            chunk_type = getattr(chunk, "type", None) or (
                chunk.get("type") if isinstance(chunk, dict) else None
            )
            if chunk_type == "text":
                txt = getattr(chunk, "text", None) or (
                    chunk.get("text") if isinstance(chunk, dict) else None
                )
                if isinstance(txt, str):
                    parts.append(txt)
            elif chunk_type == "tool_use":
                name = getattr(chunk, "name", None) or (
                    chunk.get("name") if isinstance(chunk, dict) else "?"
                )
                parts.append(f"[tool_use: {name}]")
            elif chunk_type == "tool_result":
                parts.append("[tool_result]")
        return " ".join(parts)
    return str(content)


__all__ = [
    "GeneratorInput",
    "HandoffGenerationError",
    "HandoffGenerator",
    "collect_recent_messages",
]
