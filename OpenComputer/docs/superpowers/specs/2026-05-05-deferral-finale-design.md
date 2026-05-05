# Deferral Finale — Design + Plan + Self-Audit

**Date:** 2026-05-05
**Status:** Brainstorm + plan + audit. Execute now.
**Goal:** Honestly close the 10 items the prior session marked "genuinely cannot in one session" — by either shipping them, scoping them down to a viable MVP, producing the audit artifact, or naming the operator-only blocker explicitly.

---

## 1. Audit of the 10 deferred items

| # | Item | Honest verdict | This session ships |
|---|---|---|---|
| 1 | B.1 actual tag + PyPI | Operator OIDC sign-off required; no code can substitute. | **SKIP** — explicitly handed back to operator with runbook pointer (`RELEASE.md`). |
| 2 | B.2 example plugin to PyPI | Can prep an in-repo example ready for `cp -r + pip publish`. New repo + GitHub creation are operator's call. | **PR-9** — `examples/example-tool/` |
| 3 | C.1 memory-vector | Production-grade is 3-5 days. A scoped MVP (chroma + add/search/delete + profile isolation) is shippable. Reindex/eviction documented out of scope. | **PR-5** — MVP plugin |
| 4 | C.2 memory-wiki | Same: scoped MVP (markdown files + slug + wikilinks + ripgrep search) is shippable. | **PR-6** — MVP plugin |
| 5 | C.3 media-tools | TTS via edge-tts (already a dep) + ImageInfo via PIL + AudioTranscribe wrapper. Image gen explicitly deferred (needs paid API). | **PR-7** — MVP plugin |
| 6 | C.5 coding-harness audit | A docs artifact (no code change) is the actual deliverable; "audit" is exactly that. | **PR-8** — `docs/coding-harness-audit.md` |
| 7 | D.3 T1 install-from-remote-catalog | Catalog JSON + checksum verify + safe extract = a clean day's work. Catalog content itself is operator's call. | **PR-2** — `oc plugin install --remote` |
| 8 | D.3 T3 catalog signing | Ed25519 sign+verify on catalog body. Default trusted-keys file. Real keys are operator's choice. | **PR-3** — `opencomputer/plugins/catalog_signing.py` + CLI |
| 9 | D.4 T2 interactive secret prompt | `oc profile env-init` walks the env-template + Rich password prompt + atomic .env write. | **PR-1** — interactive secret prompt |
| 10 | E.2 per-token streaming | api-server's `/v1/chat/completions` already has stream=True path; just extend the handler signature to accept an `on_delta` callback and emit one SSE chunk per delta. | **PR-4** — per-token SSE |

Net: 8 PRs ship; B.1 honestly handed back. Total ~3500 LOC + ~70 tests.

---

## 2. Brainstorm — design per PR

### PR-1 (D.4 T2) — `oc profile env-init`

**Surface:** `opencomputer/cli_profile.py` adds `env_init_cmd`. New module `opencomputer/profile_env_init.py` for testable logic.

