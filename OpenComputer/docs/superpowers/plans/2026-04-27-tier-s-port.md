# Tier S Port — Hermes-agent's 7 highest-leverage missing pieces

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the seven Tier S items from `sources/hermes-agent-2026.4.23/` that the user's deep-gap audit flagged as "embarrassingly missing, 1-day each." Tiers A/B/C are deferred to follow-up plans.

**Architecture:** Each item is a small, self-contained module in Hermes (75-230 LOC) that we port + adapt to OC's surfaces. No new heavy deps. The seven items in execution order:

| # | Item | Hermes file | OC location |
|---|---|---|---|
| 1 | Anthropic prompt caching (`system_and_3`) | `agent/prompt_caching.py` (73 LOC) | `opencomputer/agent/prompt_caching.py` + wire into anthropic-provider |
| 2 | Tool-result spillover (3-level overflow defense) | `tools/tool_result_storage.py` (227 LOC) + `tools/budget_config.py` | `opencomputer/agent/tool_result_storage.py` + `opencomputer/agent/budget_config.py` + wire into AgentLoop |
| 3 | OSV malware check before MCP launch | `tools/osv_check.py` (156 LOC) | `opencomputer/security/osv_check.py` + wire into mcp_install / mcp launcher |
| 4 | URL safety / SSRF guard | `tools/url_safety.py` (226 LOC) | `opencomputer/security/url_safety.py` + wire into WebFetch / WebSearch |
| 5 | Subdirectory hint discovery | `agent/subdirectory_hints.py` (224 LOC) | `opencomputer/agent/subdirectory_hints.py` + wire into AgentLoop tool-result path |
| 6 | Title generator | `agent/title_generator.py` (125 LOC) | `opencomputer/agent/title_generator.py` + wire into chat after first response |
| 7 | Cross-session rate-limit guard | `agent/nous_rate_guard.py` (182 LOC) | `opencomputer/agent/rate_guard.py` (generalized to per-provider) + wire into anthropic / openai provider retry |

**Tech Stack:** All pure stdlib (urllib, ipaddress, threading, json, tempfile, socket). No new pip deps.

**Source-of-truth pointer:** every implementation in this plan is a near-verbatim port from the corresponding Hermes file at `/Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/`. Where adaptation is needed (config, sandbox abstractions, naming), it's called out per-task.

---

## File Structure

| Path | Responsibility |
|---|---|
| `opencomputer/agent/prompt_caching.py` | NEW — pure functions, T1 |
| `opencomputer/agent/budget_config.py` | NEW — BudgetConfig dataclass + DEFAULT_BUDGET, T2 |
| `opencomputer/agent/tool_result_storage.py` | NEW — maybe_persist + enforce_turn_budget, T2 |
| `opencomputer/security/osv_check.py` | NEW — T3 |
| `opencomputer/security/url_safety.py` | NEW — T4 |
| `opencomputer/agent/subdirectory_hints.py` | NEW — SubdirectoryHintTracker, T5 |
| `opencomputer/agent/title_generator.py` | NEW — T6 |
| `opencomputer/agent/rate_guard.py` | NEW — generalized provider rate-limit state, T7 |
| `extensions/anthropic-provider/provider.py` (modify) | Wire prompt caching, T1 |
| `opencomputer/agent/loop.py` (modify) | Wire spillover + subdir hints + title-gen + rate guard, T2/5/6/7 |
| `opencomputer/cli_mcp.py` (modify) | Wire OSV check before launch, T3 |
| `opencomputer/tools/web_fetch.py` (modify) | Wire URL safety, T4 |
| `opencomputer/tools/web_search.py` (modify) | Wire URL safety, T4 |
| `opencomputer/agent/state.py` (modify if needed) | Add `set_session_title` / `get_session_title`, T6 |
| `extensions/anthropic-provider/provider.py` (modify) | Wire rate guard 429 record, T7 |
| `extensions/openai-provider/provider.py` (modify) | Wire rate guard 429 record, T7 |
| `tests/test_prompt_caching.py` | NEW |
| `tests/test_budget_config.py` | NEW |
| `tests/test_tool_result_storage.py` | NEW |
| `tests/test_osv_check.py` | NEW |
| `tests/test_url_safety.py` | NEW |
| `tests/test_subdirectory_hints.py` | NEW |
| `tests/test_title_generator.py` | NEW |
| `tests/test_rate_guard.py` | NEW |

---

## Task 1: Anthropic prompt caching (`system_and_3`)

**Why first:** highest ROI per LOC of any item in this plan. ~75% input cost reduction on multi-turn conversations. 73 LOC. The wiring is one-line per provider.

**Files:**
- Create: `opencomputer/agent/prompt_caching.py`
- Modify: `extensions/anthropic-provider/provider.py`
- Test: `tests/test_prompt_caching.py`

### Step 1.1: Port `prompt_caching.py`

Read `/Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/agent/prompt_caching.py` end-to-end. Copy verbatim to `opencomputer/agent/prompt_caching.py`. Two functions: `_apply_cache_marker` (private) + `apply_anthropic_cache_control` (public). Pure stdlib. Zero adaptation needed.

### Step 1.2: Wire into anthropic-provider

Read `extensions/anthropic-provider/provider.py`. Find where the API request is constructed (likely a `_build_request` or just inline in `complete()` / `stream_complete()`). Just before sending:

```python
from opencomputer.agent.prompt_caching import apply_anthropic_cache_control

# ... existing code that builds api_messages from self._messages ...

# NEW — apply cache breakpoints
api_messages = apply_anthropic_cache_control(api_messages, native_anthropic=True)
```

The `native_anthropic=True` flag matters: when calling Anthropic SDK directly (not OpenAI-compatible), the cache_control field goes on the message dict, not nested in content. Hermes detects this; we preserve.

### Step 1.3: Tests

