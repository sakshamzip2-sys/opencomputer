"""HTTP hook handler factory — POSTs the HookContext to a user URL.

CC §6 from ``docs/OC-FROM-CLAUDE-CODE.md``. Companion to the shell,
prompt, and agent handlers — same fail-open contract (a wedged hook
must never wedge the loop).

The endpoint is expected to reply with the same JSON decision shape
the shell handlers accept:

  - ``{"action": "block", "message": "..."}`` → block
  - ``{"decision": "block", "reason": "..."}`` → block
  - ``{"action": "approve" | "allow"}`` → pass
  - empty / non-JSON / non-2xx → pass (fail-open with warning)

The handler never raises out. Timeout, connection error, malformed
JSON, non-2xx — all log a warning and return ``decision="pass"``.

Env-var substitution happens on header values at fire time via
``os.path.expandvars`` so users can write ``Authorization: "Bearer
${MY_TOKEN}"`` in their YAML without baking secrets into the file.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from typing import Any

import httpx

from opencomputer.agent.config import HookHttpConfig
from plugin_sdk.hooks import HookContext, HookDecision, HookHandler

_log = logging.getLogger("opencomputer.hooks.http")


def _ctx_payload(ctx: HookContext) -> dict[str, Any]:
    """Serialize the HookContext into a JSON-able dict. Same shape the
    shell handler pipes to stdin, so endpoint authors can write one
    parser for both. ``runtime`` is dropped (mutable + not JSON-safe)."""
    raw = dataclasses.asdict(ctx)
    raw.pop("runtime", None)
    # ``messages`` may contain dataclasses; coerce via repr if they
    # don't round-trip through asdict (Message has slots).
    msgs = raw.get("messages")
    if msgs is not None:
        coerced: list[Any] = []
        for m in msgs:
            if isinstance(m, dict):
                coerced.append(m)
            elif hasattr(m, "__dict__"):
                coerced.append(
                    {k: v for k, v in m.__dict__.items() if not k.startswith("_")}
                )
            else:
                coerced.append(repr(m))
        raw["messages"] = coerced
    return raw


def _resolve_headers(config: HookHttpConfig) -> dict[str, str]:
    """Apply env-var expansion to header VALUES. Keys pass through."""
    out: dict[str, str] = {}
    for k, v in config.headers:
        if not k:
            continue
        out[k] = os.path.expandvars(v) if v else ""
    # Always tag the user agent so endpoints can identify the source.
    out.setdefault("User-Agent", "OpenComputer-Hook/1.0")
    out.setdefault("Content-Type", "application/json")
    return out


def _decision_from_body(body: dict[str, Any], event: str) -> HookDecision | None:
    """Translate the response JSON into a HookDecision.

    Returns ``None`` when no recognised key is present (caller treats
    as "no opinion" / pass). Mirrors ``shell_handlers._decision_from_stdout``
    but lives here to keep modules cohesive; if either drifts the
    behaviour comparison should call them out via tests.
    """

    def _is_block(v: object) -> bool:
        return isinstance(v, str) and v.strip().lower() == "block"

    def _is_approve(v: object) -> bool:
        return isinstance(v, str) and v.strip().lower() in ("approve", "allow")

    action = body.get("action")
    decision = body.get("decision")
    if _is_block(action):
        msg = body.get("message")
        return HookDecision(
            decision="block",
            reason=str(msg) if msg else "blocked by HTTP hook",
        )
    if _is_block(decision):
        reason = body.get("reason")
        return HookDecision(
            decision="block",
            reason=str(reason) if reason else "blocked by HTTP hook",
        )
    if _is_approve(action) or _is_approve(decision):
        return HookDecision(decision="pass")
    return None


def _parse_response(text: str, event: str) -> HookDecision | None:
    """JSON-parse the body. Malformed → log + None → caller treats
    as pass."""
    if not text or not text.strip():
        return None
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.warning(
            "http hook (%s): response body not JSON (first 80 chars: %r) — %s; passing",
            event,
            text[:80],
            exc,
        )
        return None
    if not isinstance(body, dict):
        return None
    return _decision_from_body(body, event)


def make_http_hook_handler(config: HookHttpConfig) -> HookHandler:
    """Wrap a :class:`HookHttpConfig` in an async :class:`HookHandler`.

    The returned handler is plug-and-play into :class:`HookSpec`.
    Same fail-open contract as the other settings-declared handler
    factories.
    """
    if not config.url:
        raise ValueError("HookHttpConfig.url is required")

    async def _handler(ctx: HookContext) -> HookDecision | None:
        try:
            payload = _ctx_payload(ctx)
            headers = _resolve_headers(config)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "http hook (%s): payload/headers prep failed: %s; passing",
                config.event,
                exc,
            )
            return None
        timeout = max(0.1, float(config.timeout_seconds))
        body_text: str = ""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    config.url,
                    content=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                )
        except (TimeoutError, httpx.TimeoutException):
            _log.warning(
                "http hook (%s): request timed out after %.1fs; passing",
                config.event,
                timeout,
            )
            return None
        except httpx.HTTPError as exc:
            _log.warning(
                "http hook (%s): transport error: %s; passing",
                config.event,
                exc,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — fail-open over correctness
            _log.warning(
                "http hook (%s): unexpected error: %s; passing",
                config.event,
                exc,
            )
            return None

        # 2xx required; everything else is fail-open.
        if response.status_code < 200 or response.status_code >= 300:
            _log.warning(
                "http hook (%s): non-2xx response %d from %s; passing",
                config.event,
                response.status_code,
                config.url,
            )
            return None

        # Cap response body to avoid runaway endpoints flooding the
        # agent loop.
        raw_bytes = response.content
        if len(raw_bytes) > config.max_response_bytes:
            _log.warning(
                "http hook (%s): response too large (%d > %d bytes); passing",
                config.event,
                len(raw_bytes),
                config.max_response_bytes,
            )
            return None
        try:
            body_text = raw_bytes.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 — defensive belt
            _log.warning(
                "http hook (%s): response decode failed: %s; passing",
                config.event,
                exc,
            )
            return None
        return _parse_response(body_text, config.event)

    return _handler


__all__ = ["make_http_hook_handler"]
