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

    case_input shape: {"session_excerpt": str}
    Returns: the reflection text the model produced.
    """
    from opencomputer.evolution.reflect import reflect_for_eval

    return reflect_for_eval(case_input["session_excerpt"])


def adapter_prompt_evolution(case_input: dict[str, Any]) -> str:
    """Wrap opencomputer.evolution.prompt_evolution for evaluation.

    case_input shape: {"prompt": str, "failure_mode": str}
    Returns: the mutated prompt text.
    """
    from opencomputer.evolution.prompt_evolution import mutate_for_eval

    return mutate_for_eval(case_input["prompt"], case_input["failure_mode"])


def adapter_llm_extractor(case_input: dict[str, Any]) -> dict[str, Any]:
    """Wrap opencomputer.profile_bootstrap.llm_extractor for evaluation.

    case_input shape: {"text": str}
    Returns: extracted profile dict.
    """
    from opencomputer.profile_bootstrap.llm_extractor import extract_for_eval

    return extract_for_eval(case_input["text"])


def adapter_job_change(case_input: dict[str, Any]) -> str:
    """Wrap opencomputer.awareness.life_events.job_change for evaluation.

    case_input shape: {"context": str}
    Returns: "yes" or "no".
    """
    from opencomputer.awareness.life_events.job_change import detect_for_eval

    return "yes" if detect_for_eval(case_input["context"]) else "no"


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
