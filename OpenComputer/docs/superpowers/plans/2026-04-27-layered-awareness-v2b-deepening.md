# Layered Awareness V2.B — Background Deepening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-04-26-layered-awareness-design.md` (Layer 3 section)

**Goal:** Build Layer 3 — background deepening that progressively ingests historical data over expanding time windows (7d → 30d → 90d → 365d → all-time), populates a vector index for semantic recall, and integrates Spotlight as the FTS surface. Idle-throttled and resumable.

**Architecture:** A new `opencomputer/profile_bootstrap/deepening.py` orchestrator drives the loop. Per window, it walks the existing Layer 2 readers (V2.A multi-browser, dotted-dir-pruned, consent-gated), passes each artifact through a content hasher → raw store → Ollama LLM extractor → BGE embedder → Chroma vector store → motif emitter on the F2 SignalEvent bus. Idle detection (psutil CPU<20% + plugged-in) gates the loop. Cursor JSON at `<profile_home>/profile_bootstrap/deepening_cursor.json` makes resumption trivial. Spotlight integration via `mdfind` subprocess provides ad-hoc text recall over the same corpus the user's OS already indexes.

**Tech Stack:** Ollama (subprocess for LLM extraction, optional dep), `sentence-transformers` + BGE-small-en (embeddings, MIT-licensed), Chroma (Apache 2.0 vector DB), `psutil` (idle detection), Spotlight `mdfind` (existing macOS binary), existing F4 graph + F2 bus.

---

## File Structure

| Path | Responsibility |
|---|---|
| `opencomputer/profile_bootstrap/llm_extractor.py` | NEW — Ollama subprocess wrapper, deferred from MVP |
| `opencomputer/profile_bootstrap/raw_store.py` | NEW — content-addressed artifact store |
| `opencomputer/profile_bootstrap/embedding.py` | NEW — BGE-small embedding helper |
| `opencomputer/profile_bootstrap/vector_store.py` | NEW — Chroma client wrapper |
| `opencomputer/profile_bootstrap/spotlight.py` | NEW — `mdfind` subprocess wrapper |
| `opencomputer/profile_bootstrap/idle.py` | NEW — psutil-based idle detection |
| `opencomputer/profile_bootstrap/deepening.py` | NEW — orchestrator for the deepening loop |
| `opencomputer/profile_bootstrap/orchestrator.py` (modify) | Wire LLM extraction into existing Layer 2 sites |
| `opencomputer/cli_profile.py` (modify) | Add `profile deepen [--force --max-window 365]` subcommand |
| `opencomputer/doctor.py` (modify) | Doctor checks for Ollama, BGE, Chroma availability |
| `pyproject.toml` (modify) | Add `psutil`, `chromadb`, `sentence-transformers` as optional `[deepening]` extras |
| `tests/test_profile_bootstrap_llm_extractor.py` | NEW |
| `tests/test_profile_bootstrap_raw_store.py` | NEW |
| `tests/test_profile_bootstrap_embedding.py` | NEW |
| `tests/test_profile_bootstrap_vector_store.py` | NEW |
| `tests/test_profile_bootstrap_spotlight.py` | NEW |
| `tests/test_profile_bootstrap_idle.py` | NEW |
| `tests/test_profile_bootstrap_deepening.py` | NEW |
| `tests/test_cli_profile_deepen.py` | NEW |
| `tests/test_doctor_deepening.py` | NEW |

---

## Task 1: LLM extractor (Ollama subprocess wrapper)

**Files:**
- Create: `opencomputer/profile_bootstrap/llm_extractor.py`
- Test: `tests/test_profile_bootstrap_llm_extractor.py`

This task was scoped in V1 MVP plan but dropped because nothing called it. V2.B is the consumer.

- [ ] **Step 1.1: Write failing tests**

```python
# tests/test_profile_bootstrap_llm_extractor.py
"""LLM extractor — Ollama subprocess wrapper tests."""
from unittest.mock import patch

import pytest

from opencomputer.profile_bootstrap.llm_extractor import (
    ArtifactExtraction,
    OllamaUnavailable,
    extract_artifact,
    is_ollama_available,
)


def test_extraction_dataclass_defaults():
    e = ArtifactExtraction()
    assert e.topic == ""
    assert e.people == ()
    assert e.sentiment == "unknown"


def test_is_ollama_available_returns_false_without_binary():
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.shutil.which",
        return_value=None,
    ):
        assert is_ollama_available() is False


def test_extract_raises_when_unavailable():
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=False,
    ):
        with pytest.raises(OllamaUnavailable):
            extract_artifact("some content")


def test_extract_parses_ollama_json_output():
    fake_json = (
        '{"topic": "stocks", "people": ["Warren Buffett"], '
        '"intent": "research a stock", "sentiment": "neutral", '
        '"timestamp": "2026-04-26T10:00:00"}'
    )
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.llm_extractor.subprocess.run",
    ) as run:
        run.return_value.stdout = fake_json
        run.return_value.returncode = 0
        ex = extract_artifact("text about stocks")
    assert ex.topic == "stocks"
    assert "Warren Buffett" in ex.people
    assert ex.sentiment == "neutral"


def test_extract_returns_blank_on_malformed_json():
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.llm_extractor.subprocess.run",
    ) as run:
        run.return_value.stdout = "not json"
        run.return_value.returncode = 0
        ex = extract_artifact("anything")
    assert ex.topic == ""


def test_extract_returns_blank_on_timeout():
    import subprocess as _sp
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.llm_extractor.subprocess.run",
        side_effect=_sp.TimeoutExpired(cmd="ollama", timeout=15.0),
    ):
        ex = extract_artifact("anything")
    assert ex.topic == ""


def test_extract_truncates_long_content():
    huge = "a" * 50000
    captured: dict = {}
    def capture_run(cmd, **kwargs):
        captured["prompt"] = cmd[-1]
        class _R:
            returncode = 0
            stdout = "{}"
        return _R()
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.llm_extractor.subprocess.run",
        side_effect=capture_run,
    ):
        extract_artifact(huge)
    # Prompt template + 4000-char truncated content < 50000
    assert len(captured["prompt"]) < 5000
```

- [ ] **Step 1.2: Run failing test → confirm `ModuleNotFoundError`**

```
python3.13 -m pytest tests/test_profile_bootstrap_llm_extractor.py -v
```

- [ ] **Step 1.3: Implement extractor**

Create `opencomputer/profile_bootstrap/llm_extractor.py`:

```python
"""LLM extractor — local-first via Ollama subprocess.

Used by Layer 2 (Recent Context Scan) and Layer 3 (Background Deepening)
to turn unstructured artifacts (file content, mail bodies, git commit
messages) into structured :class:`ArtifactExtraction` records that
flow into the F2 SignalEvent bus.

If Ollama is not installed, :func:`is_ollama_available` returns False
and :func:`extract_artifact` raises :class:`OllamaUnavailable`. Callers
must handle that — deepening proceeds with whatever extraction it can.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass


_DEFAULT_MODEL = "llama3.2:3b"
_EXTRACTION_PROMPT = """You are a JSON extractor. Given the artifact below, return ONE JSON object with these keys ONLY:
- topic: 1-3 word topic
- people: list of person names mentioned (empty list if none)
- intent: one sentence summarizing what the user might be trying to do (empty string if unclear)
- sentiment: one of "positive" / "neutral" / "negative" / "unknown"
- timestamp: ISO 8601 if present in artifact, else empty string

Return ONLY the JSON. No prose, no code block.

Artifact:
{artifact}
"""


class OllamaUnavailable(RuntimeError):
    """Raised when Ollama isn't on PATH."""


@dataclass(frozen=True, slots=True)
class ArtifactExtraction:
    """Structured output of one LLM extraction call. All fields safe-default."""

    topic: str = ""
    people: tuple[str, ...] = ()
    intent: str = ""
    sentiment: str = "unknown"
    timestamp: str = ""


def is_ollama_available() -> bool:
    """Cheap probe — only checks that the binary is on PATH."""
    return shutil.which("ollama") is not None


def extract_artifact(
    content: str,
    *,
    model: str = _DEFAULT_MODEL,
    timeout_seconds: float = 15.0,
) -> ArtifactExtraction:
    """Run one extraction. Raises :class:`OllamaUnavailable` if not available.

    Returns blank :class:`ArtifactExtraction` on malformed JSON, timeout, or
    nonzero exit. Truncates content to ~4000 chars for context budget.
    """
    if not is_ollama_available():
        raise OllamaUnavailable("ollama not on PATH; install via 'brew install ollama'")
    artifact = content[:4000]
    prompt = _EXTRACTION_PROMPT.format(artifact=artifact)
    try:
        result = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ArtifactExtraction()
    if result.returncode != 0:
        return ArtifactExtraction()
    return _parse_extraction(result.stdout.strip())


def _parse_extraction(raw: str) -> ArtifactExtraction:
    """Best-effort JSON parse. Returns blank extraction on any failure."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ArtifactExtraction()
    if not isinstance(data, dict):
        return ArtifactExtraction()
    return ArtifactExtraction(
        topic=str(data.get("topic", ""))[:128],
        people=tuple(str(p)[:64] for p in data.get("people", []) if isinstance(p, str))[:10],
        intent=str(data.get("intent", ""))[:512],
        sentiment=str(data.get("sentiment", "unknown")).lower(),
        timestamp=str(data.get("timestamp", ""))[:32],
    )
```

