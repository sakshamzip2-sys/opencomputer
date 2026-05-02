"""Pre-write validation for synthesized skills.

Mirrors Hermes evolution/core/constraints.py philosophy: hard gates,
not soft objectives — invalid candidates rejected BEFORE the atomic
write reaches disk. Caller catches `ConstraintViolation` and skips
the insight (never overwrites a previously-validated skill).

Design rationale (PR-5 of 2026-04-25 Hermes parity plan):
- We trust LLM output less than user input. Each constraint is a
  separate `ValueError` subclass-friendly so callers can switch on
  failure type.
- Constants are module-level for easy tuning.
- Path-traversal heuristic is intentionally cheap — defense-in-depth
  on top of SkillSynthesizer._write_safe_named_file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Module-level tunables — bumpable per design decision; mirror Hermes defaults
MAX_SKILL_SIZE_BYTES: int = 15_000      # Hermes uses 15KB cap
# Description length cap. Intentionally stricter than Anthropic's 1024-char
# spec ceiling because OpenComputer routes skills by description similarity;
# long descriptions degrade routing precision. 280 chars is enough for the
# WHAT (action verb phrase) + WHEN ("Use when..." clause) the synthesis
# prompt requires, and aligns with the synthesis prompt's documented cap.
MAX_DESCRIPTION_LEN: int = 280
MIN_BODY_LEN: int = 50                  # too short = no actionable content
MAX_REFERENCE_FILES: int = 10
MAX_EXAMPLE_FILES: int = 10
MAX_REF_FILE_SIZE_BYTES: int = 8_000    # per-reference-file cap
_SLUG_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]{1,49}$")


class ConstraintViolation(ValueError):  # noqa: N818 — "Violation" is domain terminology, not exception suffix
    """Raised when a synthesized skill payload fails pre-write validation.

    Subclass of ValueError so existing code paths that catch
    `(ValueError, FileExistsError)` (e.g. SkillSynthesizer.synthesize
    callers) continue to work."""


def validate_synthesized_skill(payload: Mapping[str, Any]) -> None:
    """Run all pre-write constraints on a synthesized-skill payload.

    Raises:
        ConstraintViolation: on the FIRST violation. Caller catches +
            skips the insight; we don't aggregate failures because the
            payload is already broken — surface the first issue cleanly.

    Constraints checked (in order):
        1. slug matches `^[a-z0-9][a-z0-9-]{1,49}$`
        2. body present and >= MIN_BODY_LEN chars
        3. body utf-8 encoded length <= MAX_SKILL_SIZE_BYTES
        4. description length <= MAX_DESCRIPTION_LEN
        5. references count <= MAX_REFERENCE_FILES; each <= MAX_REF_FILE_SIZE_BYTES
        6. examples count <= MAX_EXAMPLE_FILES; each <= MAX_REF_FILE_SIZE_BYTES
        7. body does not contain '../' or '..\\\\'  (path-traversal heuristic)
        8. name present and non-empty
    """
    slug = str(payload.get("slug", ""))
    if not _SLUG_RE.match(slug):
        raise ConstraintViolation(
            f"slug {slug!r} fails pattern {_SLUG_RE.pattern} "
            "(lowercase alphanumeric + hyphens; first char alphanumeric; 2-50 chars)"
        )

    name = str(payload.get("name", "")).strip()
    if not name:
        raise ConstraintViolation("name is required and must be non-empty")

    body = payload.get("body")
    if not isinstance(body, str):
        raise ConstraintViolation(f"body must be a string, got {type(body).__name__}")
    if len(body) < MIN_BODY_LEN:
        raise ConstraintViolation(
            f"body too short ({len(body)} < {MIN_BODY_LEN} chars) — no actionable content"
        )
    body_bytes = len(body.encode("utf-8"))
    if body_bytes > MAX_SKILL_SIZE_BYTES:
        raise ConstraintViolation(
            f"body exceeds {MAX_SKILL_SIZE_BYTES} bytes ({body_bytes} bytes after utf-8 encode)"
        )

    desc = str(payload.get("description", ""))
    if len(desc) > MAX_DESCRIPTION_LEN:
        raise ConstraintViolation(
            f"description exceeds {MAX_DESCRIPTION_LEN} chars (got {len(desc)})"
        )

    refs = payload.get("references") or []
    if len(refs) > MAX_REFERENCE_FILES:
        raise ConstraintViolation(
            f"too many reference files ({len(refs)} > {MAX_REFERENCE_FILES})"
        )
    for i, ref in enumerate(refs):
        if not isinstance(ref, dict):
            continue  # SkillSynthesizer's _write_safe_named_file handles shape errors
        content = ref.get("content", "")
        if isinstance(content, str) and len(content.encode("utf-8")) > MAX_REF_FILE_SIZE_BYTES:
            raise ConstraintViolation(
                f"reference[{i}] {ref.get('name','?')!r} exceeds {MAX_REF_FILE_SIZE_BYTES} bytes"
            )

    exs = payload.get("examples") or []
    if len(exs) > MAX_EXAMPLE_FILES:
        raise ConstraintViolation(
            f"too many example files ({len(exs)} > {MAX_EXAMPLE_FILES})"
        )
    for i, ex in enumerate(exs):
        if not isinstance(ex, dict):
            continue
        content = ex.get("content", "")
        if isinstance(content, str) and len(content.encode("utf-8")) > MAX_REF_FILE_SIZE_BYTES:
            raise ConstraintViolation(
                f"example[{i}] {ex.get('name','?')!r} exceeds {MAX_REF_FILE_SIZE_BYTES} bytes"
            )

    # Cheap path-traversal heuristic — defense in depth on top of
    # SkillSynthesizer._write_safe_named_file (which guards filenames,
    # not body content).
    if "../" in body or "..\\" in body:
        raise ConstraintViolation(
            "body contains '../' or '..\\\\' — possible path-traversal injection"
        )


__all__ = [
    "ConstraintViolation",
    "MAX_SKILL_SIZE_BYTES",
    "MAX_DESCRIPTION_LEN",
    "MIN_BODY_LEN",
    "MAX_REFERENCE_FILES",
    "MAX_EXAMPLE_FILES",
    "MAX_REF_FILE_SIZE_BYTES",
    "validate_synthesized_skill",
]
