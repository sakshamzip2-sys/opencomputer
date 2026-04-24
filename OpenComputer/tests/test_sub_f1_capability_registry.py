"""CapabilityRegistry — runtime map of plugin-declared claims."""
from opencomputer.agent.consent.capability_registry import CapabilityRegistry
from plugin_sdk import CapabilityClaim, ConsentTier


def test_register_and_lookup():
    reg = CapabilityRegistry()
    claim = CapabilityClaim(
        capability_id="read_files", tier_required=ConsentTier.EXPLICIT,
        human_description="Read files", data_scope=None,
    )
    reg.register("myplugin", "MyReadTool", [claim])
    got = reg.claims_for_tool("MyReadTool")
    assert got == [claim]


def test_claims_for_unknown_tool():
    reg = CapabilityRegistry()
    assert reg.claims_for_tool("Ghost") == []


def test_deduplicates_identical_claims():
    reg = CapabilityRegistry()
    c = CapabilityClaim("x", ConsentTier.EXPLICIT, "x", None)
    reg.register("p1", "T", [c])
    reg.register("p1", "T", [c])  # re-register
    assert len(reg.claims_for_tool("T")) == 1


def test_register_multiple_claims_per_tool():
    reg = CapabilityRegistry()
    c1 = CapabilityClaim("x", ConsentTier.EXPLICIT, "x", None)
    c2 = CapabilityClaim("y", ConsentTier.PER_ACTION, "y", None)
    reg.register("p1", "T", [c1, c2])
    assert reg.claims_for_tool("T") == [c1, c2]
