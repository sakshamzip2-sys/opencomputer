"""Layer 2 → graph writers (2026-04-28).

Pre-2026-04-28: ``run_bootstrap`` scanned recent files / git log /
calendar / browser history, returned counts in ``BootstrapResult``,
and then dropped the data on the floor — only Layer 0 (identity) had
``write_*_to_graph`` functions. The user_model graph stayed at ~3
identity nodes regardless of how much was scanned.

This test suite locks in the four new writers + their orchestrator
wiring. Each writer:

1. Aggregates raw rows into a structured signal (project-root for
   files, domain for browser, repo for git, event title for calendar)
   so individual file paths / URLs never become individual nodes.
2. Maps to ``NodeKind = "attribute"`` since ``NodeKind`` is a closed
   SDK enum and inventing kinds is a breaking change.
3. Persists confidence proportional to evidence count (more
   observations → higher confidence, capped at 0.95 — explicit user
   statements keep the 1.0 lead).
4. Is idempotent via ``UserModelStore.upsert_node``.
"""
from __future__ import annotations

import pytest

from opencomputer.profile_bootstrap.browser_history import BrowserVisitSummary
from opencomputer.profile_bootstrap.calendar_reader import CalendarEventSummary
from opencomputer.profile_bootstrap.persistence import (
    _evidence_to_confidence,
    _normalize_domain,
    _project_root_for_path,
    write_browser_history_to_graph,
    write_calendar_to_graph,
    write_git_log_to_graph,
    write_recent_files_to_graph,
)
from opencomputer.profile_bootstrap.recent_scan import (
    GitCommitSummary,
    RecentFileSummary,
)
from opencomputer.user_model.store import UserModelStore


@pytest.fixture
def store(tmp_path):
    return UserModelStore(tmp_path / "user_model.sqlite")


# ── pure-helper unit tests ───────────────────────────────────────────


def test_evidence_to_confidence_floor_and_ceiling():
    assert _evidence_to_confidence(0) == 0.5
    assert _evidence_to_confidence(1) == 0.5
    # Saturates near 0.95
    assert 0.93 <= _evidence_to_confidence(50) <= 0.95
    assert _evidence_to_confidence(1000) <= 0.95


def test_evidence_to_confidence_monotonic():
    """More observations → never less confident."""
    prev = -1.0
    for n in [1, 2, 5, 10, 20, 50]:
        cur = _evidence_to_confidence(n)
        assert cur >= prev
        prev = cur


def test_project_root_for_path_collapses_two_levels():
    home = "/Users/test"
    sig = _project_root_for_path(
        "/Users/test/Vscode/claude/OpenComputer/foo/bar.py", home=home,
    )
    assert sig == "Vscode/claude/OpenComputer"


def test_project_root_for_path_skips_outside_home():
    assert _project_root_for_path("/tmp/foo/bar.py", home="/Users/test") is None
    assert _project_root_for_path("/etc/passwd", home="/Users/test") is None


def test_project_root_for_path_handles_short_paths():
    # Only one level deep → not a project root, return None
    assert _project_root_for_path("/Users/test/foo.txt", home="/Users/test") is None


def test_normalize_domain_strips_protocol_and_www():
    assert _normalize_domain("https://www.example.com/foo") == "example.com"
    assert _normalize_domain("http://m.example.com/bar") == "example.com"
    assert _normalize_domain("https://api.github.com/repos") == "api.github.com"


def test_normalize_domain_handles_garbage():
    assert _normalize_domain("") is None
    assert _normalize_domain("not-a-url") is None


# ── recent files writer ──────────────────────────────────────────────


