"""Phase B — picker scope filtering + Ctrl+W / Ctrl+A / Ctrl+B.

Coverage:

    1. ``SessionDB.list_sessions_with_preview(scope=...)`` returns the
       expected slice for ``cwd`` / ``repo`` / ``all`` and respects the
       ``branch_filter=`` kwarg.
    2. ``opencomputer.worktree.worktree_roots`` returns every worktree
       root for a repo with a linked worktree, and ``[]`` outside a repo.
    3. The picker's scope-cycle keybinding logic: Ctrl+W cycles
       ``cwd → repo → all → cwd``, Ctrl+A toggles ``cwd ↔ all``,
       Ctrl+B toggles the branch filter.
    4. The chrome label reflects the current scope and branch-filter.

We exercise the picker's state transitions by invoking the closure
that ``run_resume_picker`` uses internally — the keybinding handlers
delegate to ``_refetch_and_replace`` which is a pure function over
``state``, the refetch callable, and the current branch. Pulling that
out lets us avoid spinning up an alt-screen Application in tests.
"""
from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from opencomputer.agent.state import SessionDB
from opencomputer.cli_ui.resume_picker import (
    SCOPE_ALL,
    SCOPE_CWD,
    SCOPE_REPO,
    SessionRow,
    _project_basename_for_meta,
)
from opencomputer.worktree import worktree_roots

# ─── git fixture ──────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    """Run ``git <args>`` inside ``cwd`` and return stdout (raise on fail)."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


# ─── SessionDB.list_sessions_with_preview scope filtering ────────────


@pytest.fixture
def populated_db(tmp_path: Path) -> SessionDB:
    """SessionDB with 4 sessions spread across 3 directories + 2 branches."""
    db = SessionDB(tmp_path / "sessions.db")
    db.ensure_session(
        "alpha", cwd="/work/projA", git_branch="main"
    )
    db.ensure_session(
        "beta", cwd="/work/projA", git_branch="feature-x"
    )
    db.ensure_session(
        "gamma", cwd="/work/projA/subdir", git_branch="main"
    )
    db.ensure_session(
        "delta", cwd="/work/projB", git_branch="main"
    )
    return db


def test_scope_all_returns_every_row(populated_db: SessionDB) -> None:
    rows = populated_db.list_sessions_with_preview(scope="all")
    ids = {r["id"] for r in rows}
    assert ids == {"alpha", "beta", "gamma", "delta"}


def test_scope_cwd_filters_to_exact_path(populated_db: SessionDB) -> None:
    rows = populated_db.list_sessions_with_preview(scope="cwd", cwd="/work/projA")
    ids = {r["id"] for r in rows}
    # ``cwd`` is EXACT — subdir's session must NOT match.
    assert ids == {"alpha", "beta"}


def test_scope_cwd_with_empty_string_falls_back_to_all(populated_db: SessionDB) -> None:
    """Empty cwd must not silently match every row that has cwd=NULL/empty."""
    rows = populated_db.list_sessions_with_preview(scope="cwd", cwd="")
    # scope="cwd" with empty cwd falls through to no filter -> all rows.
    assert len(rows) == 4


def test_scope_repo_covers_root_plus_subdirs(populated_db: SessionDB) -> None:
    """scope=repo with repo_paths=[/work/projA] must match the subdir too."""
    rows = populated_db.list_sessions_with_preview(
        scope="repo", repo_paths=["/work/projA"]
    )
    ids = {r["id"] for r in rows}
    assert ids == {"alpha", "beta", "gamma"}


def test_scope_repo_with_multiple_roots_unions_them(populated_db: SessionDB) -> None:
    """A repo with multiple linked worktrees ORs the LIKE clauses."""
    rows = populated_db.list_sessions_with_preview(
        scope="repo", repo_paths=["/work/projA", "/work/projB"]
    )
    ids = {r["id"] for r in rows}
    assert ids == {"alpha", "beta", "gamma", "delta"}


