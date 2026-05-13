"""Tests for opencomputer.agent.handoff.inbox."""
from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from opencomputer.agent.handoff.inbox import (
    HandoffInbox,
    HandoffParseError,
    InboxIOError,
)
from opencomputer.agent.handoff.models import HandoffDocument, HandoffMetadata
from opencomputer.agent.handoff.protocol_v2 import PROTOCOL_VERSION


def _make_doc(
    *,
    source: str = "default",
    target: str = "stocks",
    body: str = "A small handoff body.\n\nWith two paragraphs.",
    trigger: str = "auto",
    generated_at: str = "2026-05-13T14:32:01Z",
    confidence: float | None = 0.85,
    reason: str | None = "state-query detected",
) -> HandoffDocument:
    return HandoffDocument(
        metadata=HandoffMetadata(
            protocol_version=PROTOCOL_VERSION,
            source_profile=source,
            target_profile=target,
            generated_at=generated_at,
            source_session_id="sess-01",
            trigger=trigger,  # type: ignore[arg-type]
            classifier_confidence=confidence,
            classifier_reason=reason,
        ),
        body=body,
    )


class TestInboxWrite:
    def test_atomic_write_round_trip(self, tmp_path: Path) -> None:
        inbox = HandoffInbox(tmp_path)
        doc = _make_doc()
        path = inbox.write(doc)
        assert path.exists()
        # Filename shape
        assert path.name.startswith("handoff_")
        assert path.name.endswith(".md")
        assert "_default_" in path.name
        # Round-trip read
        read_doc = inbox.read(path)
        assert read_doc.metadata.source_profile == "default"
        assert read_doc.metadata.target_profile == "stocks"
        assert read_doc.metadata.classifier_confidence == pytest.approx(0.85)
        assert "two paragraphs" in read_doc.body

    def test_write_creates_inbox_dir(self, tmp_path: Path) -> None:
        # tmp_path/profile/inbox does NOT exist yet
        profile_home = tmp_path / "profile"
        inbox = HandoffInbox(profile_home)
        assert not (profile_home / "inbox").exists()
        inbox.write(_make_doc())
        assert (profile_home / "inbox").exists()

    def test_write_empty_body_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="empty"):
            HandoffInbox(tmp_path).write(_make_doc(body="   \n  "))

    def test_write_wrong_protocol_version_refused(self, tmp_path: Path) -> None:
        bad_doc = HandoffDocument(
            metadata=HandoffMetadata(
                protocol_version="handoff-v3",  # type: ignore[arg-type]
                source_profile="a", target_profile="b",
                generated_at="2026-05-13T00:00:00Z",
                source_session_id="x", trigger="manual",
            ),
            body="body",
        )
        with pytest.raises(ValueError, match="protocol_version"):
            HandoffInbox(tmp_path).write(bad_doc)

    def test_write_unwritable_dir_raises_inbox_io_error(
        self, tmp_path: Path,
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root bypasses permission bits")
        inbox = HandoffInbox(tmp_path / "profile")
        inbox.inbox_dir.mkdir(parents=True)
        os.chmod(inbox.inbox_dir, 0o500)  # read+exec, no write
        try:
            with pytest.raises(InboxIOError):
                inbox.write(_make_doc())
        finally:
            os.chmod(inbox.inbox_dir, 0o700)

    def test_write_wrong_input_type_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TypeError):
            HandoffInbox(tmp_path).write("not a doc")  # type: ignore[arg-type]

    def test_construct_with_non_path_raises(self) -> None:
        with pytest.raises(TypeError):
            HandoffInbox("not-a-path")  # type: ignore[arg-type]


class TestInboxRead:
    def test_list_pending_empty_dir(self, tmp_path: Path) -> None:
        inbox = HandoffInbox(tmp_path)
        assert inbox.list_pending() == []

    def test_list_pending_sorted_by_timestamp(self, tmp_path: Path) -> None:
        inbox = HandoffInbox(tmp_path)
        # Write 3 docs with explicit different timestamps
        for stamp in ("2026-01-01T00:00:00Z",
                      "2026-03-15T12:30:00Z",
                      "2026-05-13T08:00:00Z"):
            inbox.write(_make_doc(generated_at=stamp))
        listed = inbox.list_pending()
        assert len(listed) == 3
        # File names sort chronologically
        assert "20260101" in listed[0].name
        assert "20260515" in listed[-1].name or "20260513" in listed[-1].name

    def test_non_handoff_files_ignored(self, tmp_path: Path) -> None:
        inbox = HandoffInbox(tmp_path)
        inbox.inbox_dir.mkdir(parents=True)
        (inbox.inbox_dir / "README.md").write_text("not a handoff")
        (inbox.inbox_dir / "backup.bak").write_text("noise")
        inbox.write(_make_doc())
        listed = inbox.list_pending()
        assert len(listed) == 1
        assert listed[0].name.startswith("handoff_")

    def test_mark_processed_moves_to_archive(self, tmp_path: Path) -> None:
        inbox = HandoffInbox(tmp_path)
        path = inbox.write(_make_doc())
        assert path.exists()
        dest = inbox.mark_processed(path)
        assert not path.exists()
        assert dest.exists()
        assert dest.parent.name == "processed"
        # Re-listing pending must not return the processed file
        assert inbox.list_pending() == []

    def test_mark_processed_idempotent(self, tmp_path: Path) -> None:
        inbox = HandoffInbox(tmp_path)
        path = inbox.write(_make_doc())
        inbox.mark_processed(path)
        # Calling again on a non-existent path returns the expected dest, no raise
        dest = inbox.mark_processed(path)
        assert dest.parent.name == "processed"

    def test_read_and_process_all_archives_each(self, tmp_path: Path) -> None:
        inbox = HandoffInbox(tmp_path)
        inbox.write(_make_doc(generated_at="2026-01-01T00:00:00Z"))
        inbox.write(_make_doc(generated_at="2026-02-01T00:00:00Z"))
        docs = inbox.read_and_process_all()
        assert len(docs) == 2
        # Both should be archived now
        assert inbox.list_pending() == []
        # Bodies present
        for d in docs:
            assert "paragraphs" in d.body