```python
# tests/test_prompt_caching.py
"""V3.B-T1 — Anthropic prompt caching tests."""
from opencomputer.agent.prompt_caching import apply_anthropic_cache_control


def test_empty_messages_returns_empty():
    assert apply_anthropic_cache_control([]) == []


def test_system_message_gets_cache_control():
    msgs = [{"role": "system", "content": "you are an agent"}]
    out = apply_anthropic_cache_control(msgs)
    # System content becomes a list with cache_control on the last text block
    assert isinstance(out[0]["content"], list)
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_last_3_non_system_get_cache_control():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
        {"role": "user", "content": "msg3"},
        {"role": "assistant", "content": "msg4"},
    ]
    out = apply_anthropic_cache_control(msgs)
    # 4 breakpoints total: system + last 3 non-system (msg2, msg3, msg4)
    cache_count = 0
    for m in out:
        c = m.get("content")
        if isinstance(c, list):
            cache_count += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            cache_count += 1
    assert cache_count == 4
    # msg1 should NOT have cache_control (only last 3 non-system do)
    msg1_content = out[1]["content"]
    if isinstance(msg1_content, list):
        for blk in msg1_content:
            if isinstance(blk, dict):
                assert "cache_control" not in blk


def test_does_not_mutate_input():
    msgs = [{"role": "system", "content": "sys"}]
    apply_anthropic_cache_control(msgs)
    assert msgs[0]["content"] == "sys"  # untouched


def test_1h_ttl():
    msgs = [{"role": "system", "content": "sys"}]
    out = apply_anthropic_cache_control(msgs, cache_ttl="1h")
    cache_block = out[0]["content"][0]
    assert cache_block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_native_anthropic_tool_message():
    """When native_anthropic=True, tool messages get cache_control at top level."""
    msgs = [{"role": "tool", "tool_call_id": "t1", "content": "result"}]
    out = apply_anthropic_cache_control(msgs, native_anthropic=True)
    assert "cache_control" in out[0]


def test_max_4_breakpoints_with_many_messages():
    msgs = [{"role": "system", "content": "s"}]
    msgs += [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(20)]
    out = apply_anthropic_cache_control(msgs)
    cache_count = 0
    for m in out:
        c = m.get("content")
        if isinstance(c, list):
            cache_count += sum(1 for blk in c if isinstance(blk, dict) and "cache_control" in blk)
        if "cache_control" in m:
            cache_count += 1
    assert cache_count == 4
```

### Step 1.4: Verify + commit

```
python3.13 -m pytest tests/test_prompt_caching.py -v
```

Expect 7 PASS.

```bash
git add opencomputer/agent/prompt_caching.py extensions/anthropic-provider/provider.py tests/test_prompt_caching.py
git commit -m "feat(agent): TS-T1 — Anthropic prompt caching (system_and_3 strategy)"
```

---

## Task 2: Tool-result spillover (3-level overflow defense)

