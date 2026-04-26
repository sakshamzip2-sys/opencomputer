"""Layered Awareness MVP — orchestrator unit tests.

Mocks ``gather_identity`` so tests don't depend on the host's git config
or Contacts.app state. Real Layer 0 integration is exercised via the
E2E test in Task 13.

V2.A-T1 — adds coverage for F1 consent enforcement on each Layer 2
ingestion site. Tests use ``patch("..._get_consent_gate")`` so they
don't need a real SQLite + keyring; the helper's None-fallback keeps
the legacy tests (which don't set up F1 at all) passing unchanged.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.profile_bootstrap.identity_reflex import IdentityFacts
from opencomputer.profile_bootstrap.orchestrator import (
    BootstrapResult,
    run_bootstrap,
)
from opencomputer.user_model.store import UserModelStore


@pytest.fixture
def store(tmp_path: Path) -> UserModelStore:
    return UserModelStore(tmp_path / "graph.sqlite")


def test_bootstrap_runs_layers_in_order_and_returns_result(store):
    fake_facts = IdentityFacts(name="Saksham", emails=("s@e.com",))
    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=fake_facts,
    ):
        result = run_bootstrap(
            interview_answers={
                "current_focus": "OpenComputer v1.0",
                "tone_preference": "concise",
            },
            scan_roots=[],
            git_repos=[],
            include_calendar=False,
            include_browser_history=False,
            store=store,
        )
    assert isinstance(result, BootstrapResult)
    assert result.identity_nodes_written >= 1
    assert result.interview_nodes_written == 2


def test_bootstrap_marks_complete(store, tmp_path: Path):
    marker = tmp_path / "bootstrap_complete.json"
    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ):
        run_bootstrap(
            interview_answers={},
            scan_roots=[],
            git_repos=[],
            include_calendar=False,
            include_browser_history=False,
            store=store,
            marker_path=marker,
        )
    assert marker.exists()


# ─── V2.A-T1 — F1 consent enforcement on Layer 2 readers ─────────────


def _selective_gate(allow: dict[str, bool]) -> MagicMock:
    """Build a fake gate whose ``check`` honors ``allow[capability_id]``.

    Default for an unspecified capability is True (mirrors the
    open-by-default semantics of a freshly granted profile, while
    keeping tests focused on the deny path under test).
    """
    fake = MagicMock()

    def check(claim, *, scope=None, session_id=None):  # noqa: ARG001
        decision = MagicMock()
        decision.allowed = allow.get(claim.capability_id, True)
        return decision

    fake.check.side_effect = check
    return fake


def test_bootstrap_skips_calendar_when_consent_revoked(
    store, tmp_path, monkeypatch,
):
    """Revoking ingestion.calendar must skip the calendar reader path."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    fake_gate = _selective_gate({"ingestion.calendar": False})

    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator._get_consent_gate",
        return_value=fake_gate,
    ), patch(
        "opencomputer.profile_bootstrap.calendar_reader.read_upcoming_events",
    ) as mock_calendar:
        run_bootstrap(
            interview_answers={},
            scan_roots=[],
            git_repos=[],
            include_calendar=True,
            include_browser_history=False,
            store=store,
        )

    mock_calendar.assert_not_called()


def test_bootstrap_runs_calendar_when_consent_granted(
    store, tmp_path, monkeypatch,
):
    """Inverse: with consent granted, calendar reader IS called."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    fake_gate = _selective_gate({})  # all caps allowed

    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator._get_consent_gate",
        return_value=fake_gate,
    ), patch(
        "opencomputer.profile_bootstrap.calendar_reader.read_upcoming_events",
        return_value=[],
    ) as mock_calendar:
        run_bootstrap(
            interview_answers={},
            scan_roots=[],
            git_repos=[],
            include_calendar=True,
            include_browser_history=False,
            store=store,
        )

    mock_calendar.assert_called_once()


def test_bootstrap_skips_browser_history_when_consent_revoked(
    store, tmp_path, monkeypatch,
):
    """Revoking ingestion.browser_history must skip the chrome reader."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    fake_gate = _selective_gate({"ingestion.browser_history": False})

    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator._get_consent_gate",
        return_value=fake_gate,
    ), patch(
        "opencomputer.profile_bootstrap.browser_history.read_all_browser_history",
    ) as mock_history:
        run_bootstrap(
            interview_answers={},
            scan_roots=[],
            git_repos=[],
            include_calendar=False,
            include_browser_history=True,
            store=store,
        )

    mock_history.assert_not_called()


