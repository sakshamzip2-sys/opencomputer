"""Layered Awareness MVP — ingestion.* capability claims in F1 taxonomy.

Verifies all six ingestion source IDs are registered with the correct
ConsentTier values and that no extra ingestion.* entries have been added.
"""
from opencomputer.agent.consent.capability_taxonomy import F1_CAPABILITIES
from plugin_sdk import ConsentTier


def test_ingestion_capabilities_registered():
    # Use `is` (identity) not `==` for enum checks: ConsentTier is IntEnum, so
    # ``0 == ConsentTier.IMPLICIT`` evaluates True even if the dict accidentally
    # holds a bare integer. Identity check catches that class of accident.
    assert F1_CAPABILITIES["ingestion.recent_files"] is ConsentTier.IMPLICIT
    assert F1_CAPABILITIES["ingestion.calendar"] is ConsentTier.EXPLICIT
    assert F1_CAPABILITIES["ingestion.browser_history"] is ConsentTier.EXPLICIT
    assert F1_CAPABILITIES["ingestion.git_log"] is ConsentTier.IMPLICIT
    assert F1_CAPABILITIES["ingestion.messages"] is ConsentTier.EXPLICIT
    assert F1_CAPABILITIES["ingestion.browser_extension"] is ConsentTier.EXPLICIT


def test_ingestion_capabilities_all_present():
    """Catches missing AND accidentally-added ingestion.* entries.

    Complements ``test_ingestion_capabilities_registered`` (which checks
    individual tier values) by guarding the *exhaustive set* — a future
    PR adding ``ingestion.foo`` without updating this test will fail.
    """
    ingestion_keys = {k for k in F1_CAPABILITIES if k.startswith("ingestion.")}
    assert ingestion_keys == {
        "ingestion.recent_files",
        "ingestion.calendar",
        "ingestion.browser_history",
        "ingestion.git_log",
        "ingestion.messages",
        "ingestion.browser_extension",
    }