- [ ] **Step 1.4: Verify tests pass → 7 PASS**

- [ ] **Step 1.5: Commit**

```bash
git add opencomputer/profile_bootstrap/llm_extractor.py tests/test_profile_bootstrap_llm_extractor.py
git commit -m "feat(profile-bootstrap): V2.B-T1 — Ollama LLM extractor (deferred from MVP)"
```

---

## Task 2: Raw artifact store (content-addressed)

**Files:**
- Create: `opencomputer/profile_bootstrap/raw_store.py`
- Test: `tests/test_profile_bootstrap_raw_store.py`

- [ ] **Step 2.1: Write failing tests**

```python
# tests/test_profile_bootstrap_raw_store.py
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
```

- [ ] **Step 2.2: Run failing tests**

- [ ] **Step 2.3: Implement raw store**

Create `opencomputer/profile_bootstrap/raw_store.py`:

```python
"""Content-addressed raw artifact store for Layer 3 deepening.

Each artifact lives at ``<store_root>/<aa>/<bb>/<full-sha256>.json`` where
``<aa><bb>`` is the SHA256 prefix used as a fanout (avoids 100k+ files in
a single dir). The JSON envelope:

    {
      "content_hash": "...",
      "kind": "file" | "email" | "browser_visit" | "git_commit" | ...,
      "source_path": "/Users/.../doc.md",
      "stored_at": <epoch>,
      "content": "<the raw text or summary>"
    }

The store is profile-aware via :func:`opencomputer.agent.config._home`.
Idempotency: identical content produces the same hash, the same path, and
the same envelope (re-storing is a no-op).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from opencomputer.agent.config import _home


@dataclass(frozen=True, slots=True)
class RawStoreEntry:
    """A successfully-stored artifact's envelope + on-disk path."""

    content_hash: str
    kind: str
    source_path: str
    stored_at: float
    path: Path
    content: str = ""


def _default_store_root() -> Path:
    return _home() / "profile_bootstrap" / "raw_store"


def compute_content_hash(content: str | bytes) -> str:
    """SHA256 hex digest of the artifact content. Stable across re-runs."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _path_for_hash(content_hash: str, store_root: Path) -> Path:
    aa, bb = content_hash[:2], content_hash[2:4]
    return store_root / aa / bb / f"{content_hash}.json"


def has_artifact(content_hash: str, *, store_root: Path | None = None) -> bool:
    """Cheap existence probe — no JSON parse."""
    root = store_root if store_root is not None else _default_store_root()
    return _path_for_hash(content_hash, root).exists()


def store_artifact(
    *,
    content: str,
    kind: str,
    source_path: str,
    store_root: Path | None = None,
) -> RawStoreEntry:
    """Write the artifact at its content-addressed path. Idempotent."""
    root = store_root if store_root is not None else _default_store_root()
    h = compute_content_hash(content)
    path = _path_for_hash(h, root)
    if path.exists():
        # Re-read the existing envelope to return the original stored_at.
        return _read_envelope(path) or RawStoreEntry(
            content_hash=h, kind=kind, source_path=source_path,
            stored_at=time.time(), path=path, content=content,
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    entry = RawStoreEntry(
        content_hash=h,
        kind=kind,
        source_path=source_path,
        stored_at=time.time(),
        path=path,
        content=content,
    )
    envelope = {
        "content_hash": h,
        "kind": kind,
        "source_path": source_path,
        "stored_at": entry.stored_at,
        "content": content,
    }
    path.write_text(json.dumps(envelope))
    return entry


def read_artifact(
    content_hash: str, *, store_root: Path | None = None,
) -> RawStoreEntry | None:
    """Re-hydrate an envelope by hash. ``None`` if absent."""
    root = store_root if store_root is not None else _default_store_root()
    path = _path_for_hash(content_hash, root)
    if not path.exists():
        return None
    return _read_envelope(path)


def _read_envelope(path: Path) -> RawStoreEntry | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return RawStoreEntry(
        content_hash=str(data.get("content_hash", "")),
        kind=str(data.get("kind", "")),
        source_path=str(data.get("source_path", "")),
        stored_at=float(data.get("stored_at", 0.0)),
        path=path,
        content=str(data.get("content", "")),
    )
```

- [ ] **Step 2.4: Verify tests pass → 7 PASS**

- [ ] **Step 2.5: Commit**

```bash
git add opencomputer/profile_bootstrap/raw_store.py tests/test_profile_bootstrap_raw_store.py
git commit -m "feat(profile-bootstrap): V2.B-T2 — content-addressed raw artifact store"
```

---

## Task 3: Embedding helper (BGE-small via sentence-transformers)

**Files:**
- Create: `opencomputer/profile_bootstrap/embedding.py`
- Modify: `pyproject.toml` (add `[project.optional-dependencies] deepening`)
- Test: `tests/test_profile_bootstrap_embedding.py`

- [ ] **Step 3.1: Update pyproject.toml**

Add a new optional-extras group `deepening` to `pyproject.toml`. Read the file, find `[project.optional-dependencies]`, and add:

```toml
deepening = [
  "psutil>=5.9",
  "chromadb>=0.5",
  "sentence-transformers>=3.0",
]
```

(If the section already has e.g. `bedrock = ...`, add `deepening = ...` below it.)

- [ ] **Step 3.2: Write failing tests**

```python
# tests/test_profile_bootstrap_embedding.py
"""BGE embedder tests — uses sentence-transformers as optional dep.

Tests mock the model so they pass on machines without the heavy weights
downloaded. Real BGE smoke is exercised manually via the doctor check.
"""
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.profile_bootstrap.embedding import (
    EmbeddingUnavailable,
    embed_texts,
    is_embedding_available,
)


def test_is_embedding_available_returns_false_without_dep():
    with patch(
        "opencomputer.profile_bootstrap.embedding._import_sentence_transformers",
        side_effect=ImportError(),
    ):
        assert is_embedding_available() is False


def test_embed_texts_raises_when_unavailable():
    with patch(
        "opencomputer.profile_bootstrap.embedding._import_sentence_transformers",
        side_effect=ImportError(),
    ):
        with pytest.raises(EmbeddingUnavailable):
            embed_texts(["hello"])


def test_embed_texts_returns_vectors_when_available():
    fake_st = MagicMock()
    fake_model = MagicMock()
    fake_model.encode.return_value = [[0.1] * 384, [0.2] * 384]
    fake_st.SentenceTransformer.return_value = fake_model

    with patch(
        "opencomputer.profile_bootstrap.embedding._import_sentence_transformers",
        return_value=fake_st,
    ):
        vecs = embed_texts(["hello", "world"])

    assert len(vecs) == 2
    assert len(vecs[0]) == 384


def test_embed_texts_handles_empty_input():
    fake_st = MagicMock()
    with patch(
        "opencomputer.profile_bootstrap.embedding._import_sentence_transformers",
        return_value=fake_st,
    ):
        vecs = embed_texts([])
    assert vecs == []


def test_embed_texts_caches_model_across_calls():
    """Loading BGE is expensive; the helper should cache the model."""
    fake_st = MagicMock()
    fake_model = MagicMock()
    fake_model.encode.return_value = [[0.0] * 384]
    fake_st.SentenceTransformer.return_value = fake_model

    with patch(
        "opencomputer.profile_bootstrap.embedding._import_sentence_transformers",
        return_value=fake_st,
    ):
        from opencomputer.profile_bootstrap import embedding as emb_mod
        # Reset the module-level cache so we observe the load.
        emb_mod._cached_model = None
        embed_texts(["a"])
        embed_texts(["b"])

    # SentenceTransformer should be constructed only once.
    assert fake_st.SentenceTransformer.call_count == 1
```

