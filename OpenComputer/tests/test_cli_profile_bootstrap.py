"""Layered Awareness MVP — bootstrap CLI tests.

Covers the install-time flow + the E2E integration test verifying
bootstrap → graph → prompt round-trip.
"""
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli_profile import profile_app

runner = CliRunner()


def test_bootstrap_skip_runs_layers_0_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    with patch(
        "opencomputer.cli_profile.run_bootstrap",
    ) as m:
        m.return_value.__class__.__name__ = "BootstrapResult"
        m.return_value.identity_nodes_written = 1
        m.return_value.interview_nodes_written = 0
        m.return_value.files_scanned = 0
        m.return_value.git_commits_scanned = 0
        m.return_value.elapsed_seconds = 0.1
        result = runner.invoke(profile_app, ["bootstrap", "--skip-interview"])
    assert result.exit_code == 0
    assert "Identity" in result.stdout
    assert m.called


def test_bootstrap_already_complete_short_circuits(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    marker = tmp_path / "profile_bootstrap" / "complete.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    result = runner.invoke(profile_app, ["bootstrap"])
    assert result.exit_code == 0
    assert "already complete" in result.stdout.lower()


def test_bootstrap_force_reruns(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    marker = tmp_path / "profile_bootstrap" / "complete.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    with patch("opencomputer.cli_profile.run_bootstrap") as m:
        m.return_value.identity_nodes_written = 1
        m.return_value.interview_nodes_written = 0
        m.return_value.files_scanned = 0
        m.return_value.git_commits_scanned = 0
        m.return_value.elapsed_seconds = 0.1
        result = runner.invoke(
            profile_app, ["bootstrap", "--skip-interview", "--force"]
        )
    assert result.exit_code == 0
    assert m.called


def test_bootstrap_cli_displays_calendar_browser_counters(
    tmp_path: Path, monkeypatch,
):
    """V2.A-T5 — CLI must surface the calendar + browser counters."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    with patch("opencomputer.cli_profile.run_bootstrap") as m:
        m.return_value.identity_nodes_written = 0
        m.return_value.interview_nodes_written = 0
        m.return_value.files_scanned = 0
        m.return_value.git_commits_scanned = 0
        m.return_value.calendar_events_scanned = 5
        m.return_value.browser_visits_scanned = 12
        m.return_value.elapsed_seconds = 0.1
        result = runner.invoke(profile_app, ["bootstrap", "--skip-interview"])
    assert result.exit_code == 0
    assert "Calendar events scanned" in result.stdout
    assert "5" in result.stdout
    assert "Browser visits scanned" in result.stdout
    assert "12" in result.stdout


def test_bootstrap_then_prompt_includes_user_facts(tmp_path: Path, monkeypatch):
    """E2E: bootstrap → graph populated → prompt builder injects facts."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # Run the real orchestrator (no mock) with a tightly-scoped scope.
    from opencomputer.agent.prompt_builder import PromptBuilder
    from opencomputer.profile_bootstrap.orchestrator import run_bootstrap
    from opencomputer.user_model.store import UserModelStore

    graph_path = tmp_path / "user_model" / "graph.sqlite"
    graph_path.parent.mkdir(parents=True)
    store = UserModelStore(graph_path)

    result = run_bootstrap(
        interview_answers={
            "current_focus": "Shipping Layered Awareness MVP",
            "tone_preference": "concise",
        },
        scan_roots=[],
        git_repos=[],
        include_calendar=False,
        include_browser_history=False,
        store=store,
        marker_path=tmp_path / "complete.json",
    )
    assert result.interview_nodes_written == 2

    pb = PromptBuilder()
    facts_block = pb.build_user_facts(store=store)
    rendered = pb.build(user_facts=facts_block)
    assert "Layered Awareness MVP" in rendered
    assert "concise" in rendered


def test_loop_wires_user_facts_into_build_with_memory():
    """Regression for C4: ``AgentLoop`` must call ``build_user_facts`` and
    pass the result to ``build_with_memory``. A grep-style structural check
    is the cheapest defensible test — full loop integration would pull in
    the provider stack. If this fails, the new "What I know about you"
    block is permanently empty in production despite the slot being
    plumbed through ``PromptBuilder``.
    """
    import inspect

    from opencomputer.agent import loop as loop_module

    src = inspect.getsource(loop_module)
    # Both must appear: the call to build_user_facts AND the
    # forward of the result as the user_facts kwarg of
    # build_with_memory. Order matters (former precedes latter).
    bf_idx = src.find("build_user_facts(")
    bm_idx = src.find("user_facts=user_facts")
    bwm_idx = src.find("build_with_memory(")
    assert bf_idx != -1, (
        "AgentLoop must call build_user_facts() so the "
        "production prompt picks up F4 user-model nodes."
    )
    assert bm_idx != -1, (
        "AgentLoop must forward the computed user_facts into "
        "build_with_memory(user_facts=...) — the slot is otherwise empty."
    )
    assert bwm_idx != -1
    assert bf_idx < bwm_idx, (
        "build_user_facts() must be called BEFORE build_with_memory(); "
        "otherwise the forwarded value is stale or undefined."
    )
