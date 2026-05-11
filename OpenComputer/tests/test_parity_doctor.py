"""Tests for ``oc parity-doctor`` — spec parser + check runner.

These tests pin the *contract*: the parser must correctly extract
features from ``docs/OC-FROM-OPENCLAW.md``, the runner must classify
each feature into one of {shipped, partial, scaffolded, missing}, and
the CLI must surface the table without crashing.

The actual *status* of any given feature is allowed to evolve — these
tests assert structure, not specific results, so the suite stays
green while items move from missing → shipped.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_parity_doctor import parity_app
from opencomputer.parity_doctor import (
    FEATURE_CHECKS,
    FeatureCheck,
    FeatureRecord,
    _classify,
    _has_match,
    parse_spec,
    render_markdown,
    run_checks,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC_PATH = _REPO_ROOT / "docs" / "OC-FROM-OPENCLAW.md"


# ─── parser ───────────────────────────────────────────────────────────


def test_parse_spec_extracts_all_20_features():
    if not _SPEC_PATH.is_file():
        pytest.skip("spec markdown not present")
    records = parse_spec(_SPEC_PATH)
    # Spec is authored with exactly 20 entries across three tiers.
    assert len(records) == 20
    # Numbers are unique and contiguous starting at 1.
    numbers = [r.number for r in records]
    assert numbers == list(range(1, 21))
    # Each record has a non-empty title.
    for r in records:
        assert r.title


def test_parse_spec_assigns_correct_tiers():
    if not _SPEC_PATH.is_file():
        pytest.skip("spec markdown not present")
    records = parse_spec(_SPEC_PATH)
    by_number = {r.number: r for r in records}
    # Tier 1 = items 1-5, Tier 2 = 6-14, Tier 3 = 15-20 in the source spec.
    for n in range(1, 6):
        assert by_number[n].tier == 1
    for n in range(6, 15):
        assert by_number[n].tier == 2
    for n in range(15, 21):
        assert by_number[n].tier == 3


def test_parse_spec_handles_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_spec(tmp_path / "nope.md")


def test_parse_spec_with_no_tiers_returns_empty(tmp_path):
    spec = tmp_path / "empty.md"
    spec.write_text("# Hello\n\nNo tier headings here.\n")
    assert parse_spec(spec) == []


def test_parse_spec_with_synthetic_input(tmp_path):
    spec = tmp_path / "synthetic.md"
    spec.write_text(
        "# Header\n\n"
        "## TIER 1 — Build These Now\n\n"
        "### 1. Alpha Feature\n\nDescription.\n\n"
        "### 2. Beta Feature\n\nMore.\n\n"
        "## TIER 2 — Later\n\n"
        "### 3. Gamma Feature\n\nLater.\n",
    )
    records = parse_spec(spec)
    titles = [(r.number, r.title, r.tier) for r in records]
    assert titles == [
        (1, "Alpha Feature", 1),
        (2, "Beta Feature", 1),
        (3, "Gamma Feature", 2),
    ]


# ─── classifier ───────────────────────────────────────────────────────


def test_classify_all_match_returns_shipped():
    assert _classify(matched=["x"], missing=[], scaffolded_hits=[]) == "shipped"


def test_classify_partial_match():
    assert _classify(matched=["x"], missing=["y"], scaffolded_hits=[]) == "partial"


def test_classify_only_scaffolded_hits():
    assert _classify(matched=[], missing=["y"], scaffolded_hits=["primitive"]) == "scaffolded"


def test_classify_no_evidence_is_missing():
    assert _classify(matched=[], missing=["y"], scaffolded_hits=[]) == "missing"


# ─── runner integration ───────────────────────────────────────────────


def test_run_checks_returns_one_result_per_record():
    if not _SPEC_PATH.is_file():
        pytest.skip("spec markdown not present")
    results = run_checks(spec_path=_SPEC_PATH, repo_root=_REPO_ROOT)
    assert len(results) == 20
    # Status is one of the canonical values.
    valid = {"shipped", "partial", "scaffolded", "missing"}
    for r in results:
        assert r.status in valid


def test_run_checks_marks_skill_requirements_gating_shipped():
    """M1 of this PR — once shipped, parity-doctor must reflect it."""
    if not _SPEC_PATH.is_file():
        pytest.skip("spec markdown not present")
    results = run_checks(spec_path=_SPEC_PATH, repo_root=_REPO_ROOT)
    by_number = {r.record.number: r for r in results}
    # Item 4 = "Skill Requirements Gating".
    assert by_number[4].status == "shipped"


def test_run_checks_marks_secrets_provider_chain_shipped():
    """M2 of this PR."""
    if not _SPEC_PATH.is_file():
        pytest.skip("spec markdown not present")
    results = run_checks(spec_path=_SPEC_PATH, repo_root=_REPO_ROOT)
    by_number = {r.record.number: r for r in results}
    # Item 3 = "Structured Secrets Management (SecretRefs)".
    assert by_number[3].status == "shipped"


def test_run_checks_known_shipped_features_stay_shipped():
    """Heartbeat, fallback chain, loop detection were shipped before this PR.

    If parity-doctor regresses on any of these, something either got
    deleted or our check symbols are out of date. Either way, the
    failure is informative.
    """
    if not _SPEC_PATH.is_file():
        pytest.skip("spec markdown not present")
    results = run_checks(spec_path=_SPEC_PATH, repo_root=_REPO_ROOT)
    by_number = {r.record.number: r for r in results}
    assert by_number[1].status == "shipped"   # Heartbeat
    assert by_number[2].status == "shipped"   # Fallback chain
    assert by_number[7].status == "shipped"   # Tool-loop detection


def test_run_checks_round3_openclaw_drive_landed():
    """2026-05-11 (round 3) drove OpenClaw parity from 8/20 → 20/20.

    Items 6 (Lobster), 9 (Trajectory Bundles), 10 (Broadcast Groups),
    and 18 (Multi-Account Channel) all shipped real implementations
    with the symbols the parity-doctor spec mandates. This test pins
    the new state so a regression on any of them fails loudly.

    The prior version of this test asserted those items stayed
    ``missing``; flipping the assertion mirrors the codebase reality.
    Items 8 (Tokenjuice) and 20 (Context Pruning Modes) shipped on
    2026-05-10. Items 7 + 11–17 + 19 shipped or were promoted in
    round 3 on 2026-05-11.
    """
    if not _SPEC_PATH.is_file():
        pytest.skip("spec markdown not present")
    results = run_checks(spec_path=_SPEC_PATH, repo_root=_REPO_ROOT)
    by_number = {r.record.number: r for r in results}
    for n in (6, 9, 10, 18):
        assert by_number[n].status == "shipped", (
            f"round-3 OpenClaw item {n} regressed to {by_number[n].status}"
        )


def test_run_checks_marks_tokenjuice_shipped():
    """M4 of this PR — tokenjuice tool-result compaction."""
    if not _SPEC_PATH.is_file():
        pytest.skip("spec markdown not present")
    results = run_checks(spec_path=_SPEC_PATH, repo_root=_REPO_ROOT)
    by_number = {r.record.number: r for r in results}
    assert by_number[8].status == "shipped"


def test_run_checks_marks_context_pruning_shipped():
    """M6 of this PR — context pruning modes."""
    if not _SPEC_PATH.is_file():
        pytest.skip("spec markdown not present")
    results = run_checks(spec_path=_SPEC_PATH, repo_root=_REPO_ROOT)
    by_number = {r.record.number: r for r in results}
    assert by_number[20].status == "shipped"


# ─── markdown render ──────────────────────────────────────────────────


def test_render_markdown_has_table_header():
    record = FeatureRecord(number=1, title="x", tier=1)
    from opencomputer.parity_doctor import CheckResult

    out = render_markdown([
        CheckResult(record=record, status="shipped", matched=("x",), missing=(), notes=""),
    ])
    assert "| #" in out
    assert "✅ shipped" in out
    assert "Total: 1" in out


# ─── feature-check registry sanity ────────────────────────────────────


def test_feature_check_registry_has_unique_numbers():
    numbers = [c.number for c in FEATURE_CHECKS]
    assert len(numbers) == len(set(numbers)), "duplicate numbers in FEATURE_CHECKS"


def test_feature_check_numbers_match_spec_numbers():
    if not _SPEC_PATH.is_file():
        pytest.skip("spec markdown not present")
    spec_numbers = {r.number for r in parse_spec(_SPEC_PATH)}
    check_numbers = {c.number for c in FEATURE_CHECKS}
    assert check_numbers.issubset(spec_numbers), (
        "FEATURE_CHECKS references numbers not present in the spec"
    )


# ─── grep harness ─────────────────────────────────────────────────────


def test_has_match_finds_known_string(tmp_path):
    target = tmp_path / "x.py"
    target.write_text("hello_world_marker = 1\n")
    assert _has_match("hello_world_marker", tmp_path) is True
    assert _has_match("definitely_not_present_xyz", tmp_path) is False


# ─── CLI smoke ────────────────────────────────────────────────────────


def test_cli_run_prints_table():
    runner = CliRunner()
    result = runner.invoke(parity_app, ["run"])
    assert result.exit_code == 0, result.output
    assert "Parity vs" in result.output
    # At least one shipped row should be present (we shipped lots).
    assert "shipped" in result.output


def test_cli_run_json_output():
    runner = CliRunner()
    result = runner.invoke(parity_app, ["run", "--json"])
    assert result.exit_code == 0, result.output
    import json as _json

    payload = _json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 20
    assert all("number" in r and "status" in r for r in payload)


def test_cli_run_writes_markdown(tmp_path: Path):
    runner = CliRunner()
    out = tmp_path / "out.md"
    result = runner.invoke(parity_app, ["run", "--write", str(out)])
    assert result.exit_code == 0, result.output
    assert out.is_file()
    content = out.read_text(encoding="utf-8")
    assert "| #" in content
    assert "Total:" in content