def test_write_recent_files_aggregates_by_project_root(store):
    home = "/Users/test"
    files = [
        RecentFileSummary(path=f"/Users/test/Vscode/claude/OpenComputer/f{i}.py",
                          mtime=0.0, size_bytes=100)
        for i in range(20)
    ] + [
        RecentFileSummary(path=f"/Users/test/Documents/notes/n{i}.md",
                          mtime=0.0, size_bytes=100)
        for i in range(3)
    ]
    n = write_recent_files_to_graph(files, home=home, store=store)
    assert n == 2
    nodes = store.list_nodes(kinds=("attribute",))
    values = {nd.value for nd in nodes}
    assert "active_dir: Vscode/claude/OpenComputer" in values
    assert "active_dir: Documents/notes" in values
    # The 20-file dir should outrank the 3-file dir on confidence.
    by_value = {nd.value: nd for nd in nodes}
    assert (
        by_value["active_dir: Vscode/claude/OpenComputer"].confidence
        > by_value["active_dir: Documents/notes"].confidence
    )


def test_write_recent_files_skips_paths_outside_home(store):
    files = [
        RecentFileSummary(path="/tmp/scratch/foo.py", mtime=0.0, size_bytes=10),
        RecentFileSummary(path="/etc/hosts", mtime=0.0, size_bytes=10),
    ]
    n = write_recent_files_to_graph(files, home="/Users/test", store=store)
    assert n == 0


def test_write_recent_files_empty_list(store):
    assert write_recent_files_to_graph([], home="/Users/test", store=store) == 0


def test_write_recent_files_idempotent(store):
    files = [
        RecentFileSummary(path=f"/Users/test/proj/a/f{i}.py",
                          mtime=0.0, size_bytes=100)
        for i in range(5)
    ]
    write_recent_files_to_graph(files, home="/Users/test", store=store)
    n2 = write_recent_files_to_graph(files, home="/Users/test", store=store)
    nodes = store.list_nodes(kinds=("attribute",))
    assert len([n for n in nodes if n.value.startswith("active_dir:")]) == 1


# ── git log writer ───────────────────────────────────────────────────


def test_write_git_log_records_repos_and_authors(store):
    commits = [
        GitCommitSummary(
            repo_path="/r/proj-a", sha=f"sha{i}", timestamp=0.0,
            subject=f"commit {i}", author_email="user@example.com",
        )
        for i in range(15)
    ] + [
        GitCommitSummary(
            repo_path="/r/proj-b", sha="sX", timestamp=0.0,
            subject="commit", author_email="user@example.com",
        )
    ]
    n = write_git_log_to_graph(commits, store=store)
    # 2 repo nodes + 1 author identity node
    assert n == 3
    repos = {nd.value for nd in store.list_nodes(kinds=("attribute",))}
    assert "works_on_repo: /r/proj-a" in repos
    assert "works_on_repo: /r/proj-b" in repos
    identities = {nd.value for nd in store.list_nodes(kinds=("identity",))}
    assert "git_author_email: user@example.com" in identities


def test_write_git_log_empty_commits(store):
    assert write_git_log_to_graph([], store=store) == 0


# ── browser history writer ───────────────────────────────────────────


def test_write_browser_history_aggregates_by_domain(store):
    visits = [
        BrowserVisitSummary(url="https://www.github.com/x", title="", visit_time=0.0,
                            browser="chrome")
        for _ in range(20)
    ] + [
        BrowserVisitSummary(url="https://stackoverflow.com/q/1", title="", visit_time=0.0,
                            browser="chrome")
        for _ in range(8)
    ] + [
        # singleton — should be dropped (below min_visits=2)
        BrowserVisitSummary(url="https://once.example/page", title="", visit_time=0.0,
                            browser="chrome"),
    ]
    n = write_browser_history_to_graph(visits, store=store)
    assert n == 2
    nodes = store.list_nodes(kinds=("attribute",))
    values = {nd.value for nd in nodes}
    assert "frequent_domain: github.com" in values
    assert "frequent_domain: stackoverflow.com" in values
    assert "frequent_domain: once.example" not in values


def test_write_browser_history_empty(store):
    assert write_browser_history_to_graph([], store=store) == 0


# ── calendar writer ──────────────────────────────────────────────────