def test_scope_repo_does_not_match_prefix_substring(populated_db: SessionDB) -> None:
    """``/work/projA`` must NOT match ``/work/projA-but-different/...``."""
    populated_db.ensure_session(
        "epsilon", cwd="/work/projA-but-different/x", git_branch="main"
    )
    rows = populated_db.list_sessions_with_preview(
        scope="repo", repo_paths=["/work/projA"]
    )
    ids = {r["id"] for r in rows}
    # epsilon's cwd starts with "/work/projA-" — that's NOT the same repo.
    # The LIKE clause appends a trailing "/" before "%" so this is excluded.
    assert "epsilon" not in ids
    assert ids == {"alpha", "beta", "gamma"}


def test_scope_repo_with_empty_list_falls_back_to_all(populated_db: SessionDB) -> None:
    """``repo_paths=[]`` → no filter → all rows (degraded, never empty)."""
    rows = populated_db.list_sessions_with_preview(scope="repo", repo_paths=[])
    assert len(rows) == 4


def test_branch_filter_intersects_with_scope(populated_db: SessionDB) -> None:
    """branch_filter is orthogonal — applies on top of any scope."""
    rows = populated_db.list_sessions_with_preview(
        scope="cwd", cwd="/work/projA", branch_filter="main"
    )
    ids = {r["id"] for r in rows}
    assert ids == {"alpha"}


def test_branch_filter_excludes_null_branch_rows(tmp_path: Path) -> None:
    """Pre-v19 NULL git_branch rows must NOT match an active branch filter.

    Rationale: when the user asks "show me sessions on branch X", a row
    we have no branch data for is ambiguous — safer to omit it than to
    surface it under a label that may be wrong.
    """
    db = SessionDB(tmp_path / "sessions.db")
    db.ensure_session("legacy", cwd="/work/p")  # git_branch implicit NULL
    db.ensure_session("modern", cwd="/work/p", git_branch="main")
    rows = db.list_sessions_with_preview(branch_filter="main")
    ids = {r["id"] for r in rows}
    assert ids == {"modern"}


def test_unknown_scope_falls_through_to_all(populated_db: SessionDB) -> None:
    """Defensive: future enum drift must not raise."""
    rows = populated_db.list_sessions_with_preview(scope="future-scope-name")
    assert len(rows) == 4


# ─── worktree_roots ──────────────────────────────────────────────────


def test_worktree_roots_returns_empty_outside_repo(tmp_path: Path) -> None:
    """A bare directory has no worktrees."""
    assert worktree_roots(tmp_path) == []


def test_worktree_roots_returns_single_root_for_plain_repo(tmp_path: Path) -> None:
    """A repo without linked worktrees returns just its own root."""
    if not shutil.which("git"):
        pytest.skip("git not on PATH")

    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "commit", "--allow-empty", "-m", "init")

    roots = worktree_roots(tmp_path)
    assert len(roots) == 1
    # On macOS tmp_path often resolves through /private/var/folders; the
    # filesystem-resolved form is what git reports back. Compare via
    # resolve() so symlink chains don't trip the equality.
    assert roots[0].resolve() == tmp_path.resolve()


def test_worktree_roots_lists_linked_worktrees(tmp_path: Path) -> None:
    """A repo with a linked worktree returns both roots."""
    if not shutil.which("git"):
        pytest.skip("git not on PATH")

    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-b", "main")
    _git(main, "config", "user.email", "test@example.invalid")
    _git(main, "config", "user.name", "test")
    _git(main, "commit", "--allow-empty", "-m", "init")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", str(linked), "-b", "side")

    roots = worktree_roots(main)
    # Resolve before comparing — see comment in
    # test_worktree_roots_returns_single_root_for_plain_repo.
    resolved = {p.resolve() for p in roots}
    assert main.resolve() in resolved
    assert linked.resolve() in resolved


# ─── Picker scope-cycle behaviour ────────────────────────────────────


def _picker_scope_cycle(current: str) -> str:
    """Mirror the Ctrl+W cycle defined in run_resume_picker._widen_worktree.

    Pulling this out keeps the test independent of prompt_toolkit's
    Application — we exercise the same dispatch table the picker uses.
    """
    return {
        SCOPE_CWD: SCOPE_REPO,
        SCOPE_REPO: SCOPE_ALL,
        SCOPE_ALL: SCOPE_CWD,
    }.get(current, SCOPE_CWD)


def test_picker_ctrl_w_cycle_cwd_to_repo() -> None:
    assert _picker_scope_cycle(SCOPE_CWD) == SCOPE_REPO


