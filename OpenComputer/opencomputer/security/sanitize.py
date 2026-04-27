"""
Wrap-on-ingestion helper for prompt-injection defense (Phase 3.G).

This is the one-call sanitizer used by tools that fetch external
content (web pages, file bodies, email payloads, etc.). It runs
:class:`opencomputer.security.instruction_detector.InstructionDetector`
over the payload, and:

* If the verdict recommends quarantine, wraps the content in
  :func:`InstructionDetector.wrap`'s
  ``<quarantined-untrusted-content>`` envelope AND publishes a
  :class:`plugin_sdk.ingestion.HookSignalEvent` to
  :data:`opencomputer.ingestion.bus.default_bus` so audit + evolution
  subscribers can record the defense activity.
* Otherwise returns the content unchanged.

The bus publish is best-effort — exceptions from :meth:`publish` are
caught and logged at WARNING. Sanitize must NEVER break the caller.
"""

from __future__ import annotations

import logging

from opencomputer.security.instruction_detector import (
    InstructionDetector,
    default_detector,
)
from plugin_sdk.ingestion import HookSignalEvent

_log = logging.getLogger("opencomputer.security.sanitize")


def sanitize_external_content(
    content: str,
    *,
    source: str = "external",
    session_id: str | None = None,
    detector: InstructionDetector | None = None,
) -> str:
    """Detect + (optionally) quarantine injection attempts in external content.

    Parameters
    ----------
    content:
        Raw content fetched from the external source.
    source:
        Short identifier for the emitter, propagated to the
        :class:`HookSignalEvent` ``source`` field. Convention:
        ``"introspection"``, ``"web_fetch"``, ``"file_read"``, etc.
    session_id:
        The active agent session id, propagated to the bus event so
        per-session subscribers can correlate. Pass ``None`` for
        system-emitted events outside any session.
    detector:
        Optional :class:`InstructionDetector` instance. Defaults to
        the module-level lazy singleton via :func:`default_detector`.
        Override only when you need a non-default config (e.g.
        per-source threshold tuning).

    Returns
    -------
    str
        Either the original ``content`` (clean) or a wrapped envelope
        (quarantined) ready to hand back to the LLM.
    """
    det = detector or default_detector()
    verdict = det.detect(content, context=source)

    if not verdict.quarantine_recommended:
        return content

    # Quarantined — publish a HookSignalEvent so audit + trajectory
    # subscribers can record the defense activity. We import the bus
    # lazily inside the function so a broken bus import path can't
    # poison module load (defense in depth: this module must be safe
    # to import even if the bus singleton fails to construct).
    try:
        from opencomputer.ingestion.bus import default_bus

        rules = ",".join(verdict.triggered_rules) or "n/a"
        event = HookSignalEvent(
            session_id=session_id,
            source=source,
            hook_name="instruction_detector",
            decision="block",
            reason=f"quarantined: {rules}",
            metadata={
                "confidence": verdict.confidence,
                "triggered_rules": list(verdict.triggered_rules),
            },
        )
        default_bus.publish(event)
    except Exception as e:  # noqa: BLE001 — sanitize must never break the caller
        _log.warning(
            "sanitize_external_content: bus publish failed (continuing): %s",
            e,
            exc_info=True,
        )

    return det.wrap(content, verdict)


__all__ = ["sanitize_external_content"]
