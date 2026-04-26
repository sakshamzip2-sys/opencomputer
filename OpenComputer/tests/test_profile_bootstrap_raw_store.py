"""Raw artifact store tests — content-addressed deduplicated storage."""
from pathlib import Path

from opencomputer.profile_bootstrap.raw_store import (
    RawStoreEntry,
    compute_content_hash,
    has_artifact,
    read_artifact,
    store_artifact,
)


def test_compute_content_hash_is_sha256():
    h = compute_content_hash("hello world")
    assert len(h) == 64
    assert h == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_compute_content_hash_handles_bytes():
    h_str = compute_content_hash("hello")
    h_bytes = compute_content_hash(b"hello")
    assert h_str == h_bytes


def test_store_artifact_writes_to_content_addressed_path(tmp_path: Path):
    entry = store_artifact(
        content="hello world",
        kind="file",
        source_path="/Users/saksham/notes.md",
        store_root=tmp_path,
    )
    assert isinstance(entry, RawStoreEntry)
    assert entry.kind == "file"
    # Path layout: <root>/<aa>/<bb>/<full-sha>.json
    assert entry.path.exists()
    rel = entry.path.relative_to(tmp_path)
    parts = rel.parts
    assert len(parts) == 3
    assert len(parts[0]) == 2
    assert len(parts[1]) == 2


def test_store_artifact_idempotent(tmp_path: Path):
    e1 = store_artifact(content="X", kind="file", source_path="/a", store_root=tmp_path)
    e2 = store_artifact(content="X", kind="file", source_path="/b", store_root=tmp_path)
    # Same content → same hash → same path → entry idempotent
    assert e1.content_hash == e2.content_hash
    assert e1.path == e2.path


def test_has_artifact_true_after_store(tmp_path: Path):
    h = compute_content_hash("payload")
    assert has_artifact(h, store_root=tmp_path) is False
    store_artifact(content="payload", kind="file", source_path="/x", store_root=tmp_path)
    assert has_artifact(h, store_root=tmp_path) is True


def test_read_artifact_returns_content(tmp_path: Path):
    entry = store_artifact(
        content="quick brown fox", kind="file", source_path="/a", store_root=tmp_path,
    )
    read = read_artifact(entry.content_hash, store_root=tmp_path)
    assert read is not None
    assert read.content == "quick brown fox"
    assert read.kind == "file"


def test_read_artifact_returns_none_when_absent(tmp_path: Path):
    h = compute_content_hash("never stored")
    assert read_artifact(h, store_root=tmp_path) is None
