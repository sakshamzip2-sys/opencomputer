"""F1: Public SDK types for consent layer."""
import dataclasses

import pytest

from plugin_sdk import (
    CapabilityClaim,
    ConsentDecision,
    ConsentGrant,
    ConsentTier,
)


def test_consent_tier_is_ordered_enum():
    assert ConsentTier.IMPLICIT.value == 0
    assert ConsentTier.EXPLICIT.value == 1
    assert ConsentTier.PER_ACTION.value == 2
    assert ConsentTier.DELEGATED.value == 3
    assert ConsentTier.IMPLICIT < ConsentTier.EXPLICIT
    assert ConsentTier.PER_ACTION < ConsentTier.DELEGATED


def test_capability_claim_is_frozen_dataclass():
    claim = CapabilityClaim(
        capability_id="read_files",
        tier_required=ConsentTier.EXPLICIT,
        human_description="Read file contents",
        data_scope="/Users/saksham/Projects",
    )
    assert claim.capability_id == "read_files"
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.capability_id = "mutated"  # type: ignore[misc]


def test_capability_claim_default_scope_is_none():
    claim = CapabilityClaim(
        capability_id="x",
        tier_required=ConsentTier.IMPLICIT,
        human_description="x",
    )
    assert claim.data_scope is None


def test_consent_grant_fields():
    grant = ConsentGrant(
        capability_id="read_files",
        tier=ConsentTier.EXPLICIT,
        scope_filter="/Users/saksham/Projects",
        granted_at=1000.0,
        expires_at=2000.0,
        granted_by="user",
    )
    assert grant.granted_by == "user"
    assert grant.expires_at == 2000.0


def test_consent_grant_is_frozen():
    grant = ConsentGrant(
        capability_id="x", tier=ConsentTier.IMPLICIT, scope_filter=None,
        granted_at=0.0, expires_at=None, granted_by="user",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        grant.granted_by = "auto"  # type: ignore[misc]


def test_consent_decision_fields():
    decision = ConsentDecision(
        allowed=False,
        reason="no grant",
        tier_matched=None,
        audit_event_id=42,
    )
    assert decision.allowed is False
    assert decision.audit_event_id == 42


def test_consent_decision_is_frozen():
    decision = ConsentDecision(allowed=True, reason="", tier_matched=ConsentTier.EXPLICIT, audit_event_id=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.allowed = False  # type: ignore[misc]