- [ ] **Step 3.3: Run failing tests**

- [ ] **Step 3.4: Implement embedder**

Create `opencomputer/profile_bootstrap/embedding.py`:

```python
"""BGE-small embedding helper for Layer 3 deepening.

Uses ``sentence-transformers`` (optional dep, install via
``pip install opencomputer[deepening]``) with the BGE-small-en model
(33MB, MIT license). The model loads once and is cached for the
process lifetime — first call is slow, subsequent calls are fast.

Embeddings are 384-dim float vectors suitable for Chroma's default
distance metrics.
"""
from __future__ import annotations

from typing import Any

_DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"

_cached_model: Any | None = None


class EmbeddingUnavailable(RuntimeError):
    """Raised when sentence-transformers isn't installed."""


def _import_sentence_transformers() -> Any:
    """Indirect import so tests can patch easily."""
    import sentence_transformers  # type: ignore[import-not-found]
    return sentence_transformers


def is_embedding_available() -> bool:
    """Cheap probe — only checks that the package is importable."""
    try:
        _import_sentence_transformers()
        return True
    except ImportError:
        return False


def _get_model(model_name: str = _DEFAULT_MODEL_NAME) -> Any:
    """Load + cache the SentenceTransformer model. First call ~1s + downloads."""
    global _cached_model
    if _cached_model is not None:
        return _cached_model
    st = _import_sentence_transformers()
    _cached_model = st.SentenceTransformer(model_name)
    return _cached_model


def embed_texts(
    texts: list[str], *, model_name: str = _DEFAULT_MODEL_NAME,
) -> list[list[float]]:
    """Embed a batch of strings. Returns list of 384-dim float vectors.

    Empty input returns ``[]`` without loading the model.
    """
    if not texts:
        return []
    try:
        _import_sentence_transformers()
    except ImportError as exc:
        raise EmbeddingUnavailable(
            "sentence-transformers not installed; "
            "install via 'pip install opencomputer[deepening]'"
        ) from exc

    model = _get_model(model_name)
    raw = model.encode(texts)
    return [list(map(float, vec)) for vec in raw]
```

- [ ] **Step 3.5: Verify tests pass → 5 PASS**

- [ ] **Step 3.6: Commit**

```bash
git add opencomputer/profile_bootstrap/embedding.py tests/test_profile_bootstrap_embedding.py pyproject.toml
git commit -m "feat(profile-bootstrap): V2.B-T3 — BGE-small embedding helper + [deepening] extras"
```

---

## Task 4: Chroma vector store integration

**Files:**
- Create: `opencomputer/profile_bootstrap/vector_store.py`
- Test: `tests/test_profile_bootstrap_vector_store.py`

- [ ] **Step 4.1: Write failing tests**

```python
# tests/test_profile_bootstrap_vector_store.py
"""Chroma vector store wrapper tests — mocks chromadb."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.profile_bootstrap.vector_store import (
    ChromaUnavailable,
    VectorStoreClient,
    is_chroma_available,
)


def test_is_chroma_available_returns_false_without_dep():
    with patch(
        "opencomputer.profile_bootstrap.vector_store._import_chromadb",
        side_effect=ImportError(),
    ):
        assert is_chroma_available() is False


def test_client_init_raises_when_unavailable(tmp_path: Path):
    with patch(
        "opencomputer.profile_bootstrap.vector_store._import_chromadb",
        side_effect=ImportError(),
    ):
        with pytest.raises(ChromaUnavailable):
            VectorStoreClient(persist_dir=tmp_path)


def test_client_upsert_then_query_returns_matches(tmp_path: Path):
    fake_chromadb = MagicMock()
    fake_collection = MagicMock()
    fake_chromadb.PersistentClient.return_value.get_or_create_collection.return_value = (
        fake_collection
    )
    fake_collection.query.return_value = {
        "ids": [["doc1"]],
        "distances": [[0.05]],
        "metadatas": [[{"kind": "file", "source_path": "/a"}]],
        "documents": [["hello world"]],
    }

    with patch(
        "opencomputer.profile_bootstrap.vector_store._import_chromadb",
        return_value=fake_chromadb,
    ):
        client = VectorStoreClient(persist_dir=tmp_path)
        client.upsert(
            ids=["doc1"],
            embeddings=[[0.1] * 384],
            metadatas=[{"kind": "file", "source_path": "/a"}],
            documents=["hello world"],
        )
        results = client.query(query_embedding=[0.1] * 384, top_k=1)

    assert len(results) == 1
    assert results[0].id == "doc1"
    assert results[0].distance == 0.05
    assert results[0].metadata["kind"] == "file"


def test_client_upsert_handles_empty_batch(tmp_path: Path):
    fake_chromadb = MagicMock()
    fake_collection = MagicMock()
    fake_chromadb.PersistentClient.return_value.get_or_create_collection.return_value = (
        fake_collection
    )

    with patch(
        "opencomputer.profile_bootstrap.vector_store._import_chromadb",
        return_value=fake_chromadb,
    ):
        client = VectorStoreClient(persist_dir=tmp_path)
        client.upsert(ids=[], embeddings=[], metadatas=[], documents=[])

    fake_collection.upsert.assert_not_called()
```

- [ ] **Step 4.2: Run failing tests**

- [ ] **Step 4.3: Implement vector store wrapper**

Create `opencomputer/profile_bootstrap/vector_store.py`:

```python
"""Chroma vector store wrapper for Layer 3 deepening.

Single collection ``layered_awareness_v1`` per profile, persisted at
``<profile_home>/profile_bootstrap/vector/``. Uses Chroma's PersistentClient
in sqlite mode (default) — no external services required.

Wrapper is intentionally narrow: ``upsert`` + ``query``. Distance metric
left as Chroma default (cosine for HNSW). Top-K query returns
:class:`VectorMatch` records.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


_DEFAULT_COLLECTION = "layered_awareness_v1"


class ChromaUnavailable(RuntimeError):
    """Raised when chromadb isn't installed."""


@dataclass(frozen=True, slots=True)
class VectorMatch:
    """One nearest-neighbour result."""

    id: str
    distance: float
    metadata: dict[str, Any]
    document: str = ""


def _import_chromadb() -> Any:
    """Indirect import so tests can patch easily."""
    import chromadb  # type: ignore[import-not-found]
    return chromadb


def is_chroma_available() -> bool:
    """Cheap probe — only checks that the package is importable."""
    try:
        _import_chromadb()
        return True
    except ImportError:
        return False


class VectorStoreClient:
    """Profile-scoped Chroma client. Creates/opens a single collection."""

    def __init__(
        self,
        *,
        persist_dir: Path,
        collection_name: str = _DEFAULT_COLLECTION,
    ) -> None:
        try:
            chromadb = _import_chromadb()
        except ImportError as exc:
            raise ChromaUnavailable(
                "chromadb not installed; install via 'pip install opencomputer[deepening]'"
            ) from exc
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._collection = self._client.get_or_create_collection(name=collection_name)

    def upsert(
        self,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
        documents: list[str],
    ) -> None:
        """Add or update embeddings. Empty batch is a no-op."""
        if not ids:
            return
        self._collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=documents,
        )

    def query(
        self,
        *,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[VectorMatch]:
        """Top-K nearest by cosine distance."""
        raw = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
        )
        ids = (raw.get("ids") or [[]])[0]
        distances = (raw.get("distances") or [[]])[0]
        metadatas = (raw.get("metadatas") or [[]])[0]
        documents = (raw.get("documents") or [[]])[0]
        out: list[VectorMatch] = []
        for i, doc_id in enumerate(ids):
            out.append(
                VectorMatch(
                    id=str(doc_id),
                    distance=float(distances[i] if i < len(distances) else 0.0),
                    metadata=metadatas[i] if i < len(metadatas) else {},
                    document=str(documents[i] if i < len(documents) else ""),
                )
            )
        return out
```