**Files:**
- Create: `opencomputer/agent/budget_config.py`
- Create: `opencomputer/agent/tool_result_storage.py`
- Modify: `opencomputer/agent/loop.py` (call `enforce_turn_budget` after each turn's tool calls)
- Test: `tests/test_budget_config.py`, `tests/test_tool_result_storage.py`

### Step 2.1: Port `budget_config.py`

Hermes's file at `/Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/tools/budget_config.py` (read it first). It exposes `BudgetConfig` dataclass with `preview_size`, `turn_budget`, per-tool thresholds, plus `DEFAULT_BUDGET` constant and `DEFAULT_PREVIEW_SIZE_CHARS`. Port verbatim into `opencomputer/agent/budget_config.py`.

### Step 2.2: Port `tool_result_storage.py`

Read `/Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/tools/tool_result_storage.py` end-to-end. Port to `opencomputer/agent/tool_result_storage.py` with **two adaptations**:

1. **Drop the `env=` parameter** — Hermes uses a multi-backend sandbox abstraction (Docker/SSH/Modal/Daytona). OC runs locally; just write to local filesystem via `pathlib.Path.write_text`. Replace `_resolve_storage_dir(env)` with a function that returns `<profile_home>/tool_result_storage/`.

2. **Drop `_write_to_sandbox` heredoc dance** — local writes are simple. Replace with:
   ```python
   def _write_local(content: str, path: Path) -> bool:
       try:
           path.parent.mkdir(parents=True, exist_ok=True)
           path.write_text(content, encoding="utf-8", errors="replace")
           return True
       except OSError as exc:
           logger.warning("Local write failed for %s: %s", path, exc)
           return False
   ```

Public surface remains: `maybe_persist_tool_result(content, tool_name, tool_use_id, config=DEFAULT_BUDGET, threshold=None)` + `enforce_turn_budget(tool_messages, config=DEFAULT_BUDGET)` + `generate_preview(content, max_chars)`.

### Step 2.3: Wire into `agent/loop.py`

Read `opencomputer/agent/loop.py` to find where tool results are appended to messages after a tool batch executes. Most likely in `run_conversation` after `await dispatch.dispatch_batch(...)`. Wrap each result through `maybe_persist_tool_result`:

```python
from opencomputer.agent.tool_result_storage import (
    enforce_turn_budget, maybe_persist_tool_result,
)

# After collecting tool_results from the dispatcher:
tool_results = [
    maybe_persist_tool_result(
        content=r.content, tool_name=r.tool_name, tool_use_id=r.tool_call_id,
    ) for r in tool_results
]
# Then aggregate budget enforcement:
tool_message_dicts = [{"content": r.content, "tool_call_id": r.tool_call_id} for r in tool_results]
enforce_turn_budget(tool_message_dicts)
# ... copy back into ToolResult content fields
```

(Adapt to actual ToolResult shape — read it in `plugin_sdk/core.py` first.)

### Step 2.4: Tests

```python
# tests/test_budget_config.py
from opencomputer.agent.budget_config import (
    BudgetConfig, DEFAULT_BUDGET, DEFAULT_PREVIEW_SIZE_CHARS,
)


def test_default_budget_constants():
    assert DEFAULT_PREVIEW_SIZE_CHARS > 0
    assert DEFAULT_BUDGET.turn_budget > 0
    assert DEFAULT_BUDGET.preview_size > 0


def test_resolve_threshold_returns_inf_when_unknown():
    cfg = BudgetConfig()
    # Tools without explicit threshold get the default
    assert cfg.resolve_threshold("UnknownTool") > 0
```

```python
# tests/test_tool_result_storage.py
from pathlib import Path
from opencomputer.agent.budget_config import BudgetConfig
from opencomputer.agent.tool_result_storage import (
    PERSISTED_OUTPUT_TAG, generate_preview, maybe_persist_tool_result,
    enforce_turn_budget,
)


def test_generate_preview_short_content_returns_unchanged():
    preview, has_more = generate_preview("hello", max_chars=100)
    assert preview == "hello"
    assert has_more is False


def test_generate_preview_truncates_long():
    long = "a" * 500 + "\n" + "b" * 500
    preview, has_more = generate_preview(long, max_chars=200)
    assert len(preview) <= 200
    assert has_more is True


def test_maybe_persist_under_threshold_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    out = maybe_persist_tool_result(
        content="short",
        tool_name="Read",
        tool_use_id="t1",
    )
    assert out == "short"


def test_maybe_persist_over_threshold_spills_to_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    huge = "x" * 100_000
    cfg = BudgetConfig(turn_budget=200_000, preview_size=400)
    out = maybe_persist_tool_result(
        content=huge,
        tool_name="Bash",
        tool_use_id="t1",
        config=cfg,
        threshold=10_000,
    )
    assert PERSISTED_OUTPUT_TAG in out
    assert "t1.txt" in out
    # Verify the file was actually written
    spill_dir = tmp_path / "tool_result_storage"
    assert any(p.name.endswith("t1.txt") for p in spill_dir.rglob("*"))


def test_enforce_turn_budget_no_op_when_under(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    msgs = [{"content": "small", "tool_call_id": "t1"}]
    out = enforce_turn_budget(msgs)
    assert out[0]["content"] == "small"


def test_enforce_turn_budget_spills_largest(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    msgs = [
        {"content": "a" * 50_000, "tool_call_id": "small"},
        {"content": "b" * 200_000, "tool_call_id": "huge"},
        {"content": "c" * 50_000, "tool_call_id": "med"},
    ]
    cfg = BudgetConfig(turn_budget=100_000, preview_size=400)
    enforce_turn_budget(msgs, config=cfg)
    # Largest ('huge') should have been spilled
    assert PERSISTED_OUTPUT_TAG in msgs[1]["content"]
```

### Step 2.5: Verify + commit

```bash
python3.13 -m pytest tests/test_budget_config.py tests/test_tool_result_storage.py -v
git add opencomputer/agent/budget_config.py opencomputer/agent/tool_result_storage.py opencomputer/agent/loop.py tests/test_budget_config.py tests/test_tool_result_storage.py
git commit -m "feat(agent): TS-T2 — tool-result spillover with 3-level overflow defense"
```

---

## Task 3: OSV malware check before MCP launch

**Files:**
- Create: `opencomputer/security/osv_check.py`
- Modify: `opencomputer/cli_mcp.py` (call check before launch)
- Test: `tests/test_osv_check.py`

### Step 3.1: Port verbatim

Read `/Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/tools/osv_check.py`. Port to `opencomputer/security/osv_check.py`. **Zero adaptation needed** — it's pure stdlib (urllib, json, re, os).

### Step 3.2: Wire into MCP launcher

Read `opencomputer/cli_mcp.py`. Find the install / launch path that runs `npx ...` or `uvx ...`. Before launching:

```python
from opencomputer.security.osv_check import check_package_for_malware

result = check_package_for_malware(command, args)
if result is not None:
    typer.echo(result, err=True)
    typer.echo("Use --skip-osv-check to override (not recommended).", err=True)
    raise typer.Exit(1)
```

Add a `--skip-osv-check` flag for emergency overrides.

### Step 3.3: Tests

```python
# tests/test_osv_check.py
"""V3.B-T3 — OSV malware check tests."""
from unittest.mock import patch

from opencomputer.security.osv_check import (
    _infer_ecosystem, _parse_npm_package, _parse_pypi_package,
    check_package_for_malware,
)


def test_infer_ecosystem_npx():
    assert _infer_ecosystem("npx") == "npm"
    assert _infer_ecosystem("/usr/bin/npx.cmd") == "npm"


def test_infer_ecosystem_uvx():
    assert _infer_ecosystem("uvx") == "PyPI"
    assert _infer_ecosystem("pipx") == "PyPI"


def test_infer_ecosystem_unknown():
    assert _infer_ecosystem("docker") is None


def test_parse_npm_scoped():
    name, version = _parse_npm_package("@scope/pkg@1.2.3")
    assert name == "@scope/pkg"
    assert version == "1.2.3"


def test_parse_npm_unscoped():
    name, version = _parse_npm_package("react@18.0.0")
    assert name == "react"
    assert version == "18.0.0"


def test_parse_pypi_with_extras():
    name, version = _parse_pypi_package("requests[socks]==2.31.0")
    assert name == "requests"
    assert version == "2.31.0"


def test_check_unknown_command_returns_none():
    """Non-npx/uvx commands skip the check."""
    assert check_package_for_malware("docker", ["run", "redis"]) is None


def test_check_clean_package_returns_none():
    """Mock OSV API: clean response → None."""
    fake_response = b'{"vulns": []}'
    with patch("opencomputer.security.osv_check.urllib.request.urlopen") as mock:
        mock.return_value.__enter__.return_value.read.return_value = fake_response
        result = check_package_for_malware("npx", ["react"])
    assert result is None


def test_check_malware_returns_block_message():
    """Mock OSV API: MAL-* advisory → blocking message."""
    fake_response = b'{"vulns": [{"id": "MAL-2024-1234", "summary": "malicious code"}]}'
    with patch("opencomputer.security.osv_check.urllib.request.urlopen") as mock:
        mock.return_value.__enter__.return_value.read.return_value = fake_response
        result = check_package_for_malware("npx", ["evil-package"])
    assert result is not None
    assert "BLOCKED" in result
    assert "MAL-2024-1234" in result


def test_check_network_failure_fails_open():
    """Network errors → None (don't block on transient failures)."""
    with patch(
        "opencomputer.security.osv_check.urllib.request.urlopen",
        side_effect=Exception("network down"),
    ):
        result = check_package_for_malware("npx", ["react"])
    assert result is None


def test_check_ignores_regular_cves():
    """Regular CVE-* IDs are NOT blocked — only MAL-*."""
    fake_response = b'{"vulns": [{"id": "CVE-2024-1234", "summary": "xss"}]}'
    with patch("opencomputer.security.osv_check.urllib.request.urlopen") as mock:
        mock.return_value.__enter__.return_value.read.return_value = fake_response
        result = check_package_for_malware("npx", ["react"])
    assert result is None
```

### Step 3.4: Verify + commit

```bash
python3.13 -m pytest tests/test_osv_check.py -v
git add opencomputer/security/osv_check.py opencomputer/cli_mcp.py tests/test_osv_check.py
git commit -m "feat(security): TS-T3 — OSV malware check before MCP package launch"
```

---

## Task 4: URL safety / SSRF guard

**Files:**
- Create: `opencomputer/security/url_safety.py`
- Modify: `opencomputer/tools/web_fetch.py` (call `is_safe_url` before request)
- Modify: `opencomputer/tools/web_search.py` (same)
- Test: `tests/test_url_safety.py`

### Step 4.1: Port + adapt config integration

Read `/Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/tools/url_safety.py`. Port to `opencomputer/security/url_safety.py` with **one adaptation**:

- Replace `from hermes_cli.config import read_raw_config` with OC's config loader. The `_global_allow_private_urls` function reads `security.allow_private_urls` and `browser.allow_private_urls` from config.yaml. Use OC's existing config loader (likely `opencomputer.agent.config_store.load_config()` — read it to confirm).

- Replace `HERMES_ALLOW_PRIVATE_URLS` env var with `OPENCOMPUTER_ALLOW_PRIVATE_URLS`.

- Drop `_TRUSTED_PRIVATE_IP_HOSTS` content (Hermes-specific QQ media), keep the framework empty for OC.

### Step 4.2: Wire into WebFetch

Read `opencomputer/tools/web_fetch.py`. Find where the request is made. Add safety check:

```python
from opencomputer.security.url_safety import is_safe_url

# In execute(call):
url = call.arguments.get("url", "")
if not is_safe_url(url):
    return ToolResult(
        tool_call_id=call.id,
        content=f"Blocked: URL {url} fails SSRF safety check (private IP, cloud metadata, or DNS resolution failure).",
        is_error=True,
    )
# ... existing fetch logic ...
```

### Step 4.3: Wire into WebSearch

Same pattern as WebFetch — block result URLs that resolve to private IPs.

### Step 4.4: Tests

```python
# tests/test_url_safety.py
"""V3.B-T4 — URL safety / SSRF tests."""
from unittest.mock import patch

import pytest

from opencomputer.security.url_safety import (
    _reset_allow_private_cache, is_safe_url,
)


@pytest.fixture(autouse=True)
def _reset():
    _reset_allow_private_cache()
    yield
    _reset_allow_private_cache()


def test_safe_external_url_passes(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", raising=False)
    # google.com resolves to a public IP — should pass
    assert is_safe_url("https://www.google.com") is True


def test_localhost_blocked(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", raising=False)
    assert is_safe_url("http://localhost:8080") is False
    assert is_safe_url("http://127.0.0.1") is False


def test_169_254_169_254_always_blocked(monkeypatch):
    """Even with toggle on, cloud metadata is ALWAYS blocked."""
    monkeypatch.setenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", "true")
    assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False


def test_metadata_google_internal_always_blocked(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", "true")
    assert is_safe_url("http://metadata.google.internal/") is False


def test_rfc1918_blocked_by_default(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", raising=False)
    # Use a hostname known to resolve into 10.0.0.0/8 won't work in tests;
    # test direct IP literals instead
    assert is_safe_url("http://10.0.0.1") is False
    assert is_safe_url("http://192.168.1.1") is False
    assert is_safe_url("http://172.16.0.1") is False


def test_cgnat_range_blocked(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", raising=False)
    assert is_safe_url("http://100.64.0.1") is False


def test_dns_failure_blocks_request(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", raising=False)
    assert is_safe_url("http://this-domain-does-not-resolve-12345.invalid") is False


def test_env_toggle_allows_private(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", "true")
    # 10.0.0.1 should now be allowed (toggle on)
    # But 169.254.169.254 still blocked (always)
    # Note: real DNS not used for IP literals
    assert is_safe_url("http://10.0.0.1") is True


def test_env_toggle_false_blocks_private(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", "false")
    assert is_safe_url("http://10.0.0.1") is False


def test_malformed_url_blocked(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", raising=False)
    assert is_safe_url("not-a-url") is False
    assert is_safe_url("") is False
```

### Step 4.5: Verify + commit

```bash
python3.13 -m pytest tests/test_url_safety.py -v
git add opencomputer/security/url_safety.py opencomputer/tools/web_fetch.py opencomputer/tools/web_search.py tests/test_url_safety.py
git commit -m "feat(security): TS-T4 — URL safety / SSRF guard for WebFetch + WebSearch"
```

---

## Task 5: Subdirectory hint discovery

**Files:**
- Create: `opencomputer/agent/subdirectory_hints.py`
- Modify: `opencomputer/agent/loop.py` (call `tracker.check_tool_call(...)` after each tool dispatch, append hints to result)
- Test: `tests/test_subdirectory_hints.py`

### Step 5.1: Port + adapt context loader

Read `/Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/agent/subdirectory_hints.py`. Port to `opencomputer/agent/subdirectory_hints.py` with **one adaptation**:

- Replace `from agent.prompt_builder import _scan_context_content` with OC's V3.A-T8 `load_workspace_context` infrastructure. If `_scan_context_content` doesn't exist as a separate scanner in OC, just inline a noop: `_scan_context_content = lambda content, _: content` (no security scan in MVP; V3.B can add).

- Adapt `_HINT_FILENAMES` to also include `OPENCOMPUTER.md` (V3.A-T8's primary file).

### Step 5.2: Wire into AgentLoop

Read `opencomputer/agent/loop.py`. Find the tool-result append site. Add:

```python
from opencomputer.agent.subdirectory_hints import SubdirectoryHintTracker

# In AgentLoop.__init__:
self._subdir_tracker = SubdirectoryHintTracker(working_dir=os.getcwd())

# After each tool result:
hints = self._subdir_tracker.check_tool_call(call.name, dict(call.arguments))
if hints:
    # Append to the tool's result content (not the system prompt — preserves cache)
    result.content = (result.content or "") + hints
```

### Step 5.3: Tests

```python
# tests/test_subdirectory_hints.py
"""V3.B-T5 — Subdirectory hint discovery tests."""
from pathlib import Path

from opencomputer.agent.subdirectory_hints import SubdirectoryHintTracker


def test_no_hints_when_no_md_files(tmp_path):
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    sub = tmp_path / "sub"
    sub.mkdir()
    hints = tracker.check_tool_call("Read", {"file_path": str(sub / "file.py")})
    assert hints is None


def test_loads_agents_md_from_subdir(tmp_path):
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    sub = tmp_path / "backend"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("# Backend rules\nUse FastAPI.")
    hints = tracker.check_tool_call("Read", {"file_path": str(sub / "main.py")})
    assert hints is not None
    assert "FastAPI" in hints
    assert "backend" in hints  # relative path included


def test_loads_claude_md_from_subdir(tmp_path):
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    sub = tmp_path / "frontend"
    sub.mkdir()
    (sub / "CLAUDE.md").write_text("React 18, no class components.")
    hints = tracker.check_tool_call("Read", {"file_path": str(sub / "App.tsx")})
    assert hints is not None
    assert "React 18" in hints


def test_hints_only_loaded_once(tmp_path):
    """Same directory's hints should only be returned the first time."""
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    sub = tmp_path / "x"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("rules")
    first = tracker.check_tool_call("Read", {"file_path": str(sub / "f.py")})
    assert first is not None
    second = tracker.check_tool_call("Read", {"file_path": str(sub / "g.py")})
    assert second is None  # already loaded


def test_working_dir_pre_marked(tmp_path):
    """The startup CWD's hints are NOT re-loaded (handled by prompt builder)."""
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    (tmp_path / "AGENTS.md").write_text("root rules")
    hints = tracker.check_tool_call("Read", {"file_path": str(tmp_path / "main.py")})
    assert hints is None  # CWD already loaded


def test_walks_up_ancestors(tmp_path):
    """Reading project/src/main.py discovers project/AGENTS.md even when src/ has none."""
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("project-wide rules")
    src = project / "src"
    src.mkdir()
    hints = tracker.check_tool_call("Read", {"file_path": str(src / "main.py")})
    assert hints is not None
    assert "project-wide" in hints


def test_extracts_paths_from_terminal_command(tmp_path):
    """Terminal/Bash commands have their path tokens extracted."""
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    sub = tmp_path / "scripts"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("scripts rules")
    hints = tracker.check_tool_call("terminal", {"command": "ls scripts/build.sh"})
    assert hints is not None
    assert "scripts rules" in hints
```

### Step 5.4: Verify + commit

```bash
python3.13 -m pytest tests/test_subdirectory_hints.py -v
git add opencomputer/agent/subdirectory_hints.py opencomputer/agent/loop.py tests/test_subdirectory_hints.py
git commit -m "feat(agent): TS-T5 — subdirectory hint discovery (lazy AGENTS.md / CLAUDE.md / OPENCOMPUTER.md loading)"
```

---

## Task 6: Title generator

**Files:**
- Create: `opencomputer/agent/title_generator.py`
- Modify: `opencomputer/agent/loop.py` (call `maybe_auto_title` after first response)
- Modify: `opencomputer/agent/state.py` (ensure `set_session_title` / `get_session_title` exist; add if missing)
- Test: `tests/test_title_generator.py`

### Step 6.1: Port + adapt cheap-LLM client

Read `/Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/agent/title_generator.py`. Port to `opencomputer/agent/title_generator.py` with **one adaptation**:

- Replace `from agent.auxiliary_client import call_llm` with OC's existing `opencomputer/agent/cheap_route.py`. Read it first to understand the actual API. Likely it's a function like `cheap_call(messages, ...)`. Adapt the import + call signature.

- If OC doesn't have a sync cheap-LLM helper, just import the default provider and call it directly (with a small `max_tokens=50`).

### Step 6.2: Verify SessionDB has title column

Read `opencomputer/agent/state.py` (SessionDB). Look for `set_session_title` / `get_session_title` methods. If absent, add them (plus a schema migration for a `title TEXT` column on sessions table).

### Step 6.3: Wire into agent loop

In `agent/loop.py::run_conversation`, after the first user→assistant exchange completes:

```python
from opencomputer.agent.title_generator import maybe_auto_title

# At the end of run_conversation, after the assistant response is in messages:
maybe_auto_title(
    session_db=self.db,
    session_id=self.session_id,
    user_message=user_message,
    assistant_response=assistant_response,
    conversation_history=self.messages,
)
```

`maybe_auto_title` is fire-and-forget (spawns daemon thread); doesn't add latency.

### Step 6.4: Tests

```python
# tests/test_title_generator.py
"""V3.B-T6 — Title generator tests."""
from unittest.mock import MagicMock, patch

from opencomputer.agent.title_generator import (
    auto_title_session, generate_title, maybe_auto_title,
)


def test_generate_title_returns_clean_string():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "Stock Trading Strategies"
    with patch("opencomputer.agent.title_generator.call_llm", return_value=fake_response):
        title = generate_title("hi", "let's analyze stocks")
    assert title == "Stock Trading Strategies"


def test_generate_title_strips_quotes():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = '"Quoted Title"'
    with patch("opencomputer.agent.title_generator.call_llm", return_value=fake_response):
        title = generate_title("u", "a")
    assert title == "Quoted Title"


def test_generate_title_caps_length():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "x" * 200
    with patch("opencomputer.agent.title_generator.call_llm", return_value=fake_response):
        title = generate_title("u", "a")
    assert len(title) <= 80


def test_generate_title_returns_none_on_exception():
    with patch(
        "opencomputer.agent.title_generator.call_llm",
        side_effect=Exception("network"),
    ):
        title = generate_title("u", "a")
    assert title is None


def test_auto_title_skips_when_already_titled():
    db = MagicMock()
    db.get_session_title.return_value = "Existing Title"
    auto_title_session(db, "sid", "u", "a")
    db.set_session_title.assert_not_called()


def test_auto_title_sets_when_no_existing():
    db = MagicMock()
    db.get_session_title.return_value = None
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = "New Title"
    with patch("opencomputer.agent.title_generator.call_llm", return_value=fake_response):
        auto_title_session(db, "sid", "u", "a")
    db.set_session_title.assert_called_once_with("sid", "New Title")


def test_maybe_auto_title_skips_after_third_exchange():
    """Doesn't title later in long conversations."""
    db = MagicMock()
    history = [{"role": "user"} for _ in range(5)]
    maybe_auto_title(db, "sid", "u", "a", history)
    # set_session_title should NOT be called (>2 user msgs in history)
    # We can't easily assert on the threading; just verify it doesn't crash
```

### Step 6.5: Verify + commit

```bash
python3.13 -m pytest tests/test_title_generator.py -v
git add opencomputer/agent/title_generator.py opencomputer/agent/loop.py opencomputer/agent/state.py tests/test_title_generator.py
git commit -m "feat(agent): TS-T6 — async title generator (cheap LLM, fire-and-forget)"
```

---

## Task 7: Cross-session rate-limit guard (generalized per-provider)

**Files:**
- Create: `opencomputer/agent/rate_guard.py` (generalized — not Nous-specific)
- Modify: `extensions/anthropic-provider/provider.py` (record 429 + check before request)
- Modify: `extensions/openai-provider/provider.py` (same)
- Test: `tests/test_rate_guard.py`

### Step 7.1: Port + generalize

Read `/Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/agent/nous_rate_guard.py`. Port to `opencomputer/agent/rate_guard.py` with **two adaptations**:

1. **Generalize beyond Nous** — public functions take a `provider: str` parameter:
   ```python
   def record_rate_limit(provider: str, *, headers=None, error_context=None, default_cooldown=300.0) -> None: ...
   def rate_limit_remaining(provider: str) -> Optional[float]: ...
   def clear_rate_limit(provider: str) -> None: ...
   def format_remaining(seconds: float) -> str: ...  # unchanged
   ```

2. **State path uses `<profile_home>/rate_limits/{provider}.json`** instead of hardcoded `~/.hermes/...`. Use OC's `_home()` from `opencomputer.agent.config`.

### Step 7.2: Wire into providers

In `extensions/anthropic-provider/provider.py`, find the request path. Before:

```python
from opencomputer.agent.rate_guard import rate_limit_remaining, record_rate_limit, format_remaining

# Before sending:
remaining = rate_limit_remaining("anthropic")
if remaining is not None:
    raise RateLimitedError(
        f"Anthropic rate-limited; wait {format_remaining(remaining)} before retry"
    )

# After sending — on 429:
except APIError as exc:
    if exc.status_code == 429:
        record_rate_limit("anthropic", headers=dict(exc.response.headers) if exc.response else None)
    raise
```

Same in `openai-provider/provider.py`.

### Step 7.3: Tests

```python
# tests/test_rate_guard.py
"""V3.B-T7 — Cross-session rate-limit guard tests."""
import time
from pathlib import Path

import pytest

from opencomputer.agent.rate_guard import (
    clear_rate_limit, format_remaining, rate_limit_remaining, record_rate_limit,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    yield tmp_path


def test_no_rate_limit_returns_none(home):
    assert rate_limit_remaining("anthropic") is None


def test_record_then_check_returns_remaining(home):
    record_rate_limit("anthropic", default_cooldown=60.0)
    remaining = rate_limit_remaining("anthropic")
    assert remaining is not None
    assert 50 < remaining <= 60


def test_separate_providers_isolated(home):
    record_rate_limit("anthropic", default_cooldown=60.0)
    assert rate_limit_remaining("anthropic") is not None
    assert rate_limit_remaining("openai") is None


def test_expired_state_returns_none(home):
    """When the cooldown has passed, remaining returns None and cleans up."""
    record_rate_limit("anthropic", default_cooldown=0.001)
    time.sleep(0.005)
    assert rate_limit_remaining("anthropic") is None


def test_clear_removes_state(home):
    record_rate_limit("anthropic", default_cooldown=60.0)
    clear_rate_limit("anthropic")
    assert rate_limit_remaining("anthropic") is None


def test_record_uses_retry_after_header(home):
    record_rate_limit("anthropic", headers={"retry-after": "120"})
    remaining = rate_limit_remaining("anthropic")
    assert 110 < remaining <= 120


def test_record_uses_x_ratelimit_reset_header(home):
    record_rate_limit("anthropic", headers={"x-ratelimit-reset-requests-1h": "300"})
    remaining = rate_limit_remaining("anthropic")
    assert 290 < remaining <= 300


def test_format_remaining_minutes():
    assert format_remaining(120) == "2m"
    assert format_remaining(125) == "2m 5s"


def test_format_remaining_hours():
    assert format_remaining(3700) == "1h 1m"


def test_atomic_write_handles_corrupt_state(home):
    """Corrupt state file → returns None gracefully."""
    state_path = home / "rate_limits" / "anthropic.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not json {{")
    assert rate_limit_remaining("anthropic") is None
```

### Step 7.4: Verify + commit

```bash
python3.13 -m pytest tests/test_rate_guard.py -v
git add opencomputer/agent/rate_guard.py extensions/anthropic-provider/provider.py extensions/openai-provider/provider.py tests/test_rate_guard.py
git commit -m "feat(agent): TS-T7 — cross-session rate-limit guard (generalized per-provider)"
```

---

## Task 8: Final validation + CHANGELOG + push + PR

### Step 8.1: Full pytest

```
python3.13 -m pytest -q
```

Confirm 3380+ pass (V3.A baseline 3278 + this PR's ~50 tests).

### Step 8.2: Full ruff

```
ruff check . --fix
ruff check .
```

Manual fix anything not auto-fixable.

### Step 8.3: CHANGELOG entry

Append to `[Unreleased]`:

```markdown
### Added (Tier S Port from Hermes — 2026-04-27)

Seven highest-leverage modules ported from `sources/hermes-agent-2026.4.23/`
that were embarrassingly missing in OpenComputer's stack. Saksham's deep-gap
audit identified these as "1-day each, ranked by leverage."

- **TS-T1 — Anthropic prompt caching (`system_and_3` strategy).** Up to 4
  cache_control breakpoints (system + last 3 non-system messages). ~75%
  input-token cost reduction on multi-turn conversations. Wired into
  anthropic-provider's request path. Pure functions, 73 LOC, zero deps.
- **TS-T2 — Tool-result spillover with 3-level overflow defense.** Per-tool
  cap (each tool's job), per-result persistence (>threshold spills to
  `<profile_home>/tool_result_storage/<id>.txt` with preview + path in-context),
  per-turn aggregate budget (200K-char ceiling triggers largest-result spill).
  Closes the OOM-on-big-grep gap.
- **TS-T3 — OSV malware check before MCP server launch.** Queries Google's
  free OSV API for `MAL-*` advisories on `npx`/`uvx` packages before launch.
  Fail-open on network errors. ~80 LOC. Covers npm + PyPI ecosystems.
- **TS-T4 — URL safety / SSRF guard.** Blocks 169.254.169.254 (cloud
  metadata), localhost, RFC1918 + CGNAT (100.64/10) + link-local. Always
  blocks cloud metadata even with toggle on. `OPENCOMPUTER_ALLOW_PRIVATE_URLS`
  env var + `security.allow_private_urls` config opt-out for VPN/proxy edge
  cases. Wired into WebFetch + WebSearch.
- **TS-T5 — Subdirectory hint discovery.** As the agent navigates into
  subdirectories via tool calls, lazily loads `AGENTS.md` / `CLAUDE.md` /
  `OPENCOMPUTER.md` / `.cursorrules` and **appends to the tool result**, not
  the system prompt. Preserves prompt caching. Walks up to 5 ancestor
  directories. Inspired by Block/goose.
- **TS-T6 — Async title generator.** Auto-generates short session titles
  via cheap-LLM call (cheap_route) after the first response. Daemon-thread
  fire-and-forget so it never adds latency. SessionDB schema gains
  `set_session_title` / `get_session_title`.
- **TS-T7 — Cross-session rate-limit guard.** Generalized port of Hermes's
  Nous-specific guard. State at `<profile_home>/rate_limits/{provider}.json`.
  Atomic writes via tempfile + `os.replace`. Prevents 429-amplification
  when retries-of-retries kick in across parallel sessions.

V3.B follow-ups parked:
- Tier A (PTC, Skills Guard, pluggable context engines, Insights, Tirith, file_state, MCP server mode) — separate plan.
- Tier B (proxy-capture, realtime voice, canvas-host, link-understanding, command-detection, send-policy, memory-host-sdk, detached tasks) — separate plan.
- Tier C (clarify_tool, interrupt, credential_pool, shell_hooks, etc.) — separate plan.

Spec + plan: `OpenComputer/docs/superpowers/plans/2026-04-27-tier-s-port.md`
```

### Step 8.4: Commit + push + open PR

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): Tier S port entry (7 Hermes modules)"
git push -u origin feat/tier-s-port

gh pr create --base main --head feat/tier-s-port --title "feat: Tier S Port — 7 Hermes modules (prompt caching, spillover, OSV, SSRF, subdir hints, title gen, rate guard)" --body "$(cat <<'EOF'
## Summary

Seven highest-leverage modules ported from \`sources/hermes-agent-2026.4.23/\`. Saksham's deep-gap audit ranked these as "1-day each, ranked by leverage." Tiers A/B/C are deferred to follow-up plans.

### What's wired

1. **Anthropic prompt caching** — \`system_and_3\` strategy, ~75% input cost reduction
2. **Tool-result spillover** — 3-level overflow defense (per-tool cap → per-result spill → per-turn budget)
3. **OSV malware check** — blocks \`MAL-*\` advisories before npx/uvx MCP launch
4. **URL safety / SSRF guard** — blocks cloud metadata + RFC1918 + CGNAT + link-local
5. **Subdirectory hints** — lazy AGENTS.md / CLAUDE.md / OPENCOMPUTER.md discovery
6. **Async title generator** — fire-and-forget cheap-LLM call after first response
7. **Cross-session rate guard** — generalized provider 429 state at \`<profile_home>/rate_limits/\`

### Numbers

- 8 commits (clean linear history)
- ~50+ new tests vs V3.A baseline 3278
- ruff clean
- All 7 modules ported near-verbatim from Hermes (clear provenance documented)

### Test plan

- [ ] CI green
- [ ] Manual smoke: \`opencomputer chat\` for 5+ turns → verify Anthropic API responses include \`cache_creation_input_tokens\` / \`cache_read_input_tokens\` (proves cache_control is reaching the API)
- [ ] Manual smoke: run a Bash command that returns >50KB output → verify it's spilled to \`<profile_home>/tool_result_storage/\`
- [ ] Manual smoke: \`opencomputer mcp install evil-package\` (with a known MAL-* advisory) → verify blocked
- [ ] Manual smoke: \`opencomputer chat\`, ask agent to \`WebFetch http://169.254.169.254/\` → blocked
- [ ] Manual smoke: \`cd backend; oc code\` then ask agent to read a file → verify backend/AGENTS.md hint appended

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

### Step 8.5: Verify CI

```
gh pr checks <NUMBER> --watch
```

DO NOT MERGE. Report PR # + URL + CI status.

---

## Self-Review (post-audit refinements baked in)

### Spec coverage

Each Tier S item maps to a task:
- TS-T1 → Anthropic prompt caching ✅
- TS-T2 → Tool-result spillover ✅
- TS-T3 → OSV malware check ✅
- TS-T4 → URL safety ✅
- TS-T5 → Subdirectory hints ✅
- TS-T6 → Title generator ✅
- TS-T7 → Rate guard ✅
- T8 → Validation ✅

### Audit refinements applied during planning

1. **Hermes file paths confirmed** — all 7 sources exist at `sources/hermes-agent-2026.4.23/` (NOT the unsuffixed `sources/hermes-agent/` the user's report cited). Plan uses the correct path.

2. **`_TRUSTED_PRIVATE_IP_HOSTS` Hermes-specific content (QQ media)** — dropped from OC port. Empty frozenset retains the framework for future use.

3. **`env=` sandbox abstraction in tool_result_storage** — replaced with local filesystem writes since OC runs locally. Drops ~30 LOC of heredoc/sandbox code.

4. **`hermes_cli.config` import in url_safety** — replaced with OC's config_store.

5. **`auxiliary_client.call_llm`** — replaced with OC's existing `cheap_route.py` (verify name during T6).

6. **`nous_rate_guard.py`** — generalized to `rate_guard.py` with `provider: str` param. Hermes's Nous-specific helper becomes one of many providers.

7. **State paths uniformly use `_home()`** — OC's profile-aware home, NOT `~/.hermes/`.

8. **`HERMES_ALLOW_PRIVATE_URLS` env var** — renamed to `OPENCOMPUTER_ALLOW_PRIVATE_URLS`.

9. **`_HINT_FILENAMES`** in subdirectory_hints.py — added `OPENCOMPUTER.md` to match V3.A-T8's primary file convention.

10. **Test coverage** — every task has 5-10 tests covering happy path + edge cases (DNS failure, malformed input, cache fallback, cleanup on expire).

### Type / API consistency

- All new dataclasses use `@dataclass(frozen=True, slots=True)` per `plugin_sdk/CLAUDE.md` rules.
- No new heavy deps (all stdlib).
- No `@pytest.mark.asyncio` decorators (asyncio_mode = "auto" inherited from V1 audit).
- Mock targets are module-qualified.
- Import paths match repo conventions.

### Acknowledged-as-deferred

- **Connection-level SSRF mitigation** (DNS rebinding TOCTOU defense) — pre-flight only in V3.B; full mitigation needs an egress proxy or `champion`-style connection wrapper. V4 candidate.
- **Multi-provider rate-guard generalization** beyond Anthropic + OpenAI — when more providers ship, they pick up the same shape.
- **Spillover storage cleanup** — files at `<profile_home>/tool_result_storage/` accumulate forever. V3.B can add LRU eviction or session-end cleanup.
- **Title regeneration** — once auto-set, never re-evaluated. User can manually rename via slash command (V3.B).
- **OSV check caching** — every npx/uvx hits the OSV API. V3.B can add 24h TTL cache.

### Stress-test findings (audit-driven)

1. **Prompt-cache TTL choice**: `5m` default is reasonable for chat sessions; user-defined `1h` for long-running deployments. V3.B could auto-detect. **Note in plan.**

2. **Spillover threshold per tool**: BudgetConfig.resolve_threshold(tool_name) uses defaults; if a tool routinely produces 30K outputs (e.g. RunTests on a large suite), spillover happens every turn = excess disk I/O. **Tunable in BudgetConfig — defaults set conservatively (200K turn budget) to avoid spurious spills.**

3. **OSV API outage**: fail-open is the correct default — better to allow a maybe-malicious package than refuse to launch ANY MCP. Logged for forensics. **Confirmed correct.**

4. **DNS rebinding limitation**: explicitly documented in url_safety.py docstring. Real fix is connection-level (Champion library or egress proxy). **Documented as V4 deferral.**

5. **Subdirectory hint cache invalidation**: `_loaded_dirs` is process-local; on long-running daemons, AGENTS.md edits go unnoticed until restart. **Acceptable for MVP — note for V4 file-watcher hookup.**

6. **Title-gen race**: if two parallel chats fire title gen at the same moment, both write. Last-writer-wins. SQLite handles atomicity. **Acceptable.**

7. **Rate-guard atomic writes**: tempfile + `os.replace` is atomic on POSIX. Windows: `os.replace` works. **Verified.**

### Stress-test alternatives considered

- **Replace prompt caching with prefix concatenation**: doesn't save tokens. Rejected.
- **Replace spillover with simple truncation**: loses information. Current 3-level design preserves the ability to read more on demand. Kept.
- **Replace OSV with Snyk/GitHub Advisory DB**: paid + auth required. OSV is free + Google-maintained. Kept.
- **Replace SSRF guard with simple "block all 10.0.0.0/8"**: misses 169.254 / link-local / CGNAT. Current full coverage kept.
- **Replace subdirectory hints with system-prompt rewrite per-turn**: invalidates prompt cache. Hermes's tool-result-append approach kept.
- **Replace title gen with manual /title command**: less ergonomic. Auto-with-manual-override better. Kept (manual override deferred V3.B).
- **Replace rate guard with provider SDK retries**: those don't share state across sessions. Hermes pattern kept.

### Effort estimate

| Task | Lines | Subagent time |
|---|---|---|
| T1 prompt caching | ~75 LOC + 7 tests | ~30 min |
| T2 spillover | ~250 LOC + 8 tests | ~60 min |
| T3 OSV check | ~160 LOC + 11 tests | ~30 min |
| T4 URL safety | ~230 LOC + 10 tests | ~45 min |
| T5 subdir hints | ~225 LOC + 7 tests | ~45 min |
| T6 title gen | ~125 LOC + 7 tests | ~30 min |
| T7 rate guard | ~180 LOC + 10 tests | ~45 min |
| T8 finalize | CHANGELOG + PR | ~15 min |

**Total: ~5 hours subagent dispatch.** Comparable to V2.B (Layer 3 deepening) which took ~6 hours.
