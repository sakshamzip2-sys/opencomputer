"""Shared workspace-context scanner (Hermes v2 parity, gap A).

Both startup workspace-context loading
(``prompt_builder.load_workspace_context``) and progressive subdirectory-hint
discovery (``subdirectory_hints._scan_context_content``) need to scrub
secrets + PII and wrap prompt-injection signatures in a quarantine
envelope before the content reaches the LLM. Keeping that policy in one
helper means the two callers cannot drift.

Pipeline:
  1. Redact runtime secrets + PII via :func:`redact_runtime_text_with_counts`.
  2. Run :func:`default_detector().detect` over the redacted text.
  3. If the detector recommends quarantine, wrap the redacted text in a
     ``<quarantined-untrusted-content>`` envelope with an HTML-comment
     warning naming the triggered rules + confidence + source label.

Always returns a string. Never raises — defensive against future
detector/redactor regressions because workspace-context loading is on the
hot path of every chat turn.
"""
from __future__ import annotations

import logging

from opencomputer.security.instruction_detector import default_detector
from opencomputer.security.redact import redact_runtime_text_with_counts

logger = logging.getLogger("opencomputer.security.context_scan")


def scan_workspace_context_content(raw: str, *, source: str) -> str:
    """Redact secrets, then quarantine prompt-injection if detected.

    Args:
        raw: file contents as read from disk.
        source: a short label identifying the source file (e.g.
            ``"AGENTS.md"``, ``".cursorrules"``); rendered into the
            HTML-comment warning so audits can trace which file tripped
            the detector.

    Returns:
        Scrubbed text. May be wrapped in a quarantine envelope.
    """
    if not raw:
        return raw

    try:
        redacted, counts = redact_runtime_text_with_counts(raw)
    except Exception as exc:  # noqa: BLE001 — never crash the prompt-build path
        logger.warning("context_scan: redaction failed for %s — %s", source, exc)
        redacted = raw
        counts = {}

    total = sum(counts.values())
    if total > 0:
        logger.info(
            "context_scan: redacted %d secret/PII occurrence(s) from %s before LLM",
            total,
            source,
        )

    try:
        verdict = default_detector().detect(redacted)
    except Exception as exc:  # noqa: BLE001 — fail-open, never wedge prompt build
        logger.warning("context_scan: detector failed for %s — %s", source, exc)
        return redacted

    if not verdict.quarantine_recommended:
        return redacted

    logger.warning(
        "context_scan: prompt-injection signature detected in %s "
        "(rules=%s, conf=%.2f)",
        source,
        verdict.triggered_rules,
        verdict.confidence,
    )
    warning_line = (
        f"<!-- workspace-context-injection-warning source={source} "
        f"rules={','.join(verdict.triggered_rules)} "
        f"confidence={verdict.confidence:.2f} -->"
    )
    return (
        f"{warning_line}\n"
        "<quarantined-untrusted-content>\n"
        f"{redacted}\n"
        "</quarantined-untrusted-content>\n"
    )


__all__ = ["scan_workspace_context_content"]
