# memory-vector

ChromaDB-backed vector memory plugin for OpenComputer. Adds three tools:

- `VectorMemoryAdd(text, tags?)` → `{id}`
- `VectorMemorySearch(query, top_k=5)` → `{hits: [{id, text, score, metadata}]}`
- `VectorMemoryDelete(id)` → `{deleted: bool}`

Storage: `<profile-home>/memory-vector/chroma.db` (Chroma's
PersistentClient).

Embeddings: ChromaDB's default sentence-transformers
(`all-MiniLM-L6-v2`), lazy-installed. Add `chromadb[embeddings]` to
your environment to use the default; or configure ChromaDB-compatible
embedding functions directly.

## Install + enable

```bash
pip install chromadb
oc plugin enable memory-vector
```

The plugin is **not enabled by default** — chromadb is a heavy
dependency (sentence-transformers + torch). Opt in only when you need
vector recall.

## MVP scope (2026-05-05)

Shipped:

- Add/search/delete on a single per-profile collection.
- Profile isolation (each profile gets its own DB file).
- Persistence across sessions.
- Lazy ChromaDB import (no slowdown for users who don't enable).

Explicitly out of scope (open issues / future PRs):

- **Reindexing** on embedding-model schema change. If you switch the
  embedding model, drop the collection and re-add.
- **Eviction policies** — collection grows unbounded. Manual delete
  recommended.
- **Distributed sharding** across hosts.
- **Cross-profile sharing** — each profile's collection is fully
  isolated by design.
- **Custom distance metrics** — uses ChromaDB's default cosine
  similarity.

## Tests

```bash
pytest tests/test_memory_vector_backend.py -v
```

Tests use a fake-client factory; they don't pull chromadb at test time.
