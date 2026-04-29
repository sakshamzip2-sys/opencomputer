# AMENDMENTS — Ship-Now 5 Picks

**Companion to:** `2026-04-29-ship-now-5-picks-AUDIT.md` (verdict: YELLOW with 6 critical defects).
**Status:** Plan revised. All 5 picks REVISED (none dropped). Audit was right on every count.

This document overrides the original plan where conflicts exist. The 5 sub-projects still ship; the *how* changes per audit findings.

---

## Headline changes

| Pick | What changed | Why |
|---|---|---|
| A — OpenRouter | Drop env-swap; subclass via `_api_key_env` + `super().__init__(api_key=, base_url=)` | OpenAIProvider already accepts both kwargs (audit C1) |
| B — Model aliases | Delete Task B3 entirely (YAML loader plumbing) | `_apply_overrides` auto-handles dict fields (audit C6) |
| C — Adapter wiring | Keep `config: dict` signature; mutate config dict; queue between chunker + adapter | Adapters take config not kwargs (audit C3); create_task has unbounded backpressure (audit C4) |
| D — /debug | Register via `SLASH_REGISTRY` + `_handle_debug` in `slash_handlers.py`; reuse `oc doctor` | `plugin_sdk.SlashCommand` is wrong registry (audit C2); doctor is canonical source (audit alt #5) |
| E — /snapshot export\|import | Use `tarfile.data_filter` (Python 3.12+); reject symlink/link types | Manual tar-slip check misses symlinks/PAX/Windows paths (audit C5) |

**Plus:** New mandatory **Phase 0.7 hard gate** — Phase 0 findings must update each Sub-project's tasks before code is written.

---

## Critical defect fixes

### Fix C1 — OpenRouter clean subclass

Replace Task A2's provider implementation with:

```python
# extensions/openrouter-provider/provider.py
"""OpenRouter provider — thin subclass of OpenAIProvider.

OpenRouter is OpenAI-wire-compatible; the only delta is the API-key env var
and the base URL. We subclass and override the env-var name; pass api_key
and base_url through to ``OpenAIProvider.__init__`` directly. Credential pool
rotation (`OPENROUTER_API_KEY="key1,key2,key3"`) works unchanged because the
parent's comma-split happens on the value we pass in.

Env vars:
  OPENROUTER_API_KEY   — required; key from https://openrouter.ai/keys
  OPENROUTER_BASE_URL  — optional override (default: openrouter.ai)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
if str(_OPENAI_PROVIDER_DIR) not in sys.path:
    sys.path.insert(0, str(_OPENAI_PROVIDER_DIR))

from provider import OpenAIProvider  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(OpenAIProvider):
    """OpenAI-compatible provider routed through OpenRouter.

    Sensible default model (override via ``model:`` in config.yaml or
    ``--model openai/gpt-5`` etc.).
    """
    default_model = "openai/gpt-4o-mini"
    _api_key_env_name = "OPENROUTER_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get(self._api_key_env_name)
        if not resolved_key:
            raise RuntimeError(
                f"{self._api_key_env_name} is not set. "
                "Get a free key at https://openrouter.ai/keys."
            )
        resolved_base = (
            base_url
            or os.environ.get("OPENROUTER_BASE_URL")
            or DEFAULT_OPENROUTER_BASE_URL
        )
        super().__init__(api_key=resolved_key, base_url=resolved_base)
```

**Tests** assert against parent's `_api_key` and `_base` (private attrs the parent already sets), not patched-on `self.api_key`/`self.base_url`. If the parent doesn't expose readable accessors, the OpenRouter PR adds two thin properties (`@property def api_key(self) -> str: return self._api_key`).

---

### Fix C2 — `/debug` registration via SLASH_REGISTRY

Replace Sub-project D Task D2 with:

**Files actually touched:**
- Modify: `opencomputer/cli_ui/slash.py` — append a `CommandDef` to `SLASH_REGISTRY`
- Modify: `opencomputer/cli_ui/slash_handlers.py` — add `_handle_debug` and route through `_HANDLERS`

```python
# opencomputer/cli_ui/slash.py — append to SLASH_REGISTRY
SLASH_REGISTRY.append(CommandDef(
    name="debug",
    description="Sanitized diagnostic dump for bug reports (no secrets).",
    category="meta",
))
```

```python
# opencomputer/cli_ui/slash_handlers.py — append
from opencomputer.cli_ui.slash_handlers.debug import build_debug_dump


async def _handle_debug(ctx, args: str) -> SlashResult:
    return SlashResult(handled=True, message=build_debug_dump())


_HANDLERS["debug"] = _handle_debug   # follows existing _handle_snapshot pattern
```

The `build_debug_dump` helper itself (Task D1) stays — but D2 changes from "register a SlashCommand" to "wire through SLASH_REGISTRY + _HANDLERS." This matches every other built-in slash.

**Plus:** delegate as much as possible to `oc doctor`. `build_debug_dump()` calls `doctor.run_health_checks(redact=True)` and adds the env-var-presence + recent-error-tail block. Avoids two sources of truth.

---

### Fix C3 — Adapter config-dict shape

Replace Sub-project C Tasks C2/C3/C4 with:

**Don't add kwargs to adapter `__init__`.** Read 5 fields from inside `__init__` via the existing `config` dict.

```python
# extensions/telegram/adapter.py — inside TelegramAdapter.__init__
streaming_cfg = config.get("streaming", {}) if isinstance(config, dict) else {}
self.streaming_block_chunker: bool = bool(streaming_cfg.get("block_chunker", False))
self.streaming_min_chars: int = int(streaming_cfg.get("min_chars", 80))
self.streaming_max_chars: int = int(streaming_cfg.get("max_chars", 1500))
self.streaming_human_delay_min_ms: int = int(streaming_cfg.get("human_delay_min_ms", 1000))  # Telegram floor
self.streaming_human_delay_max_ms: int = int(streaming_cfg.get("human_delay_max_ms", 2500))
```

(Slack and Discord same pattern, with platform-specific min_ms floors: Discord 1100, Slack 1100.)

**The plugin.py wiring change collapses to:** "the YAML `channels.<name>.streaming` block already lands in the config dict via the loader" — *no plugin.py change needed* if the loader already passes the channel YAML through. Verify in Phase 0.3.

---

### Fix C4 — Per-chat queue between chunker and adapter.send

Replace Task C1 dispatch wiring with a queue-based serialiser:

```python
# opencomputer/gateway/dispatch.py — at the streaming opt-in branch
import asyncio

stream_cb = None
drain_task = None

if getattr(adapter, "streaming_block_chunker", False):
    from plugin_sdk.streaming import wrap_stream_callback

    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=8)
    chat_id = event.chat_id  # captured by closure intentionally
    metadata = dict(event.metadata or {})  # preserve thread routing (audit H6)

    async def _drain() -> None:
        while True:
            item = await queue.get()
            if item is None:
                return  # sentinel — end of stream
            try:
                await adapter.send(chat_id, item, **metadata)
            except Exception:
                logger.exception("chunker drain: adapter.send failed")
                # don't break — let subsequent blocks try

    drain_task = asyncio.create_task(_drain())

    loop = asyncio.get_running_loop()

    def _enqueue(text: str) -> None:
        # called from chunker on the same loop; non-blocking
        try:
            queue.put_nowait(text)
        except asyncio.QueueFull:
            # Drop with a warning — preserves liveness vs blocking the loop
            logger.warning("chunker drain backpressure: queue full, dropping block")

    stream_cb = wrap_stream_callback(
        _enqueue,
        min_chars=getattr(adapter, "streaming_min_chars", 80),
        max_chars=getattr(adapter, "streaming_max_chars", 1500),
        human_delay_min_ms=getattr(adapter, "streaming_human_delay_min_ms", 1000),
        human_delay_max_ms=getattr(adapter, "streaming_human_delay_max_ms", 2500),
    )

try:
    result = await loop.run_conversation(..., stream_callback=stream_cb)
finally:
    if drain_task is not None:
        await queue.put(None)  # signal end-of-stream
        await drain_task
```

This:
- Bounds the queue at 8 outstanding blocks (backpressure-safe)
- Serialises adapter.send so platform rate limits aren't violated by parallel sends
- Surfaces exceptions via the logger (no silent swallow)
- Preserves `metadata` so DM-thread routing isn't lost
- Cleans up the drain task in `finally` (no orphan coroutine)

---

### Fix C5 — Tar-slip via `tarfile.data_filter`

Replace Task E2 import logic:

```python
import tarfile

def import_snapshot(
    profile_home: Path,
    *,
    archive_path: Path,
    label: str | None = None,
) -> str:
    archive_path = Path(archive_path)
    if not archive_path.is_file():
        raise ValueError(f"archive not found: {archive_path}")

    new_id = uuid.uuid4().hex[:12]
    if label:
        new_id = f"{new_id}-{label[:40]}"
    dest = snapshot_root(profile_home) / new_id
    try:
        dest.mkdir(parents=True, exist_ok=False)  # collision-safe (audit H5)
    except FileExistsError:
        new_id = f"{uuid.uuid4().hex[:12]}-{label[:40]}" if label else uuid.uuid4().hex[:12]
        dest = snapshot_root(profile_home) / new_id
        dest.mkdir(parents=True, exist_ok=False)

    with tarfile.open(archive_path, "r:gz") as tf:
        # Pre-screen: reject special-type members (defense in depth on top of data_filter)
        for member in tf.getmembers():
            if member.type in (
                tarfile.SYMTYPE,
                tarfile.LNKTYPE,
                tarfile.CHRTYPE,
                tarfile.BLKTYPE,
                tarfile.FIFOTYPE,
                tarfile.CONTTYPE,
            ):
                raise ValueError(
                    f"unsafe member type {member.type!r} in archive — refusing"
                )
        # Strip top-level dir
        members_to_extract = []
        for m in tf.getmembers():
            stripped = m.name.split("/", 1)
            if len(stripped) != 2:
                continue  # skip top-level dir entry itself
            m.name = stripped[1]
            members_to_extract.append(m)

        # Use Python 3.12+ data_filter — handles tar-slip + symlinks +
        # absolute paths + PAX + Windows-style paths automatically.
        tf.extractall(path=str(dest), members=members_to_extract, filter="data")

    return new_id
```

**Plus:** explicit `member.type` rejection above provides defense-in-depth even if `data_filter` is somehow bypassed in the future.

---

### Fix C6 — Drop Task B3

Sub-project B becomes a 3-task sub-project (B1 resolver, B2 add field, B4 use-site wiring). Replace Task B3 with a 3-line note:

> **B3 — YAML loader integration:** None required. `config_store.py::_apply_overrides` already round-trips dict fields via `asdict` reflection — once `model_aliases` exists on `ModelConfig` (B2), YAML auto-populates it. Verify only via the round-trip test added in B2.

The defensive type-coerce (str-keys, str-values) moves into `model_resolver.resolve_model` itself with one extra check at the top:

```python
def resolve_model(name, aliases, *, strict=False, max_depth=5):
    if not aliases:
        return name
    # Defensive: coerce keys/values to str; ignore non-str entries
    aliases = {str(k): str(v) for k, v in aliases.items() if v is not None}
    ...
```

---

## High-priority fixes

### Fix H2 — Read agent.log not error.log

Replace in `build_debug_dump`:

```python
# OLD
log_path = Path.home() / ".opencomputer" / "logs" / "error.log"

# NEW — agent.log exists per FullSystemControlConfig (config.py:306)
from opencomputer.agent.config import default_config
cfg = default_config()
log_path = getattr(cfg, "system_control", None) and cfg.system_control.log_path
log_path = log_path or (Path.home() / ".opencomputer" / "logs" / "agent.log")
```

### Fix H3 — Expand `_TRACKED_ENV_VARS`

```python
_TRACKED_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENROUTER_BASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    # Search-tool keys (audit H3)
    "BRAVE_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    # Audio / inference
    "GROQ_API_KEY",
    # Memory provider
    "HONCHO_API_KEY",
    "HONCHO_BASE_URL",
    # OC environment
    "OPENCOMPUTER_PROFILE",
    "OPENCOMPUTER_HOME",
)
```

(Note: `BRAVE_API_KEY`, not `BRAVE_SEARCH_API_KEY` — audit H3.)

### Fix H4 — Bounded log tail

Replace `read_text().splitlines()[-20:]`:

```python
def _tail_lines(path: Path, n: int = 20) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", errors="replace") as f:
            from collections import deque
            return list(deque(f, maxlen=n))
    except OSError:
        return []
```

### Fix H7 — Phase 0.7 hard gate

Add to Phase 0:

> **Task 0.7 — Apply audit fixes:** before any Sub-project branch is cut, the implementer reads this AMENDMENTS doc end-to-end and updates each Sub-project's failing-test step to match the corrected design. If a Phase 0.0–0.6 finding contradicts the Sub-project text, that Sub-project is paused and amended before code is written. The Sub-project descriptions in the original plan are not authoritative once Phase 0 has run.

---

## Stress-test mitigations

Per audit §4:

- **S2 (chunker rate-limit reordering):** addressed by Fix C4's queue.
- **S3 (alias → unsupported model):** add a follow-up note: `oc doctor` should validate aliases against the configured provider on next health check. Out of scope for ship-now; tracked.
- **S5 (cross-version snapshot import):** add minimal compat check in `import_snapshot`:

```python
manifest = dest / "manifest.json"
if manifest.exists():
    try:
        meta = json.loads(manifest.read_text())
        if "schema_version" in meta and meta["schema_version"] > _SUPPORTED_SCHEMA_VERSION:
            logger.warning("imported snapshot has newer schema_version=%s; partial restore likely",
                           meta["schema_version"])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("manifest unreadable: %s; continuing with import", e)
```

---

## Revised pick summary

| # | Pick | Effort | Status |
|---|------|--------|--------|
| A | OpenRouter provider — clean subclass | S (~0.5d, simpler than original) | REVISED |
| B | Model aliases (B1+B2+B4 only) | XS (~0.5d, dropped B3) | REVISED |
| C | Chunker wiring (3 adapters + dispatch queue) | M (~2d, tighter due to queue work) | REVISED |
| D | /debug via SLASH_REGISTRY + reuse oc doctor | XS (~0.5d) | REVISED |
| E | /snapshot export\|import via data_filter | S (~1d, simpler with stdlib helper) | REVISED |

**Total revised effort:** ~4.5d (was ~5d). Slightly faster because B3 is gone and C5 uses stdlib filter.

---

## Execution preconditions

Before starting any sub-project:

1. **Phase 0 runs to completion** — all 7 tasks (0.1–0.7) commit a `DECISIONS.md`. Each Phase 0 finding that contradicts the original plan becomes an entry HERE before code is written.
2. **`gh pr list` re-checked** — ensure no new PR (since this audit) collides on files touched.
3. **Pin Python version assumption** — Fix C5 relies on `tarfile.data_filter` (Python 3.12+). Confirm `pyproject.toml` requires `>=3.12`.

---

## Final readiness verdict

**Plan + spec + amendments + Phase 0 actually-run = GREEN. Without Phase 0 actually-run = YELLOW. Original plan as written = YELLOW leaning RED.**

Recommended next action: open the prep branch, run all 7 Phase 0 tasks, commit `DECISIONS.md`, integrate findings as amendments here, **then** start sub-projects.
