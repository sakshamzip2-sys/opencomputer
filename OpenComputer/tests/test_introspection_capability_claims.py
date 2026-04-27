"""tests/test_introspection_capability_claims.py — F1 namespace + uniqueness contract."""
from __future__ import annotations

from extensions.coding_harness.introspection import ALL_TOOLS

from plugin_sdk.consent import ConsentTier


def test_all_tools_declare_introspection_namespace():
    """Every introspection tool declares exactly one CapabilityClaim under the
    `introspection.*` namespace at IMPLICIT tier."""
    for cls in ALL_TOOLS:
        claims = getattr(cls, "capability_claims", ())
        assert len(claims) == 1, f"{cls.__name__} must declare exactly one capability claim"
        cid = claims[0].capability_id
        assert cid.startswith("introspection."), (
            f"{cls.__name__} claim {cid!r} must use 'introspection.*' namespace"
        )
        assert claims[0].tier_required == ConsentTier.IMPLICIT, (
            f"{cls.__name__} should remain IMPLICIT (parity with prior oi_bridge)"
        )


def test_all_tool_names_unique():
    names = set()
    for cls in ALL_TOOLS:
        tool = cls()
        names.add(tool.schema.name)
    assert len(names) == 5, f"Expected 5 unique tool names; got {len(names)}: {names}"


def test_all_capability_ids_unique():
    cids = set()
    for cls in ALL_TOOLS:
        for claim in cls.capability_claims:
            cids.add(claim.capability_id)
    assert len(cids) == 5, f"Expected 5 unique capability IDs; got {len(cids)}: {cids}"


def test_no_oi_bridge_namespace_remains_in_introspection_tools():
    """Sanity guard: in case someone forgets to rename a claim during refactor."""
    for cls in ALL_TOOLS:
        for claim in cls.capability_claims:
            assert "oi_bridge" not in claim.capability_id, (
                f"{cls.__name__} still has oi_bridge prefix: {claim.capability_id}"
            )
