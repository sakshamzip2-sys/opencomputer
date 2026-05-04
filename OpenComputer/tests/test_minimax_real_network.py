"""Real-network smoke test for MiniMaxSource (Wave 6.E.5).

Skipped by default — set ``OC_TEST_NETWORK=1`` to opt in. The fixture
test in ``tests/test_skill_source_minimax.py`` covers the parsing
contract; this file proves the upstream repo is actually reachable
and that ``MiniMaxSource`` can clone + parse + return at least one
real skill from ``MiniMax-AI/cli``.

Local invocation::

    OC_TEST_NETWORK=1 uv run pytest tests/test_minimax_real_network.py -v

CI behaviour: skipped (env var unset). This protects CI from
GitHub flake / outage / rate-limit noise.

Why a separate file: keeps the network-gated test away from the
fixture-only tests so a single ``pytest tests/`` invocation in CI
never even loads it on disk.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from opencomputer.skills_hub.sources.minimax import (
    SKILLS_SUBDIR,
    UPSTREAM_REPO,
    MiniMaxSource,
)

# Master gate: every test in this module is skipped unless the env var
# is set. Fail-open by design — a network outage must NOT fail CI.
NETWORK_TESTS_ENABLED = os.environ.get("OC_TEST_NETWORK") == "1"
GIT_AVAILABLE = shutil.which("git") is not None

pytestmark = [
    pytest.mark.skipif(
        not NETWORK_TESTS_ENABLED,
        reason="set OC_TEST_NETWORK=1 to run real-network tests",
    ),
    pytest.mark.skipif(
        not GIT_AVAILABLE,
        reason="git executable not found in PATH",
    ),
]


@pytest.fixture()
def clone_root(tmp_path: Path) -> Path:
    return tmp_path / "minimax_clones"


def test_clone_succeeds(clone_root: Path):
    """The most basic check — git clone of MiniMax-AI/cli works."""
    src = MiniMaxSource(clone_root=clone_root)
    src._ensure_cloned()
    expected = clone_root / "MiniMax-AI" / "cli"
    assert expected.exists(), f"clone dir missing: {expected}"
    assert (expected / ".git").exists(), "not a git repo"


def test_search_returns_at_least_one_skill(clone_root: Path):
    """Real upstream should expose at least one parseable SKILL.md."""
    src = MiniMaxSource(clone_root=clone_root)
    results = src.search("")
    assert len(results) >= 1, (
        f"upstream {UPSTREAM_REPO} returned 0 skills — has the repo "
        f"layout changed? Check skills/ subdir or the SKILL.md format."
    )
    # Spot-check the shape
    sample = results[0]
    assert sample.identifier.startswith("minimax/")
    assert sample.source == "minimax"
    assert sample.name
    assert sample.description


def test_fetch_first_skill_returns_skill_md(clone_root: Path):
    """Fetch the first listed skill end-to-end — bundle has SKILL.md."""
    src = MiniMaxSource(clone_root=clone_root)
    results = src.search("")
    if not results:
        pytest.skip("upstream has no skills (caller should fix the repo)")
    bundle = src.fetch(results[0].identifier)
    assert bundle is not None
    assert bundle.identifier == results[0].identifier
    assert bundle.skill_md  # non-empty
    # files dict can be empty (some skills are SKILL.md-only) — that's OK
    assert isinstance(bundle.files, dict)


def test_inspect_first_skill_returns_meta(clone_root: Path):
    src = MiniMaxSource(clone_root=clone_root)
    results = src.search("")
    if not results:
        pytest.skip("upstream has no skills")
    meta = src.inspect(results[0].identifier)
    assert meta is not None
    assert meta.identifier == results[0].identifier


def test_walk_uses_skills_subdir_if_present(clone_root: Path):
    """If the upstream layout has skills/, the walk filters to that dir.
    This is a soft assertion — if upstream changes the layout, we only
    log not fail (the fall-through walk in MiniMaxSource handles it)."""
    src = MiniMaxSource(clone_root=clone_root)
    src._ensure_cloned()
    skills = src._clone_dir / SKILLS_SUBDIR
    walk = src._walk_skills()
    assert len(walk) >= 1, "no SKILL.md files found anywhere"
    if skills.exists():
        # If skills/ is present, every walked path must be under it
        for p in walk:
            assert str(p).startswith(str(skills)), (
                f"unexpected SKILL.md outside skills/: {p}"
            )