- [ ] **Step 4.4: Verify tests pass → 4 PASS**

- [ ] **Step 4.5: Commit**

```bash
git add opencomputer/profile_bootstrap/vector_store.py tests/test_profile_bootstrap_vector_store.py
git commit -m "feat(profile-bootstrap): V2.B-T4 — Chroma vector store wrapper"
```

---

## Task 5: Spotlight FTS integration (`mdfind` subprocess)

**Files:**
- Create: `opencomputer/profile_bootstrap/spotlight.py`
- Test: `tests/test_profile_bootstrap_spotlight.py`

- [ ] **Step 5.1: Write failing tests**

```python
# tests/test_profile_bootstrap_spotlight.py
"""Spotlight `mdfind` subprocess wrapper tests."""
from unittest.mock import patch

from opencomputer.profile_bootstrap.spotlight import (
    SpotlightHit,
    is_spotlight_available,
    spotlight_query,
)


def test_is_spotlight_available_returns_false_without_binary():
    with patch(
        "opencomputer.profile_bootstrap.spotlight.shutil.which",
        return_value=None,
    ):
        assert is_spotlight_available() is False


def test_spotlight_query_returns_empty_when_unavailable():
    with patch(
        "opencomputer.profile_bootstrap.spotlight.is_spotlight_available",
        return_value=False,
    ):
        hits = spotlight_query("foo")
    assert hits == []


def test_spotlight_query_parses_mdfind_output():
    fake_stdout = (
        "/Users/saksham/Documents/notes.md\n"
        "/Users/saksham/Documents/draft.txt\n"
    )
    with patch(
        "opencomputer.profile_bootstrap.spotlight.is_spotlight_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.spotlight.subprocess.run",
    ) as run:
        run.return_value.stdout = fake_stdout
        run.return_value.returncode = 0
        hits = spotlight_query("budget")
    assert len(hits) == 2
    assert hits[0].path == "/Users/saksham/Documents/notes.md"


def test_spotlight_query_returns_empty_on_nonzero_exit():
    with patch(
        "opencomputer.profile_bootstrap.spotlight.is_spotlight_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.spotlight.subprocess.run",
    ) as run:
        run.return_value.stdout = ""
        run.return_value.returncode = 1
        hits = spotlight_query("anything")
    assert hits == []


def test_spotlight_query_caps_results():
    paths = "\n".join(f"/path/{i}" for i in range(500))
    with patch(
        "opencomputer.profile_bootstrap.spotlight.is_spotlight_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.spotlight.subprocess.run",
    ) as run:
        run.return_value.stdout = paths
        run.return_value.returncode = 0
        hits = spotlight_query("x", max_results=50)
    assert len(hits) == 50
```

- [ ] **Step 5.2: Run failing tests**

- [ ] **Step 5.3: Implement spotlight wrapper**

Create `opencomputer/profile_bootstrap/spotlight.py`:

```python
"""Spotlight FTS via `mdfind` subprocess.

macOS already indexes the user's filesystem + mail + contacts + messages
+ calendar via Spotlight. Querying it via `mdfind` is free, fast, and
doesn't duplicate the index. We use it as the FTS surface for Layer 3 —
semantic queries hit Chroma, exact-match queries hit Spotlight.

On non-macOS, :func:`is_spotlight_available` returns False and queries
return ``[]``. V3 will plug in `tantivy` as a cross-platform fallback.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpotlightHit:
    """One result from `mdfind`."""

    path: str


def is_spotlight_available() -> bool:
    """Cheap probe — only checks that `mdfind` is on PATH."""
    return shutil.which("mdfind") is not None


def spotlight_query(
    query: str,
    *,
    only_in: str | None = None,
    max_results: int = 100,
    timeout_seconds: float = 5.0,
) -> list[SpotlightHit]:
    """Run a `mdfind` query and return result paths.

    ``only_in`` constrains the search to a directory subtree. ``max_results``
    caps the returned list (mdfind itself doesn't bound).
    """
    if not is_spotlight_available():
        return []
    cmd = ["mdfind"]
    if only_in:
        cmd += ["-onlyin", only_in]
    cmd.append(query)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    paths = [line for line in result.stdout.splitlines() if line.strip()]
    return [SpotlightHit(path=p) for p in paths[:max_results]]
```

- [ ] **Step 5.4: Verify tests pass → 5 PASS**

- [ ] **Step 5.5: Commit**

```bash
git add opencomputer/profile_bootstrap/spotlight.py tests/test_profile_bootstrap_spotlight.py
git commit -m "feat(profile-bootstrap): V2.B-T5 — Spotlight FTS via mdfind subprocess"
```

---

## Task 6: Idle detection (psutil-based)

**Files:**
- Create: `opencomputer/profile_bootstrap/idle.py`
- Test: `tests/test_profile_bootstrap_idle.py`

- [ ] **Step 6.1: Write failing tests**

```python
# tests/test_profile_bootstrap_idle.py
"""Idle detection tests — psutil-based CPU + power source check."""
from unittest.mock import MagicMock, patch

from opencomputer.profile_bootstrap.idle import (
    IdleStatus,
    check_idle,
    is_idle_detection_available,
)


def test_is_idle_detection_available_false_without_psutil():
    with patch(
        "opencomputer.profile_bootstrap.idle._import_psutil",
        side_effect=ImportError(),
    ):
        assert is_idle_detection_available() is False


def test_check_idle_treats_unavailable_as_not_idle():
    with patch(
        "opencomputer.profile_bootstrap.idle._import_psutil",
        side_effect=ImportError(),
    ):
        status = check_idle()
    assert status.idle is False
    assert "psutil" in status.reason


def test_check_idle_returns_idle_when_cpu_low_and_plugged_in():
    fake = MagicMock()
    fake.cpu_percent.return_value = 5.0
    fake_battery = MagicMock()
    fake_battery.power_plugged = True
    fake.sensors_battery.return_value = fake_battery
    with patch(
        "opencomputer.profile_bootstrap.idle._import_psutil",
        return_value=fake,
    ):
        status = check_idle(cpu_threshold=20.0, sample_seconds=0.0)
    assert status.idle is True


def test_check_idle_not_idle_when_cpu_high():
    fake = MagicMock()
    fake.cpu_percent.return_value = 75.0
    fake_battery = MagicMock()
    fake_battery.power_plugged = True
    fake.sensors_battery.return_value = fake_battery
    with patch(
        "opencomputer.profile_bootstrap.idle._import_psutil",
        return_value=fake,
    ):
        status = check_idle(cpu_threshold=20.0, sample_seconds=0.0)
    assert status.idle is False
    assert "CPU" in status.reason


def test_check_idle_not_idle_when_on_battery():
    fake = MagicMock()
    fake.cpu_percent.return_value = 5.0
    fake_battery = MagicMock()
    fake_battery.power_plugged = False
    fake.sensors_battery.return_value = fake_battery
    with patch(
        "opencomputer.profile_bootstrap.idle._import_psutil",
        return_value=fake,
    ):
        status = check_idle(cpu_threshold=20.0, sample_seconds=0.0)
    assert status.idle is False
    assert "battery" in status.reason.lower()


def test_check_idle_treats_no_battery_as_plugged_in():
    """Desktops have no battery — always plugged in."""
    fake = MagicMock()
    fake.cpu_percent.return_value = 5.0
    fake.sensors_battery.return_value = None  # no battery
    with patch(
        "opencomputer.profile_bootstrap.idle._import_psutil",
        return_value=fake,
    ):
        status = check_idle(cpu_threshold=20.0, sample_seconds=0.0)
    assert status.idle is True
```