class TestInboxParse:
    def test_missing_frontmatter_raises_parse_error(
        self, tmp_path: Path,
    ) -> None:
        inbox = HandoffInbox(tmp_path)
        inbox.inbox_dir.mkdir(parents=True)
        bad = inbox.inbox_dir / "handoff_20260101T000000Z_default_abc123.md"
        bad.write_text("no frontmatter just body")
        with pytest.raises(HandoffParseError, match="frontmatter"):
            inbox.read(bad)

    def test_unknown_protocol_version_raises(self, tmp_path: Path) -> None:
        inbox = HandoffInbox(tmp_path)
        inbox.inbox_dir.mkdir(parents=True)
        bad = inbox.inbox_dir / "handoff_20260101T000000Z_default_abc123.md"
        bad.write_text(
            "---\n"
            "protocol_version: handoff-v9\n"
            "source_profile: a\n"
            "target_profile: b\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "source_session_id: x\n"
            "trigger: auto\n"
            "---\n"
            "body\n"
        )
        with pytest.raises(HandoffParseError, match="protocol_version"):
            inbox.read(bad)

    def test_missing_required_fields_raises(self, tmp_path: Path) -> None:
        inbox = HandoffInbox(tmp_path)
        inbox.inbox_dir.mkdir(parents=True)
        bad = inbox.inbox_dir / "handoff_20260101T000000Z_default_abc123.md"
        bad.write_text(
            "---\n"
            f"protocol_version: {PROTOCOL_VERSION}\n"
            "source_profile: a\n"
            # missing target_profile, generated_at, source_session_id, trigger
            "---\n"
            "body\n"
        )
        with pytest.raises(HandoffParseError, match="missing"):
            inbox.read(bad)

    def test_invalid_trigger_raises(self, tmp_path: Path) -> None:
        inbox = HandoffInbox(tmp_path)
        inbox.inbox_dir.mkdir(parents=True)
        bad = inbox.inbox_dir / "handoff_20260101T000000Z_default_abc123.md"
        bad.write_text(
            "---\n"
            f"protocol_version: {PROTOCOL_VERSION}\n"
            "source_profile: a\n"
            "target_profile: b\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "source_session_id: x\n"
            "trigger: unknown\n"
            "---\n"
            "body\n"
        )
        with pytest.raises(HandoffParseError, match="trigger"):
            inbox.read(bad)

    def test_confidence_out_of_range_raises(self, tmp_path: Path) -> None:
        inbox = HandoffInbox(tmp_path)
        inbox.inbox_dir.mkdir(parents=True)
        bad = inbox.inbox_dir / "handoff_20260101T000000Z_default_abc123.md"
        bad.write_text(
            "---\n"
            f"protocol_version: {PROTOCOL_VERSION}\n"
            "source_profile: a\n"
            "target_profile: b\n"
            "generated_at: 2026-01-01T00:00:00Z\n"
            "source_session_id: x\n"
            "trigger: auto\n"
            "classifier_confidence: 1.5\n"
            "---\n"
            "body\n"
        )
        with pytest.raises(HandoffParseError, match="out of range"):
            inbox.read(bad)

    def test_read_and_process_all_skips_malformed(self, tmp_path: Path) -> None:
        inbox = HandoffInbox(tmp_path)
        # One good doc + one malformed file
        inbox.write(_make_doc())
        inbox.inbox_dir.mkdir(parents=True, exist_ok=True)
        bad = inbox.inbox_dir / "handoff_20260101T000000Z_default_bad999.md"
        bad.write_text("garbage")
        docs = inbox.read_and_process_all()
        # Good one came through; bad one was logged + skipped
        assert len(docs) == 1


class TestInboxConcurrency:
    def test_parallel_writes_no_collision(self, tmp_path: Path) -> None:
        """Multiple concurrent writes should not collide (random suffix)."""
        inbox = HandoffInbox(tmp_path)
        errors: list[Exception] = []
        paths: list[Path] = []
        lock = threading.Lock()

        def write_one(idx: int) -> None:
            try:
                p = inbox.write(_make_doc(source=f"src{idx}"))
                with lock:
                    paths.append(p)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=write_one, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        # All 8 files should exist, all unique
        assert len({p.name for p in paths}) == 8
        assert len(inbox.list_pending()) == 8
