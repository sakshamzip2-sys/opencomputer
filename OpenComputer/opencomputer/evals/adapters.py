"""Adapters: wrap each call site as a single-dict-in / structured-out function.

Adapters live HERE (not in core modules) to preserve the rule:
evals imports from core, never the reverse.

Each adapter signature: adapter_<site>(case_input: dict) -> Any

NOTE: Each adapter imports its production target lazily inside the function.
This means importing this module does NOT pull in the entire opencomputer
core graph — adapters fail at call time with a clear error if the target
module's eval shim isn't yet defined (Task 1.6).
"""

from __future__ import annotations

from typing import Any


def adapter_reflect(case_input: dict[str, Any]) -> str:
    """Wrap opencomputer.evolution.reflect for evaluation.

    case_input shape: {"events": [<TrajectoryEvent dict>, ...]}
    Returns: joined Insight texts for the rubric grader.

    Legacy "session_excerpt" shape is rejected — TrajectoryRecord's
    privacy contract requires structured events, not free text.
    """
    from opencomputer.evolution.reflect import reflect_for_eval

    if "events" not in case_input:
        raise KeyError(
            "reflect adapter requires 'events' key — "
            "legacy 'session_excerpt' shape is no longer supported"
        )
    return reflect_for_eval(case_input["events"])


def adapter_llm_extractor(case_input: dict[str, Any]) -> dict[str, Any]:
    """Wrap opencomputer.profile_bootstrap.llm_extractor for evaluation.

    case_input shape: {"text": str}
    Returns: extracted artifact fields as a dict.
    """
    from opencomputer.profile_bootstrap.llm_extractor import extract_for_eval

    return extract_for_eval(case_input["text"])


def adapter_job_change(case_input: dict[str, Any]) -> str:
    """Wrap opencomputer.awareness.life_events.job_change for evaluation.

    case_input shape: {"url": str, "title": str}
    Returns: "yes" if the job-change regex classifier fires, "no" otherwise.
    """
    from opencomputer.awareness.life_events.job_change import detect_for_eval

    return "yes" if detect_for_eval(case_input["url"], case_input["title"]) else "no"


def adapter_instruction_detector(case_input: dict[str, Any]) -> str:
    """Wrap opencomputer.security.instruction_detector for evaluation.

    case_input shape: {"text": str}
    Returns: "yes" or "no".

    Uses the existing detect() API; no production-side shim needed.
    """
    from opencomputer.security.instruction_detector import (
        InstructionDetector,
        InstructionDetectorConfig,
    )

    detector = InstructionDetector(InstructionDetectorConfig(enabled=True))
    verdict = detector.detect(case_input["text"])
    return "yes" if verdict.is_instruction_like else "no"