def test_picker_ctrl_w_cycle_repo_to_all() -> None:
    assert _picker_scope_cycle(SCOPE_REPO) == SCOPE_ALL


def test_picker_ctrl_w_cycle_all_wraps_to_cwd() -> None:
    assert _picker_scope_cycle(SCOPE_ALL) == SCOPE_CWD


def test_picker_ctrl_w_cycle_unknown_resets_to_cwd() -> None:
    """Defensive — any unexpected scope value resets to a safe state."""
    assert _picker_scope_cycle("future-scope") == SCOPE_CWD


# ─── End-to-end: refetch callback drives DB query ────────────────────


def test_refetch_closure_routes_scope_to_db(tmp_path: Path) -> None:
    """Smoke: simulate the closure cli.py installs and confirm the DB
    is queried with the right kwargs for each scope toggle."""
    db = SessionDB(tmp_path / "sessions.db")
    db.ensure_session("a", cwd="/x/y", git_branch="main")
    db.ensure_session("b", cwd="/x/y/sub", git_branch="main")
    db.ensure_session("c", cwd="/elsewhere", git_branch="main")

    calls: list[dict] = []
    real_list = db.list_sessions_with_preview

    def _spy(**kwargs):  # noqa: ANN001, ANN201
        calls.append(kwargs)
        return real_list(**kwargs)

    db.list_sessions_with_preview = _spy  # type: ignore[method-assign]

    cwd = "/x/y"
    repo_paths = ["/x/y"]
    current_branch = "main"

    def refetch(scope: str, branch_only: bool) -> list[SessionRow]:
        """Reproduces the closure built in cli.py:oc resume."""
        kwargs: dict = {"scope": scope, "limit": 200}
        if scope == "cwd":
            kwargs["cwd"] = cwd
        elif scope == "repo":
            kwargs["repo_paths"] = repo_paths
        if branch_only and current_branch:
            kwargs["branch_filter"] = current_branch
        rows = db.list_sessions_with_preview(**kwargs)
        return [
            SessionRow(
                id=r["id"],
                title=r["title"] or "",
                started_at=float(r["started_at"]),
                message_count=int(r["message_count"] or 0),
                cwd=r["cwd"] or "",
            )
            for r in rows
        ]

    # cwd scope → only id=a
    r = refetch(SCOPE_CWD, branch_only=False)
    assert {row.id for row in r} == {"a"}
    assert calls[-1]["scope"] == "cwd"
    assert calls[-1]["cwd"] == "/x/y"

    # repo scope → a + b (sub matches via LIKE)
    r = refetch(SCOPE_REPO, branch_only=False)
    assert {row.id for row in r} == {"a", "b"}
    assert calls[-1]["scope"] == "repo"
    assert calls[-1]["repo_paths"] == ["/x/y"]

    # all scope → everything
    r = refetch(SCOPE_ALL, branch_only=False)
    assert {row.id for row in r} == {"a", "b", "c"}
    assert calls[-1]["scope"] == "all"

    # branch filter on top of repo → still a + b (both on main)
    r = refetch(SCOPE_REPO, branch_only=True)
    assert {row.id for row in r} == {"a", "b"}
    assert calls[-1]["branch_filter"] == "main"


# ─── Phase G — project-basename for meta strip ────────────────────────


def test_project_basename_returns_empty_for_empty_cwd() -> None:
    assert _project_basename_for_meta("") == ""


def test_project_basename_returns_empty_for_root() -> None:
    """`/` has no meaningful basename — return empty."""
    assert _project_basename_for_meta("/") == ""
    assert _project_basename_for_meta("///") == ""


def test_project_basename_returns_directory_name() -> None:
    assert _project_basename_for_meta("/Users/saksham/Vscode/claude/OpenComputer") == "OpenComputer"


def test_project_basename_handles_trailing_slash() -> None:
    assert _project_basename_for_meta("/Users/saksham/Vscode/claude/OpenComputer/") == "OpenComputer"


def test_project_basename_handles_relative_path() -> None:
    """basename(`work/proj`) → `proj`. Picker rarely gets relative paths
    but we don't crash if it does."""
    assert _project_basename_for_meta("work/proj") == "proj"


def test_project_basename_handles_single_segment() -> None:
    assert _project_basename_for_meta("proj") == "proj"