def test_bootstrap_skips_recent_files_when_consent_revoked(
    store, tmp_path, monkeypatch,
):
    """Revoking ingestion.recent_files must skip scan_recent_files."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    fake_gate = _selective_gate({"ingestion.recent_files": False})

    # Need a non-empty scan_roots so the include flag doesn't short-
    # circuit before the gate check.
    fake_root = tmp_path / "code"
    fake_root.mkdir()

    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator._get_consent_gate",
        return_value=fake_gate,
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator.scan_recent_files",
    ) as mock_scan:
        run_bootstrap(
            interview_answers={},
            scan_roots=[fake_root],
            git_repos=[],
            include_calendar=False,
            include_browser_history=False,
            store=store,
        )

    mock_scan.assert_not_called()


def test_bootstrap_skips_git_log_when_consent_revoked(
    store, tmp_path, monkeypatch,
):
    """Revoking ingestion.git_log must skip scan_git_log."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    fake_gate = _selective_gate({"ingestion.git_log": False})

    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / ".git").mkdir()

    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator._get_consent_gate",
        return_value=fake_gate,
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator.scan_git_log",
    ) as mock_git:
        run_bootstrap(
            interview_answers={},
            scan_roots=[],
            git_repos=[fake_repo],
            include_calendar=False,
            include_browser_history=False,
            store=store,
        )

    mock_git.assert_not_called()


def test_bootstrap_runs_recent_files_and_git_when_consent_granted(
    store, tmp_path, monkeypatch,
):
    """Inverse of the recent_files / git_log deny tests — both run when allowed."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    fake_gate = _selective_gate({})  # all caps allowed

    fake_root = tmp_path / "code"
    fake_root.mkdir()
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / ".git").mkdir()

    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator._get_consent_gate",
        return_value=fake_gate,
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator.scan_recent_files",
        return_value=[],
    ) as mock_scan, patch(
        "opencomputer.profile_bootstrap.orchestrator.scan_git_log",
        return_value=[],
    ) as mock_git:
        run_bootstrap(
            interview_answers={},
            scan_roots=[fake_root],
            git_repos=[fake_repo],
            include_calendar=False,
            include_browser_history=False,
            store=store,
        )

    mock_scan.assert_called_once()
    mock_git.assert_called_once()


def test_bootstrap_no_gate_falls_back_to_open(store, tmp_path, monkeypatch):
    """When _get_consent_gate returns None, every reader runs (legacy parity).

    This is the deliberate open-by-default fallback for first-run
    profiles that have not yet seen the consent CLI; without it the
    bootstrap would silently skip all Layer 2 readers on a brand-new
    install and the agent would have nothing to learn from.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    fake_root = tmp_path / "code"
    fake_root.mkdir()

    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator._get_consent_gate",
        return_value=None,
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator.scan_recent_files",
        return_value=[],
    ) as mock_scan, patch(
        "opencomputer.profile_bootstrap.calendar_reader.read_upcoming_events",
        return_value=[],
    ) as mock_calendar:
        run_bootstrap(
            interview_answers={},
            scan_roots=[fake_root],
            git_repos=[],
            include_calendar=True,
            include_browser_history=False,
            store=store,
        )

    mock_scan.assert_called_once()
    mock_calendar.assert_called_once()


def test_bootstrap_treats_gate_exception_as_denied(
    store, tmp_path, monkeypatch,
):
    """A gate that raises is fail-closed: the reader must not run."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    fake_gate = MagicMock()
    fake_gate.check.side_effect = RuntimeError("gate exploded")

    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator._get_consent_gate",
        return_value=fake_gate,
    ), patch(
        "opencomputer.profile_bootstrap.calendar_reader.read_upcoming_events",
    ) as mock_calendar:
        run_bootstrap(
            interview_answers={},
            scan_roots=[],
            git_repos=[],
            include_calendar=True,
            include_browser_history=False,
            store=store,
        )

    mock_calendar.assert_not_called()


# ─── V2.A-T5 — calendar + browser visit counters ─────────────────────


def test_bootstrap_counts_calendar_events(store, tmp_path, monkeypatch):
    """When calendar consent is granted, the count surfaces in the result."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.profile_bootstrap.calendar_reader import (
        CalendarEventSummary,
    )

    fake_events = [CalendarEventSummary(title=f"event{i}") for i in range(5)]

    fake_gate = _selective_gate({})  # all caps allowed

    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator._get_consent_gate",
        return_value=fake_gate,
    ), patch(
        "opencomputer.profile_bootstrap.calendar_reader.read_upcoming_events",
        return_value=fake_events,
    ):
        result = run_bootstrap(
            interview_answers={},
            scan_roots=[],
            git_repos=[],
            include_calendar=True,
            include_browser_history=False,
            store=store,
        )
    assert result.calendar_events_scanned == 5
    assert result.browser_visits_scanned == 0


