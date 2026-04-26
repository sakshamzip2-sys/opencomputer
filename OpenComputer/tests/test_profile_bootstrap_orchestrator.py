"""Layered Awareness MVP — orchestrator unit tests.

Mocks ``gather_identity`` so tests don't depend on the host's git config
or Contacts.app state. Real Layer 0 integration is exercised via the
E2E test in Task 13.
"""
from pathlib import Path
from unittest.mock import patch

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
