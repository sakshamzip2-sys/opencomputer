"""tests/test_skill_evolution_no_raw_transcript.py — privacy contract enforcement.

After ``add_candidate()``, the only files in ``_proposed/<name>/`` should
be ``SKILL.md`` and ``provenance.json``. No raw transcript, no message
log, no tool-call dump.

The provenance file itself is metadata-only: ``session_id``, timestamps,
``confidence_score``, and a short ``source_summary`` are allowed.
``messages``, ``transcript``, ``tool_calls``, ``raw_session``, and
``user_messages`` keys are forbidden.
"""

from __future__ import annotations

import json
import time

from extensions.skill_evolution.candidate_store import add_candidate
from extensions.skill_evolution.skill_extractor import ProposedSkill


def _make_proposal(name: str = "auto-x") -> ProposedSkill:
    return ProposedSkill(
        name=name,
        description="d",
        body=f"---\nname: {name}\ndescription: d\n---\n\n# T\n\nbody",
        provenance={
            "session_id": "s1",
            "generated_at": time.time(),
            "confidence_score": 80,
            "source_summary": "test",
        },
    )


def test_proposed_dir_only_contains_skill_md_and_provenance(tmp_path):
    add_candidate(tmp_path, _make_proposal("auto-test"))
    proposed = tmp_path / "skills" / "_proposed" / "auto-test"
    files = sorted(p.name for p in proposed.iterdir())
    assert files == [
        "SKILL.md",
        "provenance.json",
    ], f"unexpected files in proposed dir: {files}"


def test_no_transcript_or_messages_files(tmp_path):
    add_candidate(tmp_path, _make_proposal("auto-test2"))
    proposed = tmp_path / "skills" / "_proposed" / "auto-test2"
    forbidden_names = {
        "transcript.json",
        "messages.jsonl",
        "session.log",
        "raw.txt",
    }
    found = {p.name for p in proposed.iterdir()}
    leaks = found & forbidden_names
    assert not leaks, f"forbidden transcript files found: {leaks}"


def test_provenance_json_does_not_contain_raw_messages(tmp_path):
    """``provenance.json`` should be metadata-only (session_id, timestamps,
    confidence) — never message bodies."""
    add_candidate(tmp_path, _make_proposal("auto-meta"))
    prov = json.loads(
        (
            tmp_path / "skills" / "_proposed" / "auto-meta" / "provenance.json"
        ).read_text()
    )
    forbidden_keys = {
        "messages",
        "transcript",
        "tool_calls",
        "raw_session",
        "user_messages",
    }
    leaked = forbidden_keys & set(prov.keys())
    assert not leaked, f"provenance.json contains forbidden keys: {leaked}"