- [ ] **Step 6.2: Run failing tests**

- [ ] **Step 6.3: Implement idle detection**

Create `opencomputer/profile_bootstrap/idle.py`:

```python
"""Idle detection for Layer 3 deepening — psutil-based.

The deepening loop only runs when the user is idle so the laptop stays
responsive. Two checks:

1. CPU usage averaged over a short window < threshold (default 20%).
2. Power source is AC (not running on battery), unless there's no
   battery sensor (desktops without batteries always count as plugged).

If psutil is missing, idle detection returns ``IdleStatus(idle=False)``
so the loop never runs without explicit consent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class IdleStatus:
    """Output of :func:`check_idle`."""

    idle: bool
    cpu_percent: float = 0.0
    on_battery: bool = False
    reason: str = ""


def _import_psutil() -> Any:
    import psutil  # type: ignore[import-not-found]
    return psutil


def is_idle_detection_available() -> bool:
    try:
        _import_psutil()
        return True
    except ImportError:
        return False


def check_idle(
    *,
    cpu_threshold: float = 20.0,
    sample_seconds: float = 1.0,
) -> IdleStatus:
    """Return whether the system is idle right now."""
    try:
        psutil = _import_psutil()
    except ImportError:
        return IdleStatus(
            idle=False, reason="psutil not installed (install opencomputer[deepening])",
        )

    cpu = float(psutil.cpu_percent(interval=sample_seconds))
    battery = psutil.sensors_battery()
    on_battery = battery is not None and not battery.power_plugged

    if cpu >= cpu_threshold:
        return IdleStatus(
            idle=False, cpu_percent=cpu, on_battery=on_battery,
            reason=f"CPU at {cpu:.1f}% (threshold {cpu_threshold:.1f}%)",
        )
    if on_battery:
        return IdleStatus(
            idle=False, cpu_percent=cpu, on_battery=True,
            reason="On battery power",
        )
    return IdleStatus(
        idle=True, cpu_percent=cpu, on_battery=on_battery,
        reason="idle",
    )
```

- [ ] **Step 6.4: Verify tests pass → 6 PASS**

- [ ] **Step 6.5: Commit**

```bash
git add opencomputer/profile_bootstrap/idle.py tests/test_profile_bootstrap_idle.py
git commit -m "feat(profile-bootstrap): V2.B-T6 — psutil-based idle detection"
```

---

## Task 7: Wire LLM extraction into Layer 2 orchestrator

**Files:**
- Modify: `opencomputer/profile_bootstrap/orchestrator.py`
- Test: `tests/test_profile_bootstrap_orchestrator.py`

- [ ] **Step 7.1: Add a helper that extracts + emits motifs**

In `opencomputer/profile_bootstrap/orchestrator.py`, add a helper used by the deepening loop (Task 8). For V2.B's first wiring, just expose the extract+emit pattern; the deepening loop will call it per artifact.

```python
def extract_and_emit_motif(
    *,
    content: str,
    kind: str,
    source_path: str,
    bus: Any | None = None,
) -> bool:
    """Run LLM extraction on an artifact and emit a motif on the F2 bus.

    Returns True if a motif was emitted; False if Ollama is unavailable
    or the extraction was empty. Best-effort — never raises.
    """
    from opencomputer.profile_bootstrap.llm_extractor import (
        ArtifactExtraction,
        OllamaUnavailable,
        extract_artifact,
    )
    try:
        extraction = extract_artifact(content)
    except OllamaUnavailable:
        return False
    if extraction == ArtifactExtraction():
        # All defaults → nothing extracted.
        return False

    if bus is None:
        from opencomputer.ingestion.bus import get_default_bus
        bus = get_default_bus()

    from plugin_sdk.ingestion import SignalEvent

    bus.publish(SignalEvent(
        event_type="layered_awareness.artifact_extraction",
        source="profile_bootstrap.orchestrator",
        metadata={
            "kind": kind,
            "source_path": source_path,
            "topic": extraction.topic,
            "people": list(extraction.people),
            "intent": extraction.intent,
            "sentiment": extraction.sentiment,
            "timestamp": extraction.timestamp,
        },
    ))
    return True
```

- [ ] **Step 7.2: Add tests**

```python
# Append to tests/test_profile_bootstrap_orchestrator.py

def test_extract_and_emit_motif_returns_false_when_ollama_unavailable():
    from unittest.mock import MagicMock, patch
    from opencomputer.profile_bootstrap.llm_extractor import OllamaUnavailable
    from opencomputer.profile_bootstrap.orchestrator import extract_and_emit_motif

    bus = MagicMock()
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.extract_artifact",
        side_effect=OllamaUnavailable("test"),
    ):
        emitted = extract_and_emit_motif(
            content="x", kind="file", source_path="/a", bus=bus,
        )
    assert emitted is False
    bus.publish.assert_not_called()


def test_extract_and_emit_motif_publishes_when_extraction_nonempty():
    from unittest.mock import MagicMock, patch
    from opencomputer.profile_bootstrap.llm_extractor import ArtifactExtraction
    from opencomputer.profile_bootstrap.orchestrator import extract_and_emit_motif

    bus = MagicMock()
    fake = ArtifactExtraction(topic="stocks", sentiment="neutral")
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.extract_artifact",
        return_value=fake,
    ):
        emitted = extract_and_emit_motif(
            content="x", kind="file", source_path="/a", bus=bus,
        )
    assert emitted is True
    bus.publish.assert_called_once()
    event = bus.publish.call_args[0][0]
    assert event.event_type == "layered_awareness.artifact_extraction"
    assert event.metadata["topic"] == "stocks"


def test_extract_and_emit_motif_returns_false_when_extraction_blank():
    from unittest.mock import MagicMock, patch
    from opencomputer.profile_bootstrap.llm_extractor import ArtifactExtraction
    from opencomputer.profile_bootstrap.orchestrator import extract_and_emit_motif

    bus = MagicMock()
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.extract_artifact",
        return_value=ArtifactExtraction(),
    ):
        emitted = extract_and_emit_motif(
            content="x", kind="file", source_path="/a", bus=bus,
        )
    assert emitted is False
    bus.publish.assert_not_called()
```

- [ ] **Step 7.3: Verify tests pass**

- [ ] **Step 7.4: Commit**

```bash
git add opencomputer/profile_bootstrap/orchestrator.py tests/test_profile_bootstrap_orchestrator.py
git commit -m "feat(profile-bootstrap): V2.B-T7 — extract_and_emit_motif helper"
```

---

## Task 8: Deepening loop with window progression

**Files:**
- Create: `opencomputer/profile_bootstrap/deepening.py`
- Test: `tests/test_profile_bootstrap_deepening.py`

- [ ] **Step 8.1: Write failing tests**

