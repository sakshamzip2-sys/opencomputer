"""M2 T2.1 — tests for plugin_sdk.user_model.NodeKindValidator.

The validator is the write-boundary guard that keeps agent-internal
machinery (turn_start, tool_call, agent_loop, …) out of the user-model
graph. It is part of the public plugin SDK.
"""
from __future__ import annotations


def test_validator_and_result_importable_from_plugin_sdk() -> None:
    """NodeKindValidator + NodeValidation are part of the public SDK."""
    from plugin_sdk import NodeKindValidator, NodeValidation

    assert NodeKindValidator is not None
    assert NodeValidation is not None


def test_legit_node_passes() -> None:
    """A normal user fact validates cleanly."""
    from plugin_sdk.user_model import NodeKindValidator

    v = NodeKindValidator()
    assert v.check("preference", "tone_preference: terse").valid is True
    assert v.check("goal", "learn Rust by Q3").valid is True
    assert v.check("attribute", "uses Python").valid is True


def test_unknown_kind_is_rejected() -> None:
    """A kind outside the NodeKind taxonomy is invalid."""
    from plugin_sdk.user_model import NodeKindValidator

    result = NodeKindValidator().check("concern", "worried about exams")
    assert result.valid is False
    assert "concern" in result.reason


def test_empty_value_is_rejected() -> None:
    """Empty / whitespace-only values are invalid."""
    from plugin_sdk.user_model import NodeKindValidator

    v = NodeKindValidator()
    assert v.check("attribute", "").valid is False
    assert v.check("attribute", "   ").valid is False


def test_agent_internal_token_is_rejected() -> None:
    """Values embedding agent-internal machinery are invalid, reason names it."""
    from plugin_sdk.user_model import NodeKindValidator

    v = NodeKindValidator()
    r1 = v.check("attribute", "uses agent_loop")
    assert r1.valid is False
    assert "agent_loop" in r1.reason
    assert v.check("attribute", "runs tool_call/Bash").valid is False
    assert v.check("preference", "prefers Wednesday 20:00 for agent_loop").valid is False
    assert v.check("attribute", "runs turn_completed/gateway.dispatch").valid is False


def test_token_match_is_case_insensitive() -> None:
    """Casing does not let agent-internal noise slip through."""
    from plugin_sdk.user_model import NodeKindValidator

    assert NodeKindValidator().check("attribute", "uses AGENT_LOOP").valid is False


def test_innocuous_values_are_not_false_positives() -> None:
    """Ordinary user facts that merely look similar still pass."""
    from plugin_sdk.user_model import NodeKindValidator

    v = NodeKindValidator()
    # 'cron' is deliberately NOT in the denylist (too ambiguous a substring).
    assert v.check("attribute", "manages a cron schedule").valid is True
    assert v.check("identity", "name: Saksham").valid is True


def test_custom_denylist_overrides_default() -> None:
    """A caller can supply its own token set."""
    from plugin_sdk.user_model import NodeKindValidator

    v = NodeKindValidator(agent_internal_tokens=("zzz",))
    assert v.check("attribute", "has zzz inside").valid is False
    # The default tokens no longer apply.
    assert v.check("attribute", "uses agent_loop").valid is True


def test_node_validation_is_frozen() -> None:
    """NodeValidation is an immutable SDK dataclass."""
    import dataclasses

    from plugin_sdk.user_model import NodeValidation

    nv = NodeValidation(valid=True)
    try:
        nv.valid = False  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - frozen guarantee
        raise AssertionError("NodeValidation must be frozen")
