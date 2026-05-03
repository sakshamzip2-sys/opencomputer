"""Phase 7 — atomic promotion of candidate cases into the canonical cases file."""

from __future__ import annotations

import pytest

from opencomputer.evals.promote import promote_candidates


def test_promote_appends_candidates_to_cases(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    cases = cases_dir / "x.jsonl"
    candidates = cases_dir / "x.candidates.jsonl"

    cases.write_text('{"id": "a", "input": {}, "expected": "yes"}\n')
    candidates.write_text(
        '{"id": "b", "input": {}, "expected": "no"}\n'
        '{"id": "c", "input": {}, "expected": "yes"}\n'
    )

    n = promote_candidates(site_name="x", cases_dir=cases_dir)
    assert n == 2

    contents = cases.read_text()
    assert '"id": "a"' in contents
    assert '"id": "b"' in contents
    assert '"id": "c"' in contents
    assert not candidates.exists(), "candidates should be cleared after promote"


def test_promote_rejects_duplicate_ids(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    cases = cases_dir / "x.jsonl"
    candidates = cases_dir / "x.candidates.jsonl"

    cases.write_text('{"id": "a", "input": {}, "expected": "yes"}\n')
    candidates.write_text('{"id": "a", "input": {}, "expected": "no"}\n')

    with pytest.raises(ValueError, match="duplicate"):
        promote_candidates(site_name="x", cases_dir=cases_dir)

    # Original cases file unchanged on failure
    assert cases.read_text() == '{"id": "a", "input": {}, "expected": "yes"}\n'
    # Candidates untouched on failure
    assert candidates.exists()


def test_promote_no_candidates_returns_zero(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    n = promote_candidates(site_name="x", cases_dir=cases_dir)
    assert n == 0


def test_promote_creates_cases_file_when_missing(tmp_path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    candidates = cases_dir / "x.candidates.jsonl"
    candidates.write_text('{"id": "b", "input": {}, "expected": "no"}\n')

    n = promote_candidates(site_name="x", cases_dir=cases_dir)
    assert n == 1
    cases = cases_dir / "x.jsonl"
    assert cases.exists()
    assert '"id": "b"' in cases.read_text()


def test_promote_handles_internal_duplicates(tmp_path):
    """Two candidates with the same ID should also fail."""
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    candidates = cases_dir / "x.candidates.jsonl"
    candidates.write_text(
        '{"id": "dup", "input": {}}\n'
        '{"id": "dup", "input": {}}\n'
    )

    with pytest.raises(ValueError, match="duplicate"):
        promote_candidates(site_name="x", cases_dir=cases_dir)