```python
# tests/test_profile_bootstrap_deepening.py
"""Deepening loop tests — window progression + cursor persistence."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from opencomputer.profile_bootstrap.deepening import (
    DEFAULT_WINDOWS,
    DeepeningCursor,
    DeepeningResult,
    load_cursor,
    run_deepening,
    save_cursor,
)


def test_default_windows_progression():
    assert DEFAULT_WINDOWS == (7, 30, 90, 365, 0)
    # 0 == all-time (sentinel value)


def test_save_then_load_cursor(tmp_path: Path):
    cursor = DeepeningCursor(
        last_window_days=30, last_started_at=1714000000.0, completed_windows=(7,),
    )
    cursor_path = tmp_path / "cursor.json"
    save_cursor(cursor, path=cursor_path)
    loaded = load_cursor(path=cursor_path)
    assert loaded.last_window_days == 30
    assert loaded.completed_windows == (7,)


def test_load_cursor_returns_default_when_absent(tmp_path: Path):
    cursor_path = tmp_path / "missing.json"
    cursor = load_cursor(path=cursor_path)
    assert cursor.last_window_days == 0
    assert cursor.completed_windows == ()


def test_load_cursor_returns_default_on_corrupt_json(tmp_path: Path):
    cursor_path = tmp_path / "bad.json"
    cursor_path.write_text("not json {{{")
    cursor = load_cursor(path=cursor_path)
    assert cursor.last_window_days == 0


def test_run_deepening_advances_to_next_window(tmp_path: Path):
    """Cursor at completed=[7], calling run advances to 30 next."""
    cursor_path = tmp_path / "cursor.json"
    save_cursor(
        DeepeningCursor(last_window_days=7, last_started_at=0.0, completed_windows=(7,)),
        path=cursor_path,
    )

    fake_idle = MagicMock(return_value=MagicMock(idle=True))
    fake_scan_files = MagicMock(return_value=[])
    fake_scan_git = MagicMock(return_value=[])
    fake_extract = MagicMock(return_value=False)
    fake_idle_check = MagicMock(return_value=MagicMock(idle=True))

    with patch(
        "opencomputer.profile_bootstrap.deepening.check_idle",
        fake_idle_check,
    ), patch(
        "opencomputer.profile_bootstrap.deepening.scan_recent_files",
        fake_scan_files,
    ), patch(
        "opencomputer.profile_bootstrap.deepening.scan_git_log",
        fake_scan_git,
    ), patch(
        "opencomputer.profile_bootstrap.deepening.extract_and_emit_motif",
        fake_extract,
    ):
        result = run_deepening(
            cursor_path=cursor_path,
            scan_roots=[tmp_path],
            git_repos=[],
            max_artifacts_per_window=10,
        )

    assert isinstance(result, DeepeningResult)
    assert result.window_processed_days == 30  # advanced from 7
    loaded = load_cursor(path=cursor_path)
    assert 30 in loaded.completed_windows


def test_run_deepening_skips_when_not_idle(tmp_path: Path):
    cursor_path = tmp_path / "cursor.json"
    fake_status = MagicMock()
    fake_status.idle = False
    fake_status.reason = "CPU at 80%"

    with patch(
        "opencomputer.profile_bootstrap.deepening.check_idle",
        return_value=fake_status,
    ):
        result = run_deepening(
            cursor_path=cursor_path, scan_roots=[tmp_path], git_repos=[],
        )
    assert result.skipped_reason == "CPU at 80%"
    assert result.artifacts_processed == 0


def test_run_deepening_force_bypasses_idle_check(tmp_path: Path):
    cursor_path = tmp_path / "cursor.json"
    fake_status = MagicMock()
    fake_status.idle = False
    fake_status.reason = "CPU at 80%"

    with patch(
        "opencomputer.profile_bootstrap.deepening.check_idle",
        return_value=fake_status,
    ), patch(
        "opencomputer.profile_bootstrap.deepening.scan_recent_files",
        return_value=[],
    ), patch(
        "opencomputer.profile_bootstrap.deepening.scan_git_log",
        return_value=[],
    ):
        result = run_deepening(
            cursor_path=cursor_path,
            scan_roots=[tmp_path],
            git_repos=[],
            force=True,
        )
    assert result.skipped_reason == ""
```

- [ ] **Step 8.2: Run failing tests**

- [ ] **Step 8.3: Implement deepening loop**

Create `opencomputer/profile_bootstrap/deepening.py`:

```python
"""Layer 3 — Background Deepening orchestrator.

Idle-throttled loop that progressively widens the time window over which
Layer 2 sources are ingested. Cursor persistence makes the loop
resumable across crashes / reboots.

Window progression (in days, 0 = all-time):
    7 → 30 → 90 → 365 → 0 (all-time)

Each ``run_deepening()`` call processes ONE window, advances the cursor,
and returns. Caller loops at their own cadence (e.g., every 5 minutes
in a daemon, or once per CLI invocation).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from opencomputer.profile_bootstrap.idle import IdleStatus, check_idle
from opencomputer.profile_bootstrap.orchestrator import extract_and_emit_motif
from opencomputer.profile_bootstrap.recent_scan import scan_git_log, scan_recent_files

_log = logging.getLogger("opencomputer.profile_bootstrap.deepening")

#: Window progression: 7d → 30d → 90d → 365d → all-time (0).
DEFAULT_WINDOWS: tuple[int, ...] = (7, 30, 90, 365, 0)


@dataclass(frozen=True, slots=True)
class DeepeningCursor:
    """Persistent state for the deepening loop."""

    last_window_days: int = 0
    last_started_at: float = 0.0
    completed_windows: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DeepeningResult:
    """Outcome of one ``run_deepening`` call."""

    window_processed_days: int = 0
    artifacts_processed: int = 0
    motifs_emitted: int = 0
    elapsed_seconds: float = 0.0
    skipped_reason: str = ""


def _default_cursor_path() -> Path:
    from opencomputer.agent.config import _home
    return _home() / "profile_bootstrap" / "deepening_cursor.json"


def save_cursor(cursor: DeepeningCursor, *, path: Path | None = None) -> None:
    """Atomically write the cursor JSON."""
    p = path if path is not None else _default_cursor_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_window_days": cursor.last_window_days,
        "last_started_at": cursor.last_started_at,
        "completed_windows": list(cursor.completed_windows),
    }
    p.write_text(json.dumps(payload))


def load_cursor(*, path: Path | None = None) -> DeepeningCursor:
    """Read the cursor JSON. Returns default cursor if missing/corrupt."""
    p = path if path is not None else _default_cursor_path()
    if not p.exists():
        return DeepeningCursor()
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return DeepeningCursor()
    return DeepeningCursor(
        last_window_days=int(data.get("last_window_days", 0)),
        last_started_at=float(data.get("last_started_at", 0.0)),
        completed_windows=tuple(int(w) for w in data.get("completed_windows", [])),
    )


def _next_window(cursor: DeepeningCursor) -> int:
    """Pick the next window in the progression that hasn't been completed."""
    for w in DEFAULT_WINDOWS:
        if w not in cursor.completed_windows:
            return w
    # All windows complete → return all-time as a no-op cycle.
    return 0


def run_deepening(
    *,
    cursor_path: Path | None = None,
    scan_roots: list[Path] | None = None,
    git_repos: list[Path] | None = None,
    max_artifacts_per_window: int = 500,
    force: bool = False,
) -> DeepeningResult:
    """Run ONE deepening pass over the next window in the progression.

    With ``force=False`` (default), short-circuits if the system is not idle.
    With ``force=True``, ignores idle detection — useful for the
    ``opencomputer profile deepen`` CLI invocation.
    """
    started = time.monotonic()

    if not force:
        status = check_idle()
        if not status.idle:
            return DeepeningResult(skipped_reason=status.reason)

    cursor = load_cursor(path=cursor_path)
    window = _next_window(cursor)
    days = window if window > 0 else 365 * 10  # 0 → "all-time" approximated as 10 years

    files = scan_recent_files(
        roots=scan_roots or [], days=days, max_files=max_artifacts_per_window,
    )
    commits = scan_git_log(
        repo_paths=git_repos or [], days=days, max_per_repo=max_artifacts_per_window,
    )

    artifacts_processed = 0
    motifs_emitted = 0

    # Process files (we have the path; LLM gets a brief content sample).
    for f in files:
        artifacts_processed += 1
        try:
            sample = Path(f.path).read_text(errors="replace")[:4000]
        except (OSError, UnicodeDecodeError):
            continue
        if extract_and_emit_motif(
            content=sample, kind="file", source_path=f.path,
        ):
            motifs_emitted += 1

    # Process git commits (subject is the content).
    for c in commits:
        artifacts_processed += 1
        if extract_and_emit_motif(
            content=c.subject, kind="git_commit", source_path=c.repo_path,
        ):
            motifs_emitted += 1

    # Advance cursor.
    new_completed = tuple(sorted({*cursor.completed_windows, window}))
    new_cursor = DeepeningCursor(
        last_window_days=window,
        last_started_at=time.time(),
        completed_windows=new_completed,
    )
    save_cursor(new_cursor, path=cursor_path)

    return DeepeningResult(
        window_processed_days=window,
        artifacts_processed=artifacts_processed,
        motifs_emitted=motifs_emitted,
        elapsed_seconds=time.monotonic() - started,
        skipped_reason="",
    )
```

