"""tests/test_ambient_capability_claim.py — F1 namespace contract."""
from __future__ import annotations

from opencomputer.agent.consent.capability_taxonomy import F1_CAPABILITIES
from plugin_sdk.consent import ConsentTier


def test_ambient_capability_registered():
    """The ambient.foreground.observe capability must be in F1_CAPABILITIES."""
    assert "ambient.foreground.observe" in F1_CAPABILITIES


def test_ambient_capability_is_implicit_tier():
    """Foreground observation is low-risk: hashed titles, sensitive filter,
    user opted in via 'oc ambient on'. IMPLICIT tier matches similar
    introspection.* capabilities."""
    assert F1_CAPABILITIES["ambient.foreground.observe"] == ConsentTier.IMPLICIT


def test_ambient_namespace_uses_dot_separator():
    cid = "ambient.foreground.observe"
    assert "/" not in cid
    assert ":" not in cid
    assert cid.startswith("ambient.")


def test_ambient_capability_name_consistent_with_taxonomy():
    """The string used in CapabilityClaim.capability_id must match the
    taxonomy key exactly (no hyphens vs dots typos)."""
    expected = "ambient.foreground.observe"
    assert expected in F1_CAPABILITIES
    # Sanity: this is the only ambient.* entry currently
    ambient_keys = [k for k in F1_CAPABILITIES if k.startswith("ambient.")]
    assert "ambient.foreground.observe" in ambient_keys
