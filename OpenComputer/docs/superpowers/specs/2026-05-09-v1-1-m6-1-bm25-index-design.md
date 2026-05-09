# M6.1 — `MEMORY.md` BM25 index — Design Spec

Date: 2026-05-09
Author: agent (executing v1.1 Plan 3)
Parent plan: `OpenComputer/docs/superpowers/plans/2026-05-08-v1-1-plan-3-heavy-features-and-parked.md`
Companion specs (deferred follow-up):
- M6.2 vector index — next PR
- M6.3 Active Memory pre-loop injection — after M6.2
- M6.4 Dreaming, M6.5 cap pressure, M6.6 embedding provider — demand-driven

## Why this scope and not Plan 3 in full

Plan 3 totals 5–7 weeks of work across three independent subsystems (M6 memory, M9 auto-mode, M10 routing). It explicitly refuses to skip the dogfood gate and refuses bundled landings. Plan 1's M0 v1.0 ship + M1.4 per-profile env are also not yet on `main`. M9 and M10 inherit those dependencies.

M6.1 is the only sub-milestone that:
- has zero dependency on Plan 1 or Plan 2 work,
- has zero parallel-session collision risk (no `memory_index*` / `*bm25*` files anywhere on `main` or in any active branch),
- ships internal infrastructure (no user-facing surface) so the dogfood gate does not gate it,
- is small enough (2-day scope) to land cleanly in a single session with TDD discipline,
- is a foundation for M6.2 (vector index) and M6.3 (Active Memory injection), so doing it now does not waste effort.

## Audit findings folded into this spec

A brainstorm-phase audit of Plan 3 surfaced four real holes. Two of them are inside M6.1's scope and are addressed here. The other two are outside M6.1's scope and are recorded at the bottom as carry-forward notes for later sub-milestones.

### Inside M6.1 — addressed in this spec

1. **Cache integrity** — Plan 3 says "Persisted at `~/.opencomputer/<profile>/cache/memory_bm25.idx` as a pickled corpus + tokenized form" with no integrity check. A truncated, corrupted, stale, or version-skewed cache file would silently return wrong results. This spec mandates a header `(format_version, corpus_sha256, entry_count)` validated on load; on mismatch the index rebuilds from `MEMORY.md` and overwrites the cache.
2. **FTS5-vs-pickle architectural choice** — Plan 3 picks pickled BM25 without justification when FTS5 is already used by `sessions.db`. This spec documents the choice: `MEMORY.md` is unstructured markdown with entries delimited by paragraph boundaries (not rows), today's corpus is small in practice (the user's MEMORY.md across multiple sessions has stayed under 32KB), and BM25's tokenization + ranking is the desired retrieval model rather than FTS5's lexer. Pickle is acceptable because rebuild from any plausible MEMORY.md size is sub-200ms even cold; M6.5 owns any future hard cap policy.

### Outside M6.1 — recorded as upstream notes for later sub-milestones

3. **M9.2 classifier-down failure mode** (security-critical; goes into the M9.2 spec when it is written): default = **fail-closed** (block tool dispatch on classifier error or timeout, do not allow). Telemetry surfaced via `oc audit verify`. Rationale: an attacker can DoS the classifier; failing open hands them tool execution.
4. **M6.4 Dreaming cron-miss policy** (goes into the M6.4 spec): if the last successful run is older than `2 × cron_interval`, run a catch-up pass at next cron invocation, capped at one catch-up per real run.
5. **M6.3 + Honcho prompt ordering** (goes into the M6.3 spec): fixed order is `[Honcho prefetch] → [Active Memory] → [user content]`. The Honcho block must come first to keep the prompt-cache prefix stable across the wider variability in Active Memory output.
6. **`BaseProvider.embed()` contract** (goes into the M6.6 spec): `embed(texts: list[str]) -> EmbeddingBatch` where `EmbeddingBatch` carries `vectors: list[list[float]]`, `dimensionality: int`, `model_id: str`, `cost_estimate_usd: float`. Batch size capped at 100. Providers that lack embeddings raise `EmbeddingsNotSupported`.

## Goal

Add a fast, reproducible, in-process BM25 search index over `MEMORY.md` so that future sub-milestones can retrieve the most-relevant declarative memory entries for a given query in <10ms warm.