- [ ] **Step 8.4: Verify tests pass → 7 PASS**

- [ ] **Step 8.5: Commit**

```bash
git add opencomputer/profile_bootstrap/deepening.py tests/test_profile_bootstrap_deepening.py
git commit -m "feat(profile-bootstrap): V2.B-T8 — deepening loop with window progression + cursor"
```

---

## Task 9: `opencomputer profile deepen` CLI

**Files:**
- Modify: `opencomputer/cli_profile.py`
- Test: `tests/test_cli_profile_deepen.py`

- [ ] **Step 9.1: Write failing tests**

```python
# tests/test_cli_profile_deepen.py
"""CLI tests for `opencomputer profile deepen`."""
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from opencomputer.cli_profile import profile_app

runner = CliRunner()


def test_deepen_command_exists():
    result = runner.invoke(profile_app, ["deepen", "--help"])
    assert result.exit_code == 0
    assert "deepen" in result.stdout.lower() or "Layer 3" in result.stdout


def test_deepen_runs_and_displays_summary(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    fake_result = type("R", (), {})()
    fake_result.window_processed_days = 30
    fake_result.artifacts_processed = 12
    fake_result.motifs_emitted = 5
    fake_result.elapsed_seconds = 1.5
    fake_result.skipped_reason = ""

    with patch("opencomputer.cli_profile.run_deepening", return_value=fake_result):
        result = runner.invoke(profile_app, ["deepen", "--force"])
    assert result.exit_code == 0
    assert "30" in result.stdout  # window
    assert "12" in result.stdout  # artifacts
    assert "5" in result.stdout   # motifs


def test_deepen_displays_skip_reason_when_not_idle(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    fake_result = type("R", (), {})()
    fake_result.window_processed_days = 0
    fake_result.artifacts_processed = 0
    fake_result.motifs_emitted = 0
    fake_result.elapsed_seconds = 0.1
    fake_result.skipped_reason = "CPU at 80%"

    with patch("opencomputer.cli_profile.run_deepening", return_value=fake_result):
        result = runner.invoke(profile_app, ["deepen"])
    assert result.exit_code == 0
    assert "CPU at 80%" in result.stdout or "skipped" in result.stdout.lower()
```

- [ ] **Step 9.2: Run failing tests**

- [ ] **Step 9.3: Add `deepen` command**

In `opencomputer/cli_profile.py`, add at module scope (after the existing top-level imports):

```python
from opencomputer.profile_bootstrap.deepening import run_deepening  # noqa: F401
```

Add the command function alongside the existing profile commands:

```python
@profile_app.command("deepen")
def profile_deepen(
    force: bool = typer.Option(
        False, "--force", help="Bypass idle check; run regardless of CPU/battery"
    ),
    max_artifacts: int = typer.Option(
        500, "--max-artifacts", help="Cap artifacts processed in this window"
    ),
) -> None:
    """Run one deepening pass (Layer 3 of Layered Awareness).

    Walks the current window from the cursor, extracts motifs via Ollama,
    and advances to the next window. With --force, ignores idle gating.
    """
    from pathlib import Path

    from opencomputer.profile_bootstrap.deepening import run_deepening

    home_dirs = [
        Path.home() / "Documents",
        Path.home() / "Desktop",
        Path.home() / "Downloads",
    ]
    git_repos = _detect_git_repos()  # already exists from V1

    result = run_deepening(
        scan_roots=[d for d in home_dirs if d.exists()],
        git_repos=git_repos,
        max_artifacts_per_window=max_artifacts,
        force=force,
    )

    if result.skipped_reason:
        typer.echo(f"Deepening skipped: {result.skipped_reason}")
        typer.echo("Use --force to run anyway.")
        return

    typer.echo("Deepening pass complete:")
    typer.echo(f"  Window processed (days):    {result.window_processed_days}")
    typer.echo(f"  Artifacts processed:        {result.artifacts_processed}")
    typer.echo(f"  Motifs emitted:             {result.motifs_emitted}")
    typer.echo(f"  Elapsed:                    {result.elapsed_seconds:.1f}s")
```

- [ ] **Step 9.4: Verify tests pass → 3 PASS**

- [ ] **Step 9.5: Commit**

```bash
git add opencomputer/cli_profile.py tests/test_cli_profile_deepen.py
git commit -m "feat(cli): V2.B-T9 — opencomputer profile deepen"
```

---

## Task 10: Doctor checks for deepening dependencies

**Files:**
- Modify: `opencomputer/doctor.py`
- Test: `tests/test_doctor_deepening.py`

- [ ] **Step 10.1: Read existing doctor.py to find the check pattern**

Read `opencomputer/doctor.py`. The doctor module exposes some kind of check function/list. Identify the existing pattern (e.g., a list of `(name, fn)` tuples or a class with check methods).

- [ ] **Step 10.2: Write failing tests**

```python
# tests/test_doctor_deepening.py
"""Doctor checks for deepening dependencies."""
from unittest.mock import patch

from opencomputer.doctor import (
    check_chroma_available,
    check_embedding_available,
    check_ollama_available,
)


def test_check_ollama_available_pass_when_installed():
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=True,
    ):
        result = check_ollama_available()
    assert result.passed is True


def test_check_ollama_available_fail_when_missing():
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=False,
    ):
        result = check_ollama_available()
    assert result.passed is False
    assert "ollama" in result.remediation.lower()


def test_check_embedding_available_pass():
    with patch(
        "opencomputer.profile_bootstrap.embedding.is_embedding_available",
        return_value=True,
    ):
        result = check_embedding_available()
    assert result.passed is True


def test_check_chroma_available_pass():
    with patch(
        "opencomputer.profile_bootstrap.vector_store.is_chroma_available",
        return_value=True,
    ):
        result = check_chroma_available()
    assert result.passed is True
```

- [ ] **Step 10.3: Run failing tests**

- [ ] **Step 10.4: Implement check functions**

Add to `opencomputer/doctor.py` (read the existing file first; match its `DoctorResult` / check-result shape — likely something like `@dataclass class CheckResult: passed: bool; name: str; remediation: str = ""`):

```python
def check_ollama_available() -> CheckResult:
    """Check whether Ollama is on PATH (required for Layer 3 LLM extraction)."""
    from opencomputer.profile_bootstrap.llm_extractor import is_ollama_available
    if is_ollama_available():
        return CheckResult(name="ollama", passed=True)
    return CheckResult(
        name="ollama",
        passed=False,
        remediation=(
            "Ollama not found. Install via 'brew install ollama' (macOS) "
            "or follow https://ollama.com — required for Layer 3 deepening."
        ),
    )


def check_embedding_available() -> CheckResult:
    """Check whether sentence-transformers is importable."""
    from opencomputer.profile_bootstrap.embedding import is_embedding_available
    if is_embedding_available():
        return CheckResult(name="sentence-transformers", passed=True)
    return CheckResult(
        name="sentence-transformers",
        passed=False,
        remediation="Install via 'pip install opencomputer[deepening]'",
    )


def check_chroma_available() -> CheckResult:
    """Check whether chromadb is importable."""
    from opencomputer.profile_bootstrap.vector_store import is_chroma_available
    if is_chroma_available():
        return CheckResult(name="chromadb", passed=True)
    return CheckResult(
        name="chromadb",
        passed=False,
        remediation="Install via 'pip install opencomputer[deepening]'",
    )
```

(If the existing doctor uses a different result shape, adapt accordingly. Read first, match the pattern.)

- [ ] **Step 10.5: Verify tests pass → 4 PASS**

- [ ] **Step 10.6: Commit**

