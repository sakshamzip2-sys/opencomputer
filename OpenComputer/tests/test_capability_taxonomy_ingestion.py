from opencomputer.agent.consent.capability_taxonomy import F1_CAPABILITIES
from plugin_sdk import ConsentTier


def test_ingestion_capabilities_registered():
    assert F1_CAPABILITIES["ingestion.recent_files"] == ConsentTier.IMPLICIT
    assert F1_CAPABILITIES["ingestion.calendar"] == ConsentTier.EXPLICIT
    assert F1_CAPABILITIES["ingestion.browser_history"] == ConsentTier.EXPLICIT
    assert F1_CAPABILITIES["ingestion.git_log"] == ConsentTier.IMPLICIT
    assert F1_CAPABILITIES["ingestion.messages"] == ConsentTier.EXPLICIT
    assert F1_CAPABILITIES["ingestion.browser_extension"] == ConsentTier.EXPLICIT


def test_ingestion_capabilities_all_present():
    expected = {
        "ingestion.recent_files",
        "ingestion.calendar",
        "ingestion.browser_history",
        "ingestion.git_log",
        "ingestion.messages",
        "ingestion.browser_extension",
    }
    assert expected.issubset(F1_CAPABILITIES.keys())