## Non-goals

- Vector retrieval (M6.2)
- Pre-loop injection into the agent loop (M6.3)
- Auto-promotion of episodic events into `MEMORY.md` (M6.4 Dreaming)
- Cross-profile retrieval (each profile's `MEMORY.md` is its own corpus)
- Index of `USER.md`, `SOUL.md`, `CLAUDE.md`, or `DREAMS.md` (separate sub-milestones if a real demand surfaces)

## Architecture

### Data flow

```
MEMORY.md (~/.opencomputer/<profile>/MEMORY.md)
   │
   │   on first .query() / on cache mismatch
   ▼
Entry segmenter  (paragraph-delimited; preserves frontmatter and headings as boundary markers)
   │
   ▼
Tokenizer  (lowercase + r"[a-z0-9]+" word split; no stopwords, no stemming, v1)
   │
   ▼
BM25Okapi(corpus_tokens)  (rank_bm25 ^0.2.2)
   │
   ▼
PickleCache  (header + body)
   │   header: dict(format_version=1, corpus_sha256=..., entry_count=N, mtime_ns=...)
   │   body: dict(entries=[...raw text...], tokens=[[...], ...], bm25=BM25Okapi)
   ▼
~/.opencomputer/<profile>/cache/memory_bm25.idx
```

### Module placement

- New module: `opencomputer/agent/memory_index.py`
- Touch: `opencomputer/agent/memory.py` — instantiate one `BM25Index` per `MemoryManager`, expose via property, and call `invalidate()` after each successful declarative-MEMORY.md write. Concretely, the four call sites are `MemoryManager.append_declarative`, `replace_declarative`, `remove_declarative`, and `restore_backup(which="memory")`. They route through `_append`/`_replace`/`_remove`/`_write_atomic`; the simplest, most explicit hook is in the public methods themselves, after the underlying call returns successfully. (The `Memory` tool action map — `add`/`replace`/`remove` — is one layer above; hooking at `MemoryManager` covers both the tool path and any direct caller.)
- Touch: `pyproject.toml` — add `rank_bm25 = "^0.2.2"` to runtime `dependencies`.

### Public API

```python
# opencomputer/agent/memory_index.py

@dataclass(frozen=True)
class IndexedEntry:
    """One paragraph-delimited entry from MEMORY.md."""
    raw: str               # exact text as it appears in MEMORY.md
    line_start: int        # 1-indexed line of first non-blank char
    line_end: int          # 1-indexed line of last non-blank char


@dataclass(frozen=True)
class QueryHit:
    entry: IndexedEntry
    score: float           # BM25 score (raw, not normalized)
    rank: int              # 0-indexed position in result list


class BM25Index:
    """BM25 retrieval over MEMORY.md.  Profile-scoped, lazily built, cache-backed."""

    FORMAT_VERSION: int = 1
    CACHE_FILENAME: str = "memory_bm25.idx"

    def __init__(self, profile_home: Path) -> None:
        ...

    def query(self, text: str, top_k: int = 5) -> list[QueryHit]:
        """Return top_k entries by BM25 score; empty list if MEMORY.md is missing or empty."""
        ...

    def invalidate(self) -> None:
        """Drop in-memory state and remove on-disk cache.  Next query triggers rebuild."""
        ...

    # internals (not part of public API but referenced in tests)
    def _build(self) -> None: ...
    def _load_cache(self) -> bool: ...   # True iff valid cache loaded
    def _save_cache(self) -> None: ...
    def _segment(self, text: str) -> list[IndexedEntry]: ...
    @staticmethod
    def _tokenize(text: str) -> list[str]: ...
```

The class is per-profile-home. `MemoryManager` owns one instance and forwards `invalidate()` on writes. Tests construct `BM25Index(tmp_path)` directly to avoid touching the real profile home.

### Segmentation rules

1. Read `MEMORY.md` as UTF-8.
2. Split on **1-or-more blank lines** OR on markdown headings (`^#{1,6}\s`).
3. Drop empty entries.
4. Each entry retains its raw text including any inline markdown (links, code spans).

Rationale: typical agent-written MEMORY.md uses heading-bounded sections separated by single blank lines. A stricter "2+ blank lines" rule would collapse adjacent topics into one entry, hurting BM25 ranking precision. The trade-off: tightly-packed bullet lists (e.g., a "Memory Index" with no blank lines between bullets) collapse into one entry; this is acceptable for v1 because such structures are rare in the agent's own writes, and BM25 still surfaces them as a single high-recall hit.

### Tokenization

```python
@staticmethod
def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())
```

No stopwords (BM25's IDF handles term frequency naturally; stopwords would discard valid signals like "the team"). No stemming (v1; revisit only if real-use shows hits like "running" missing "run" queries). Numbers retained (versions like "v1.1" tokenize to `["v1", "1"]` which is acceptable).

### Cache format

`memory_bm25.idx` is a single pickle file with this structure:

```python
{
    "header": {
        "format_version": 1,
        "corpus_sha256": "<hex>",       # sha256 of MEMORY.md raw bytes at build time
        "entry_count": N,
        "mtime_ns": <int>,              # MEMORY.md st_mtime_ns at build time
        "built_at": <ISO8601 string>,
    },
    "entries": [IndexedEntry, ...],
    "tokens": [[token, ...], ...],       # parallel to entries
    "bm25": <pickled BM25Okapi instance>,
}
```

### Cache load logic

```python
def _load_cache(self) -> bool:
    if not self._cache_path.exists():
        return False
    try:
        with self._cache_path.open("rb") as f:
            data = pickle.load(f)
        header = data["header"]
        if header["format_version"] != self.FORMAT_VERSION:
            return False
        if header["corpus_sha256"] != self._current_sha256():
            return False
    except (pickle.UnpicklingError, KeyError, EOFError, OSError):
        return False
    self._entries = data["entries"]
    self._tokens = data["tokens"]
    self._bm25 = data["bm25"]
    return True
```

Rationale for sha256 + format_version: covers four failure modes — file truncated mid-write, file from a previous code version with a different schema, file from a different `MEMORY.md` (different profile copied across machines), or `MEMORY.md` edited externally without going through `MemoryManager`. mtime is recorded for debugging but not used for validation (unreliable across copies).

### Cache write logic

Atomic via rename-into-place pattern (consistent with M1.1 flock pattern from Plan 1):

```python
tmp = self._cache_path.with_suffix(".tmp")
with tmp.open("wb") as f:
    pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
os.replace(tmp, self._cache_path)
```

Atomic rename means a crashed write leaves either the old cache or no cache, never a torn one.

### Invalidation hooks in `MemoryManager`

`opencomputer/agent/memory.py` already centralizes MEMORY.md writes through `MemoryManager` and routes them through `_write_atomic` for durability. The four public write methods are the right invalidation points (one explicit call per method, after the underlying private helper returns success):

```python
# in agent/memory.py

class MemoryManager:
    def __init__(self, declarative_path: Path, ...):
        ...
        from opencomputer.agent.memory_index import BM25Index
        # MemoryManager already knows declarative_path; BM25Index lives in <profile_home>/cache/
        self._bm25_index = BM25Index(declarative_path.parent)

    @property
    def bm25_index(self) -> BM25Index:
        return self._bm25_index

    def append_declarative(self, text: str) -> None:
        self._append(self.declarative_path, text, limit=..., kind="memory")
        self._bm25_index.invalidate()

    def replace_declarative(self, old: str, new: str) -> bool:
        changed = self._replace(self.declarative_path, old, new, limit=..., kind="memory")
        if changed:
            self._bm25_index.invalidate()
        return changed

    def remove_declarative(self, block: str) -> bool:
        changed = self._remove(self.declarative_path, block, kind="memory")
        if changed:
            self._bm25_index.invalidate()
        return changed

    def restore_backup(self, which: Literal["memory", "user"]) -> bool:
        changed = ...  # existing impl
        if changed and which == "memory":
            self._bm25_index.invalidate()
        return changed

    def rebind_to_profile(self, profile_home: Path) -> None:
        # existing rebind logic also needs to point the index at the new home
        ...
        self._bm25_index = BM25Index(profile_home)
```

The `bm25_index` property exposes the index to future callers (M6.3's `ActiveMemory.retrieve` will reach in via `MemoryManager.bm25_index.query(...)`). `rebind_to_profile` swaps the index along with the paths so per-profile isolation holds.

## Concurrency / failure modes

| Scenario | Behavior |
|---|---|
| `MEMORY.md` missing | `query()` returns `[]`; cache not written |
| `MEMORY.md` empty | `query()` returns `[]`; cache not written |
| `MEMORY.md` 64KB+ | Build still works; rebuild time <500ms warm. No artificial cap (M6.5 owns the cap-pressure UX) |
| Cache file truncated | `_load_cache()` returns False; rebuild + overwrite |
| Cache file from a prior `format_version` | `_load_cache()` returns False; rebuild |
| Cache file's corpus_sha256 ≠ current MEMORY.md sha256 | rebuild |
| Two processes call `query()` simultaneously, no cache yet | Both build; whichever finishes last wins on cache write (idempotent because `_build()` is deterministic). Acceptable; no flock needed for v1 |
| Caller calls `invalidate()` while another is mid-`query()` | The mid-`query()` returns its result against the now-old in-memory state; the next `query()` rebuilds. Acceptable for v1 |
| `pyproject.toml` is shipped without `rank_bm25` after a stale install | `import rank_bm25` raises ModuleNotFoundError with a clear error message; `BM25Index` does not silently degrade. M6.2 will introduce graceful degradation later for vector embeddings (different concern: BM25 is mandatory infra, embeddings are best-effort) |

## Performance targets

- Cold build of a 4KB `MEMORY.md`: <100ms wall clock
- Warm query (cache hit): <5ms wall clock
- Rebuild after invalidation on a 16KB file: <200ms

## Testing strategy

TDD-shaped. Tests authored before implementation. All tests live in `OpenComputer/tests/` to match repo convention.

### Unit tests

`tests/test_memory_bm25_basic.py`:
- 10 entries in a synthetic `MEMORY.md`, `query("postgres")` returns the postgres-related entry first
- Empty `MEMORY.md` returns `[]`
- Missing `MEMORY.md` returns `[]`
- `top_k=3` returns at most 3 hits

`tests/test_memory_bm25_persist.py`:
- Build index, restart (new `BM25Index` instance), assert cache loaded (no rebuild)
- Verify by patching `_build()` to fail and confirming `query()` still works on the warm path

`tests/test_memory_bm25_invalidate.py`:
- Build index over 5-entry MEMORY.md
- Append a new entry directly to MEMORY.md, call `invalidate()`, query for the new entry's keyword
- Assert new entry appears in results

`tests/test_memory_bm25_corpus_change_detection.py`:
- Build index, manually overwrite `MEMORY.md` (simulating external edit), call `query()`
- Assert results reflect the new corpus (sha256 mismatch triggers rebuild)

`tests/test_memory_bm25_corrupt_cache.py`:
- Build index, truncate the cache file to 50% of its size, call `query()`
- Assert no exception raised; results match a fresh build

`tests/test_memory_bm25_format_version_skew.py`:
- Build index, hand-edit the cache header's `format_version` to 999, call `query()`
- Assert rebuild + new cache written with `format_version=1`

`tests/test_memory_bm25_segmentation.py`:
- Sample MEMORY.md with mix of `## Heading`, blank-line-separated paragraphs, code blocks
- Assert each entry's `raw` matches expected segmentation

`tests/test_memory_bm25_tokenizer.py`:
- `_tokenize("Hello, World! 2024-01-15")` → `["hello", "world", "2024", "01", "15"]`
- Edge cases: emoji, unicode, mixed case

### Integration tests

`tests/test_memory_bm25_integration.py`:
- Construct a real `MemoryManager` over `tmp_path`
- Call `MemoryManager.add` (or whatever the consolidated write API is — this test will pin the contract) with new entries
- Assert `MemoryManager.bm25_index.query()` returns them
- Assert cache file lives at `tmp_path / "cache" / "memory_bm25.idx"`

### Performance test (smoke, not load)

`tests/test_memory_bm25_perf.py`:
- 4KB synthetic `MEMORY.md`
- Assert cold build wall-clock <250ms (lenient on CI; spec target is 100ms but CI variance is real)
- Assert warm query <20ms

## Acceptance

Per the parent plan plus the audit additions:

- [x] `pip show rank_bm25` returns the package after install (it is a real runtime dep, not transitive)
- [x] `pytest tests/test_memory_bm25*.py -v` all pass
- [x] `ruff check opencomputer/agent/memory_index.py opencomputer/agent/memory.py tests/test_memory_bm25*.py` clean
- [x] Full repo test suite green: `pytest tests/ -x` (memory rule: never push without full-suite verification)
- [x] PR description quotes the audit findings folded into this PR (cache integrity + FTS5-vs-pickle decision) plus the carry-forward notes for M6.2/M6.3/M6.4/M6.6/M9.2

## What this spec refuses

- Indexing `MEMORY.md` as a single document. Entries are independent units; ranking only matters at the entry level
- Stemming or stopword removal in v1
- Locking the cache file. Two-process race ends in two valid rebuilds; no correctness loss
- Incremental updates to the BM25 corpus. Full rebuild is fast enough; incremental adds maintenance complexity
- Cross-profile sharing of the index. Each profile owns its `BM25Index`
- Touching M6.2's vector index design except by leaving room (the cache directory is shared but each index has its own filename)

## Refused alternatives

- **FTS5 over a `memory_entries` table**: rejected because (a) `MEMORY.md` is the canonical store and we don't want a second source of truth, (b) BM25Okapi is the desired ranking model, not FTS5's bm25() approximation, (c) corpus is tiny so build cost is negligible
- **Indexing the whole MEMORY.md as one document and returning highlighted spans**: rejected because retrieval semantics for M6.3 expect entry-level results, not span-level
- **A stopword list**: rejected for v1 because the corpus is small and the user's words are domain-specific (terms like "the model" or "for any" can be intentional). Revisit only on real-use evidence

## File-touch list

- `OpenComputer/pyproject.toml` — add `rank_bm25 = "^0.2.2"` to runtime `dependencies`
- `OpenComputer/opencomputer/agent/memory_index.py` — new module
- `OpenComputer/opencomputer/agent/memory.py` — minimal touch: instantiate `BM25Index`, expose via property, invalidate on declarative write
- `OpenComputer/tests/test_memory_bm25_basic.py` — new
- `OpenComputer/tests/test_memory_bm25_persist.py` — new
- `OpenComputer/tests/test_memory_bm25_invalidate.py` — new
- `OpenComputer/tests/test_memory_bm25_corpus_change_detection.py` — new
- `OpenComputer/tests/test_memory_bm25_corrupt_cache.py` — new
- `OpenComputer/tests/test_memory_bm25_format_version_skew.py` — new
- `OpenComputer/tests/test_memory_bm25_segmentation.py` — new
- `OpenComputer/tests/test_memory_bm25_tokenizer.py` — new
- `OpenComputer/tests/test_memory_bm25_integration.py` — new
- `OpenComputer/tests/test_memory_bm25_perf.py` — new
- `OpenComputer/CHANGELOG.md` — Unreleased section, "Added: BM25 index over MEMORY.md (M6.1; foundation for Active Memory in M6.3)"

## Branch / PR plan

- Branch: `feat/v1-1-memory-bm25-index-2026-05-09` (already created as a worktree under `.claude/worktrees/v1-1-memory-bm25-2026-05-09`)
- PR title: `feat(memory): MEMORY.md BM25 index — v1.1 plan-3 M6.1`
- PR body quotes Plan 3 M6.1 acceptance criteria + the audit findings folded in + the carry-forward notes
- Target: `main`
- Target reviewer: human (the operator)
- CI must be green before merge (no admin bypass)

## Carry-forward notes (do NOT lose these when M6.2/M6.3/M6.4/M6.6/M9 plans get written)

These are findings from the brainstorm-phase audit on Plan 3 that fall outside M6.1's scope but must not be lost:

1. **M9.2 fail-mode** — Default fail-closed on classifier error/timeout. Telemetry surfaced via `oc audit verify`.
2. **M6.4 cron-miss policy** — Catch-up if last successful run > `2 × cron_interval`, capped at one catch-up per real run.
3. **M6.3 + Honcho ordering** — Fixed system-prompt order: `[Honcho prefetch] → [Active Memory] → [user content]`. Cache prefix stability matters.
4. **`BaseProvider.embed()` contract for M6.6** — Returns `EmbeddingBatch(vectors, dimensionality, model_id, cost_estimate_usd)`. Batch ≤ 100. Unsupported providers raise `EmbeddingsNotSupported`.