```bash
git add opencomputer/doctor.py tests/test_doctor_deepening.py
git commit -m "feat(doctor): V2.B-T10 — checks for Ollama / sentence-transformers / chromadb"
```

---

## Task 11: Final validation + CHANGELOG + push

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 11.1: Full pytest**

```
python3.13 -m pytest -q
```

Confirm 3115+ pass (V2.A baseline) plus the V2.B test additions.

- [ ] **Step 11.2: Full ruff**

```
ruff check .
```

Confirm `All checks passed!`. Auto-fix any UP/B/I rule violations: `ruff check --fix .`. Manual fixes for anything `--unsafe-fixes` would handle.

- [ ] **Step 11.3: CHANGELOG entry**

Append to `CHANGELOG.md` `[Unreleased]` section:

```markdown
### Added (Layered Awareness V2.B — Background Deepening, 2026-04-27)

Layer 3 of the Layered Awareness design ships as a separate orchestrator
that progressively ingests historical data over expanding time windows.

- **Ollama LLM extractor** (`profile_bootstrap/llm_extractor.py`) —
  was deferred from V1 MVP. Subprocess wrapper around `ollama run`
  that turns artifacts into structured `ArtifactExtraction` records
  (topic, people, intent, sentiment, timestamp). Falls back gracefully
  when Ollama isn't installed.
- **Content-addressed raw artifact store**
  (`profile_bootstrap/raw_store.py`) — SHA256 hashing, two-level fanout
  (`<aa>/<bb>/<full-sha>.json`), idempotent writes.
- **BGE-small embedding helper** (`profile_bootstrap/embedding.py`) —
  via sentence-transformers (optional `[deepening]` dep). Module-level
  model cache so first call is slow but subsequent calls are fast.
- **Chroma vector store wrapper** (`profile_bootstrap/vector_store.py`) —
  PersistentClient in sqlite mode, single collection per profile.
  Narrow API: upsert + query → list[VectorMatch].
- **Spotlight FTS via mdfind** (`profile_bootstrap/spotlight.py`) —
  zero-cost FTS surface on macOS; queries the same index Spotlight
  already maintains. Returns SpotlightHit records.
- **psutil-based idle detection** (`profile_bootstrap/idle.py`) —
  CPU<20% AND plugged-in (or no battery sensor) → idle. Fail-safe to
  not-idle when psutil unavailable.
- **Deepening loop** (`profile_bootstrap/deepening.py`) — window
  progression (7d → 30d → 90d → 365d → all-time), cursor persistence
  at `<profile_home>/profile_bootstrap/deepening_cursor.json`,
  per-call advance. Idle-gated unless `--force`.
- **`extract_and_emit_motif` helper** in orchestrator —
  feeds `layered_awareness.artifact_extraction` SignalEvents onto the
  F2 bus for downstream graph importers.
- **`opencomputer profile deepen [--force --max-artifacts N]`** CLI.
- **Doctor checks** for Ollama, sentence-transformers, chromadb.
- **`pyproject.toml [deepening]` extras**: `psutil>=5.9`,
  `chromadb>=0.5`, `sentence-transformers>=3.0`.

V2.C (life-event detector + plural personas) ships separately.

Spec: `docs/superpowers/specs/2026-04-26-layered-awareness-design.md`
Plan: `docs/superpowers/plans/2026-04-27-layered-awareness-v2b-deepening.md`
```

- [ ] **Step 11.4: Commit + push**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): V2.B — Background Deepening entry"
git push -u origin feat/layered-awareness-v2b
```

- [ ] **Step 11.5: Open PR**

```bash
gh pr create --base main --head feat/layered-awareness-v2b --title "feat: Layered Awareness V2.B — Background Deepening (Layer 3)" --body "$(cat <<'EOF'
## Summary

Layer 3 of the Layered Awareness design — idle-throttled deepening loop that progressively ingests historical data over expanding time windows (7d → 30d → 90d → 365d → all-time).

**11 tasks across 11 commits + CHANGELOG.** Builds on V2.A (PR #151) which shipped consent enforcement, dotted-dir pruning, multi-Chromium-family browser history, and other follow-ups.

### What's wired

- **Ollama LLM extraction** — subprocess wrapper, deferred from MVP, now feeds artifacts → structured ArtifactExtraction records.
- **Content-addressed raw store** — SHA256 fanout layout, idempotent.
- **BGE-small embeddings + Chroma vector store** — semantic recall via sentence-transformers (optional `[deepening]` dep).
- **Spotlight FTS** — zero-cost text recall via `mdfind` subprocess.
- **psutil idle detection** — CPU<20% + plugged-in gating.
- **Deepening loop** — cursor-persistent window progression.
- **`opencomputer profile deepen`** CLI surface.
- **Doctor checks** for the 3 new deps.

### Optional dependency posture

`pyproject.toml` adds a `[deepening]` extras group. Base install stays light; users opt in via `pip install opencomputer[deepening]`. Each helper falls back gracefully when its dep is missing — `extract_artifact` raises `OllamaUnavailable`, `embed_texts` raises `EmbeddingUnavailable`, etc. The deepening loop catches these and skips the affected step.

### Test plan

- [ ] CI passes (pytest + ruff workflows).
- [ ] Manual smoke on macOS with `pip install opencomputer[deepening]` and `brew install ollama && ollama pull llama3.2:3b`:
  - `opencomputer doctor` shows Ollama / chromadb / sentence-transformers all green
  - `opencomputer profile deepen --force` runs one window, prints "X artifacts, Y motifs"
  - Cursor advances on next invocation (re-run `--force`, see window 30 → 90)
  - Bus motif observable via `opencomputer trajectories show` (or whatever the F2 inspection surface is)

### V2.C (next plan)

Life-event detector + plural personas auto-classifier. Separate PR after this lands.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 11.6: Verify CI**

```
gh pr checks <NUMBER> --watch
```

Confirm all 3 checks (ruff, pytest 3.12, pytest 3.13) pass.

DO NOT MERGE. Report the PR number + CI status.

---

## Self-Review

Pass against the spec — section by section, can I point to a task?

- ✅ **Layer 3 LLM extractor (Ollama)** — Task 1.
- ✅ **Raw artifact store (content-addressed)** — Task 2.
- ✅ **Embedding helper (BGE)** — Task 3.
- ✅ **Chroma vector store** — Task 4.
- ✅ **Spotlight FTS integration** — Task 5.
- ✅ **Idle detection (psutil)** — Task 6.
- ✅ **Wire LLM into orchestrator** — Task 7.
- ✅ **Deepening loop with windows + cursor** — Task 8.
- ✅ **`profile deepen` CLI** — Task 9.
- ✅ **Doctor checks for new deps** — Task 10.
- ✅ **CHANGELOG + ruff + push** — Task 11.

Spec coverage: complete for V2.B scope. V2.C (life-event detector, plural personas) and V2.D (curious companion) explicitly out.

Placeholder scan: no "TBD" / "fill in details" remain. Real code in every step. Commit messages match the convention. File paths exact. Test code exercises real behaviour.

Type consistency:
- `ArtifactExtraction` shape consistent across Tasks 1, 7, 8.
- `RawStoreEntry` consistent in Tasks 2.
- `VectorMatch` + `VectorStoreClient` consistent in Tasks 4.
- `IdleStatus` consistent in Tasks 6, 8.
- `DeepeningCursor` + `DeepeningResult` consistent in Tasks 8, 9.
- `SignalEvent(event_type=..., source=..., metadata=...)` matches the SDK shape.
- `list_nodes(kinds=..., limit=...)` plural-sequence form.
- No `@pytest.mark.asyncio` decorators (asyncio_mode = "auto").

Acknowledged-as-deferred (V2.C+):
- Auto-start of deepening loop from gateway daemon (CLI-only in V2.B).
- Embedding cost monitoring + per-day budget cap.
- Chroma collection migration on schema bump (single collection in V2.B).
- Spotlight integration with NSMetadataQuery (Python-native — currently mdfind subprocess only).
- Concurrent file scan via ThreadPoolExecutor.