def test_bootstrap_counts_browser_visits(store, tmp_path, monkeypatch):
    """When browser-history consent is granted, the count surfaces in the result."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.profile_bootstrap.browser_history import (
        BrowserVisitSummary,
    )

    fake_visits = [BrowserVisitSummary(url=f"https://e{i}.com") for i in range(3)]

    fake_gate = _selective_gate({})  # all caps allowed

    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator._get_consent_gate",
        return_value=fake_gate,
    ), patch(
        "opencomputer.profile_bootstrap.browser_history.read_all_browser_history",
        return_value=fake_visits,
    ):
        result = run_bootstrap(
            interview_answers={},
            scan_roots=[],
            git_repos=[],
            include_calendar=False,
            include_browser_history=True,
            store=store,
        )
    assert result.browser_visits_scanned == 3
    assert result.calendar_events_scanned == 0


def test_bootstrap_zero_counters_when_consent_denied(
    store, tmp_path, monkeypatch,
):
    """Consent revocation must zero the counters (no reads happened)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    fake_gate = _selective_gate(
        {"ingestion.calendar": False, "ingestion.browser_history": False}
    )

    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ), patch(
        "opencomputer.profile_bootstrap.orchestrator._get_consent_gate",
        return_value=fake_gate,
    ):
        result = run_bootstrap(
            interview_answers={},
            scan_roots=[],
            git_repos=[],
            include_calendar=True,
            include_browser_history=True,
            store=store,
        )
    assert result.calendar_events_scanned == 0
    assert result.browser_visits_scanned == 0


# ─── V2.B-T7 — extract_and_emit_motif helper ─────────────────────────


def test_extract_and_emit_motif_returns_false_when_ollama_unavailable():
    from unittest.mock import MagicMock, patch
    from opencomputer.profile_bootstrap.llm_extractor import OllamaUnavailable
    from opencomputer.profile_bootstrap.orchestrator import extract_and_emit_motif

    bus = MagicMock()
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.extract_artifact",
        side_effect=OllamaUnavailable("test"),
    ):
        emitted = extract_and_emit_motif(
            content="x", kind="file", source_path="/a", bus=bus,
        )
    assert emitted is False
    bus.publish.assert_not_called()


def test_extract_and_emit_motif_publishes_when_extraction_nonempty():
    from unittest.mock import MagicMock, patch
    from opencomputer.profile_bootstrap.llm_extractor import ArtifactExtraction
    from opencomputer.profile_bootstrap.orchestrator import extract_and_emit_motif

    bus = MagicMock()
    fake = ArtifactExtraction(topic="stocks", sentiment="neutral")
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.extract_artifact",
        return_value=fake,
    ):
        emitted = extract_and_emit_motif(
            content="x", kind="file", source_path="/a", bus=bus,
        )
    assert emitted is True
    bus.publish.assert_called_once()
    event = bus.publish.call_args[0][0]
    assert event.event_type == "layered_awareness.artifact_extraction"
    assert event.metadata["topic"] == "stocks"


def test_extract_and_emit_motif_returns_false_when_extraction_blank():
    from unittest.mock import MagicMock, patch
    from opencomputer.profile_bootstrap.llm_extractor import ArtifactExtraction
    from opencomputer.profile_bootstrap.orchestrator import extract_and_emit_motif

    bus = MagicMock()
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.extract_artifact",
        return_value=ArtifactExtraction(),
    ):
        emitted = extract_and_emit_motif(
            content="x", kind="file", source_path="/a", bus=bus,
        )
    assert emitted is False
    bus.publish.assert_not_called()
