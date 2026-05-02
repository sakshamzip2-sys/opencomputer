"""Voice-mode integration test for effort_policy.

cli_voice constructs RuntimeContext(custom={"voice_mode": True}) so the
effort policy picks ``low`` automatically — voice round-trips are
latency-bound and can't afford a thinking budget.
"""

from __future__ import annotations

import dataclasses

from opencomputer.agent.effort_policy import recommended_effort
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT


def test_voice_mode_runtime_picks_low_on_any_model() -> None:
    """RuntimeContext(custom={'voice_mode': True}) → 'low' regardless of model."""
    runtime = dataclasses.replace(
        DEFAULT_RUNTIME_CONTEXT, custom={"voice_mode": True}
    )

    # Voice on Opus 4.7 (which would otherwise → xhigh)
    assert recommended_effort(runtime=runtime, model="claude-opus-4-7") == "low"
    # Voice on Sonnet 4.6 (which would otherwise → medium)
    assert recommended_effort(runtime=runtime, model="claude-sonnet-4-6") == "low"
    # Voice on a model with no per-model default
    assert recommended_effort(runtime=runtime, model="kimi-k2") == "low"


def test_non_voice_runtime_falls_back_to_per_model_defaults() -> None:
    """A runtime without voice_mode flag still gets per-model defaults."""
    runtime = DEFAULT_RUNTIME_CONTEXT  # no voice_mode in custom
    assert recommended_effort(runtime=runtime, model="claude-opus-4-7") == "xhigh"