def test_write_calendar_records_each_event(store):
    events = [
        CalendarEventSummary(title="Team standup", start=1000.0, end=1500.0,
                             location="", calendar_name="Work"),
        CalendarEventSummary(title="Doctor", start=2000.0, end=2500.0,
                             location="Clinic", calendar_name="Personal"),
        # Empty title → skipped (busy block, declined invite)
        CalendarEventSummary(title="", start=3000.0, end=3500.0,
                             location="", calendar_name="Work"),
    ]
    n = write_calendar_to_graph(events, store=store)
    assert n == 2
    values = {nd.value for nd in store.list_nodes(kinds=("attribute",))}
    assert "upcoming: Team standup" in values
    assert "upcoming: Doctor" in values


def test_write_calendar_attaches_metadata(store):
    events = [
        CalendarEventSummary(title="Demo", start=1234.5, end=2345.6,
                             location="", calendar_name="Work"),
    ]
    write_calendar_to_graph(events, store=store)
    nodes = [
        nd for nd in store.list_nodes(kinds=("attribute",))
        if nd.value == "upcoming: Demo"
    ]
    assert len(nodes) == 1
    assert nodes[0].metadata.get("start") == 1234.5
    assert nodes[0].metadata.get("calendar") == "Work"


# ── orchestrator integration ─────────────────────────────────────────


def test_orchestrator_writes_to_graph_for_all_layer_2_sources(tmp_path, monkeypatch):
    """End-to-end: run_bootstrap with mocked Layer 2 readers populates
    the graph and surfaces non-zero counts in BootstrapResult.
    """
    from opencomputer.profile_bootstrap import orchestrator as orch

    fake_files = [
        RecentFileSummary(path=f"/Users/u/proj/x/f{i}.py", mtime=0.0, size_bytes=10)
        for i in range(5)
    ]
    fake_commits = [
        GitCommitSummary(repo_path="/r/x", sha="abc", timestamp=0.0,
                         subject="s", author_email="u@e.com")
    ]
    fake_calendar = [
        CalendarEventSummary(title="standup", start=0.0, end=0.0, location="",
                             calendar_name="Work"),
    ]
    fake_visits = [
        BrowserVisitSummary(url="https://example.com/", title="",
                            visit_time=0.0, browser="chrome")
        for _ in range(5)
    ]

    # Stub the Layer 2 readers so we don't depend on the user's real
    # filesystem / git repos / calendar perms / browser history during
    # the integration check.
    monkeypatch.setattr(orch, "scan_recent_files", lambda **kw: fake_files)
    monkeypatch.setattr(orch, "scan_git_log", lambda **kw: fake_commits)
    monkeypatch.setattr(orch, "_get_consent_gate", lambda: None)  # allow-by-default

    # Patch lazy imports inside run_bootstrap.
    import opencomputer.profile_bootstrap.browser_history as bh
    import opencomputer.profile_bootstrap.calendar_reader as cal
    monkeypatch.setattr(cal, "read_upcoming_events", lambda **kw: fake_calendar)
    monkeypatch.setattr(bh, "read_all_browser_history", lambda **kw: fake_visits)

    # Patch the home-dir lookup the recent_files writer uses so the
    # fake `/Users/u/...` paths register as "inside home".
    monkeypatch.setattr(
        "pathlib.Path.home",
        classmethod(lambda cls: __import__("pathlib").Path("/Users/u")),
    )

    store = UserModelStore(tmp_path / "user_model.sqlite")
    result = orch.run_bootstrap(
        interview_answers={},
        scan_roots=[tmp_path],
        git_repos=[tmp_path],
        include_calendar=True,
        include_browser_history=True,
        store=store,
    )

    assert result.recent_file_nodes_written >= 1
    assert result.git_nodes_written >= 1
    assert result.calendar_nodes_written == 1
    assert result.browser_nodes_written == 1
    # Verify graph state, not just counters.
    all_attrs = {nd.value for nd in store.list_nodes(kinds=("attribute",))}
    assert any(v.startswith("active_dir:") for v in all_attrs)
    assert any(v.startswith("works_on_repo:") for v in all_attrs)
    assert "upcoming: standup" in all_attrs
    assert "frequent_domain: example.com" in all_attrs