**Flow:**
1. Read manifests via existing `discover()` + active profile's `.env` (parse what's already set).
2. For each declared env var across enabled plugins:
   - Already-set + length ≥ 8 → skip silently (or report `[ok]` in `--verbose`).
   - Missing → `Prompt.ask("<label> [<env_name>]", password=True)`.
   - Empty input → skip (don't write empty values).
3. Write to `<profile>/.env` atomically (`tmp + os.replace`). Existing values preserved unless `--overwrite`.
4. `chmod 0o600`.

**Edge cases:**
- Ctrl-C mid-flow: rollback (don't write a partial .env). Use temp file + replace at end.
- Already-set values: leave untouched unless `--overwrite`.
- Non-TTY environment: refuse to run with a clear error (use `--write-template` instead).
- Profile not yet created: create `<profile>` dir.

**Tests (~6):** flow with mocked Prompt, tty-check, overwrite vs no-overwrite, atomic-write rollback, chmod 0600, empty-input skip.

---

### PR-2 (D.3 T1) — `oc plugin install --remote <slug>`

**Surface:** new module `opencomputer/plugins/remote_install.py`. CLI `cli_plugin.py` extends `install` to accept a `--remote` mode.

**Wire format (`oc-plugin-catalog.json`):**
```json
{
  "schema_version": 1,
  "generated_at": "2026-05-05T...",
  "signing_key_fingerprint": "ed25519:abc123...",
  "signature": "<base64 ed25519 sig over the canonical body>",
  "plugins": [
    {
      "id": "example-tool",
      "version": "0.1.0",
      "description": "...",
      "homepage": "https://...",
      "tarball_url": "https://github.com/.../example-tool-0.1.0.tgz",
      "tarball_sha256": "abc...",
      "min_host_version": "0.1.0",
      "license": "MIT"
    }
  ]
}
```

**Flow:**
1. Resolve catalog URL: env `OC_PLUGIN_CATALOG_URL` > `~/.opencomputer/config.yaml` `plugins.catalog_url` > built-in default (empty placeholder URL — operator configures).
2. Cache at `~/.opencomputer/plugin_catalog_cache.json` with 24h TTL. Stale → re-fetch + replace; fetch fail with cache → use cache + warn.
3. Optional signature verify (PR-3). If signing keys present + signature missing → warn + proceed (advisory). If keys present + signature invalid → REJECT.
4. Find slug; fetch tarball; verify sha256; extract via `tarfile.open(...).extractall(filter='data')` to profile plugins dir.
5. Re-validate manifest (`min_host_version` etc) post-extract before declaring success.

**Safety:**
- 50 MB hard cap on tarball size.
- `filter='data'` rejects abs paths, symlink escapes, device files (Python 3.12+).
- Refuse to overwrite a plugin id without `--force`.

**Tests (~8):** catalog cached, TTL re-fetch, slug-not-found, sha256-mismatch rejection, oversize rejection, path-traversal rejection, fetch-fail-no-cache raises, fetch-fail-with-cache warns + uses.

---

### PR-3 (D.3 T3) — Catalog signing (Ed25519)

**Surface:** new `opencomputer/plugins/catalog_signing.py`. CLI extension on `cli_plugin.py`: `oc plugin catalog sign <path> --key <pem>` and `oc plugin catalog verify <path>`.

**Module:**
```python
def sign_catalog(catalog: dict, private_key_pem: bytes) -> dict:
    """Returns catalog with `signature` + `signing_key_fingerprint` added."""

def verify_catalog(catalog: dict, trusted_keys: dict[str, bytes]) -> VerifyResult:
    """trusted_keys: fingerprint → public-key-pem. Returns Ok/Untrusted/Tampered/Missing."""
```

**Canonicalization:** `json.dumps(body, sort_keys=True, separators=(",", ":"))` after stripping `signature` + `signing_key_fingerprint` from the body.

**Trusted-keys store:** `~/.opencomputer/trusted_catalog_keys.json`:
```json
{
  "ed25519:abc123...": {
    "name": "OC official",
    "added_at": "2026-05-05T...",
    "public_key_pem": "-----BEGIN PUBLIC KEY-----\n..."
  }
}
```

**Wiring into PR-2:** `remote_install.fetch_catalog` calls `verify_catalog` if any trusted keys are present; rejects on tamper. If no trusted keys configured → unsigned acceptance (with `[warn]`).

**Tests (~6):** sign+verify roundtrip, tampered-body rejection, untrusted-key rejection, missing-signature warns, canonicalization stable, malformed signature handled.

---

### PR-4 (E.2) — per-token SSE in api-server

**Surface:** `extensions/api-server/adapter.py` + `plugin.py`.

**APIServer constructor adds:** `streaming_handler: Callable[[str, str, Callable[[str], None]], Awaitable[None]] | None = None`.

**Flow:**
- If `stream=True` AND `streaming_handler` configured: open SSE stream, drive `streaming_handler` with an `on_delta` callback that writes one SSE chunk per call. Final `[DONE]` after handler returns.
- If `stream=True` AND only legacy handler: existing single-chunk path (back-compat).
- Errors mid-stream: emit `data: {"error": ...}\n\n` then `[DONE]` and close.

**Wiring (plugin.py):** the gateway-level binding that wires up the streaming handler — when AgentLoop streaming is available (it is), we register a streaming handler that forwards `agent.run_conversation_streaming(stream_callback=on_delta)` deltas through.

**Tests (~5):** multi-delta SSE chunks, [DONE] terminator, streaming_handler not configured falls back, error mid-stream produces error chunk, deltas escape JSON properly.

---

### PR-5 (C.1) — extensions/memory-vector/ MVP

**Files:**
```
extensions/memory-vector/
├── plugin.json
├── plugin.py            ← register(api): MemoryVectorBackend
├── backend.py           ← chromadb-backed implementation
├── tests/test_basic.py
└── README.md
```

**Backend:**
- `add(text, metadata, doc_id=None) -> id`
- `search(query, top_k=5) -> list[Hit]`
- `delete(doc_id)`
- `count()`

**Storage:** `<profile>/memory-vector/chroma.db` (PersistentClient).

**Embeddings:** chromadb's built-in (sentence-transformers `all-MiniLM-L6-v2`, lazy-installed).

**Plugin manifest:** `kind: memory`, `setup.dependencies: ["chromadb>=0.4"]`. Declared as optional — plugin unloads with clear message if dep missing.

**Out of scope (documented in README):**
- Reindexing on schema change
- Eviction policies
- Distributed sharding
- Cross-profile sharing

**Tests (~5):** roundtrip add+search, persistence across reopens, top_k cap, profile isolation (two profiles → two DBs), delete-by-id.

---

### PR-6 (C.2) — extensions/memory-wiki/ MVP

**Files:**
```
extensions/memory-wiki/
├── plugin.json
├── plugin.py
├── backend.py           ← MarkdownWikiBackend
├── tests/test_basic.py
└── README.md
```

**Storage layout:**
```
<profile>/wiki/
├── slug-1.md            ← frontmatter + body
├── slug-2.md
└── .backlinks.json      ← computed cache
```

**Frontmatter format:** YAML at top:
```yaml
---
title: ...
created_at: ...
updated_at: ...
tags: [a, b]
---
body markdown with [[wikilinks]] auto-extracted
```

**API:**
- `add(title, body, tags=()) -> slug`
- `read(slug) -> Note`
- `search(query) -> list[slug]` (ripgrep-or-fallback)
- `delete(slug)`
- `backlinks(slug) -> list[slug]`

**Out of scope:** conflict resolution across simultaneous edits; cross-profile sharing.

**Tests (~5):** write+read+search, wikilink parsing, slug uniqueness (collision suffix), backlinks computed + cached, delete-cleans-backlinks.

---

### PR-7 (C.3) — extensions/media-tools/ MVP

**Files:**
```
extensions/media-tools/
├── plugin.json
├── plugin.py            ← register 3 tools
├── tools/
│   ├── image_info.py    ← PIL: dimensions, format, EXIF
│   ├── tts_generate.py  ← edge-tts wrapper (already a dep)
│   └── audio_transcribe.py ← delegates to local-whisper if installed
├── tests/test_basic.py
└── README.md
```

**Tools:**
- `ImageInfo(path)` → `{format, width, height, mode, exif: {...}}`
- `TTSGenerate(text, voice="en-US-AvaNeural", out_path)` → writes mp3 via edge-tts
- `AudioTranscribe(path)` → `{text, segments?}` via mlx-whisper or whisper.cpp; clear error if neither installed

**Out of scope:** image generation (paid API), video processing (ffmpeg pipelines), real-time streaming TTS.

**Tests (~5):** ImageInfo on PNG, ImageInfo on JPEG, TTS missing-edge-tts handled, AudioTranscribe missing-deps clear error, plugin.json validates.

---

### PR-8 (C.5) — coding-harness audit doc

**Output:** `docs/coding-harness-audit.md` containing per-subdirectory analysis:

```markdown
# Coding-Harness Audit (2026-05-05)

## Subdirectories (12)
| Path | LOC | Purpose | Public exports | Duplicate-with-core risk |
|---|---|---|---|---|
| introspection/ | 800 | psutil/mss/pyperclip wrappers | 5 tools | Low — distinct from core |
| modes/ | ... | plan/yolo runtime injection | ... | Some overlap with `agent/injection.py`? |
...

## Findings
- 3 candidates for dedup with `opencomputer/agent/injection.py` (modes/)
- 1 candidate for promotion to core (slash_commands/)
- 0 dead code

## Recommendations
- ...
```

No code changes. Just the audit artifact.

---

### PR-9 (B.2 prep) — `examples/example-tool/`

**Files:**
```
examples/example-tool/
├── README.md            ← step-by-step "fork this, publish to PyPI"
├── LICENSE
├── pyproject.toml       ← ready for `python -m build && twine upload`
├── plugin.json
├── example_tool/
│   ├── __init__.py
│   ├── plugin.py        ← register(api) → 1 simple tool ("WordCount")
│   └── tools.py         ← WordCount(text) -> count
├── tests/
│   └── test_word_count.py
└── .github/workflows/
    └── publish.yml      ← OIDC → PyPI on tag (template, commented)
```

**README walks through:**
1. `cp -r examples/example-tool ~/my-plugin && cd ~/my-plugin && git init`
2. Edit `pyproject.toml` (name, author, repo URL).
3. `python -m build && twine upload dist/*` (or set up GH repo + tag → CI).
4. Then in any OC profile: `oc plugin install ~/my-plugin` (local) or list it in your catalog (PR-2).

**Lives outside `extensions/`** so it's not auto-discovered as a bundled plugin. Tests still run (pytest picks up `examples/*/tests/`).

---

## 3. Self-audit (10 lenses)

### A1. Silent API drift
- `discover()` exists in `opencomputer/plugins/discovery.py` ✓
- `_load_source_manifest` exists in `cli_plugin.py` ✓
- `Rich Prompt(password=True)` is correct API ✓
- `tarfile.extractall(filter='data')` requires Python 3.12+ — pyproject already mandates this ✓
- `chromadb.PersistentClient` is the current API (not the deprecated `Client(persist_dir=...)`) — verified
- `edge-tts` is a declared dep — verified in pyproject.toml
- `cryptography.hazmat.primitives.asymmetric.ed25519` is the right path for Ed25519
- `aiohttp.web.StreamResponse.write` + `prepare()` is the existing SSE pattern in adapter.py

### A2. Test isolation
All new modules use the standard fixture pattern: `tmp_path` for filesystem, monkeypatch for env vars, mock httpx.AsyncClient for catalog fetch. No tests reach the real network.

### A3. Plugin SDK boundary
PR-5/6/7 are extensions — they import from `plugin_sdk` only. Confirmed by reading `plugin_sdk/__init__.py` exports. No `from opencomputer.*` in extension code (the boundary test enforces this).

### A4. Catalog signing — key infrastructure
A skeptic asks: "Where do the keys come from?" Honest answer:
- The signing module ships with `keygen` helper (`oc plugin catalog keygen --out <pem>`).
- The trusted-keys file starts EMPTY by default — operator chooses to add a key. If empty, signatures aren't enforced (warn-only).
- An operator running an internal catalog generates one keypair, signs their catalog, distributes the public key to users via README.

This is the standard apt/yum signed-repo pattern. Self-hosted by design.

### A5. Backwards compatibility
- PR-2's `oc plugin install` extension: `--remote <slug>` is opt-in. Existing local-path install unchanged.
- PR-4's streaming_handler: existing single-chunk path is fallback when handler missing. Not a breaking change.
- PR-1's env-init: brand-new command. No existing surface affected.

### A6. Concurrent installs
Two `oc plugin install --remote` calls racing on the same slug → both download tarball → both write to same destination. **Refinement:** acquire a `flock` on `<plugins_dir>/.install.lock` for the duration. Worst case is the second waits.

### A7. Empty catalog
First-run experience: no catalog URL configured. `oc plugin install --remote foo` should fail with: `error: no catalog URL configured. Set OC_PLUGIN_CATALOG_URL or run 'oc config set plugins.catalog_url <url>'.` Clear pointer to fix.

### A8. SSE error mid-stream
If the agent loop raises mid-streaming, we've already started the SSE response. We can't rewrite headers. **Refinement:** emit `data: {"error": "..."}\n\n` followed by `data: [DONE]\n\n` and close. Logged at error level. Standard SSE pattern.

### A9. memory-vector dependency weight
chromadb pulls sentence-transformers + torch by default. **Refinement:** plugin.json declares it as `optional_dependencies` not core; `oc plugin install memory-vector` triggers `pip install chromadb`. Bundled extension stays lightweight; activation lazy-imports.

### A10. coding-harness audit scope
"Audit" is properly an exploratory artifact. **Refinement:** scope it to per-subdirectory tabular analysis + 5 specific dedup recommendations. Anything beyond that is implementation work for a future session.

---

## 4. Plan summary

| PR | Title | Branch | LOC | Tests |
|---|---|---|---|---|
| 1 | D.4 T2 — `oc profile env-init` interactive | `feat/profile-env-init` | ~250 | 6 |
| 2 | D.3 T1 — `oc plugin install --remote` | `feat/plugin-remote-install` | ~500 | 8 |
| 3 | D.3 T3 — catalog Ed25519 signing | `feat/catalog-signing` | ~350 | 6 |
| 4 | E.2 — per-token SSE streaming | `feat/per-token-sse` | ~250 | 5 |
| 5 | C.1 — memory-vector MVP plugin | `feat/memory-vector-mvp` | ~400 | 5 |
| 6 | C.2 — memory-wiki MVP plugin | `feat/memory-wiki-mvp` | ~450 | 5 |
| 7 | C.3 — media-tools MVP plugin | `feat/media-tools-mvp` | ~400 | 5 |
| 8 | C.5 — coding-harness audit doc | `docs/coding-harness-audit` | 0 code, ~600 doc | 0 |
| 9 | B.2 prep — examples/example-tool/ | `feat/example-plugin` | ~300 | 3 |

**Total ~2900 LOC + ~43 tests across 9 PRs.**

**Honestly skipped:** B.1 (PyPI tag) — operator OIDC required.

Execute now in PR order 1→9.
