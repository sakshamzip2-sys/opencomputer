# Hermes Config v2 Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the ~/.opencomputer/ config-management gap between OpenComputer and the Hermes config-v2 reference doc — `${VAR}` substitution, secret-routing on `oc config set`, `oc config check`, top-level `timezone:`, `auxiliary.compression.*` nested slot, `privacy.redact_pii`, `security.redact_secrets`, plus three cheap polish knobs.

**Architecture:** Single-PR additive scope. All ten features are backward-compatible (new fields with safe defaults; flat `summary_model` shape preserved alongside the new nested `compression` slot). Pure config-layer + gateway-layer plumbing — no provider-extension surgery. Worktree lives at `~/.claude/worktrees/hermes-config-v2-2026-05-08` on branch `feat/hermes-config-v2-2026-05-08` from `origin/main` (`abb3d9ce`).

**Tech Stack:** Python 3.13, dataclasses, PyYAML, Typer (CLI), pytest, ruff, SQLite (sessions.db), `zoneinfo` stdlib (timezone), `hmac` + `hashlib` stdlib (PII hashing).

**Spec:** `docs/superpowers/specs/2026-05-08-hermes-config-v2-foundation-design.md`

**Branch + worktree (already created — verify before any task):**

```bash
git -C ~/Vscode/claude/.claude/worktrees/hermes-config-v2-2026-05-08 status
# expect: clean, on feat/hermes-config-v2-2026-05-08
```

All tasks below run from inside the worktree. Per parallel-sessions memory rule (2026-05-01): never share a working tree on one branch with another Claude session.

---

## Task 0: API verification (prevent silent-API-drift)

Per Karpathy rule from memory ("Think Before Coding — verify APIs!"), confirm every external contract this plan touches before writing code. Each finding here gets a one-line note in the executor's notepad.

- [ ] **0.1 — `Config` dataclass tree shape AND `_apply_overrides` nested handling**

```bash
cd ~/Vscode/claude/.claude/worktrees/hermes-config-v2-2026-05-08
grep -nE "^class (Config|LoopConfig|SessionConfig|GatewayConfig|MemoryConfig|MCPConfig|ToolsConfig|DeepeningConfig|SystemControlConfig)" opencomputer/agent/config.py
grep -nA20 "def _apply_overrides" opencomputer/agent/config_store.py | head -40
```

Confirm field names. Then read `_apply_overrides` body — does it recurse into nested dataclasses (e.g., when `raw["privacy"] = {"redact_pii": True}` and `Config.privacy: PrivacyConfig`, does it construct `PrivacyConfig(redact_pii=True)`?). If recursion is shallow-only, add a thin manual coercion in `load_config` for the new nested fields BEFORE writing test code in Tasks 5/6/7. Document the finding.

- [ ] **0.2 — `AuxiliaryConfig` shape**

```bash
grep -nE "class AuxiliaryConfig|summary_model|classify_model|extract_model|title_model|temperature" opencomputer/agent/auxiliary_client.py | head -20
```

Confirm flat `summary_model: str | None` etc. exist (per gap survey). If renamed already, adjust Task 5.

- [ ] **0.3 — `load_config` signature**

```bash
grep -nE "def load_config|def _apply_overrides|yaml.safe_load" opencomputer/agent/config_store.py | head -10
```

Confirm `load_config(path: Path | None = None) -> Config` and that `_apply_overrides(base, raw_dict)` is the right hook for env-var substitution to land between `safe_load` and override application.

- [ ] **0.4 — `oc config set` location + dotted-key parser**

```bash
grep -nE "def (config_set|_set|.*set.*config)" opencomputer/cli.py | head -10
```

Find the existing `oc config set` handler. Read it to identify how dotted keys (`memory.provider`) are parsed. Reuse the same parser for the secret-routing path.

- [ ] **0.5 — Existing `.env` reader/writer**

```bash
grep -rnE "\.env|dotenv|load_dotenv|env_file" opencomputer/ | head -20
```

Find any existing .env reader. Confirm what it returns and how it's loaded; use the same path for the new writer.

- [ ] **0.6 — Tool registry where `disabled_toolsets` will land**

```bash
grep -nE "def (build_tool_registry|register_tools|list_tools|get_tool_registry)" opencomputer/agent/*.py opencomputer/tools/*.py 2>/dev/null | head -15
```

Find the registry build site. Filter happens after per-platform tool config (per Hermes spec). Confirm the file:line.

- [ ] **0.7 — Retry layer where `api_max_retries` will land**

```bash
grep -nE "max_retries|retry|backoff" opencomputer/providers/base_provider.py opencomputer/providers/openai*.py 2>/dev/null | head -15
```

Find the existing retry knob. Confirm its current default and where to plumb the config value through.

- [ ] **0.8 — Session prune + VACUUM site**

```bash
grep -nE "def (auto_prune|prune|vacuum)|VACUUM" opencomputer/agent/state.py opencomputer/sessions/*.py 2>/dev/null | head -10
```

Find the prune sweep. Confirm where to add the conditional VACUUM call.

- [ ] **0.9 — Gateway message-ingest chokepoint per adapter**

```bash
grep -rnE "def (handle_message|on_message|process_inbound|deliver_to_session)" opencomputer/gateway/ extensions/*-channel/ 2>/dev/null | head -20
```

Identify where inbound messages cross from adapter into shared "in-session" handling. PII hashing happens at that boundary.

- [ ] **0.10 — Tool output → context handoff site**

```bash
grep -rnE "tool_result|tool_output|format_tool_response" opencomputer/agent/ 2>/dev/null | head -15
```

Identify where tool output is normalized before LLM consumption. `redact_secrets` hooks in here (off by default).

- [ ] **0.11 — System prompt time injection**

```bash
grep -rnE "datetime.now|datetime\.utcnow|strftime|isoformat" opencomputer/agent/system_prompt*.py opencomputer/agent/prompt*.py 2>/dev/null | head -10
```

Confirm where the system prompt embeds the current time. Timezone change goes there.

- [ ] **0.12 — Cron scheduler timezone**

```bash
grep -nE "schedule|cron|tzinfo|ZoneInfo" opencomputer/cron/*.py 2>/dev/null | head -10
```

Find scheduler. Confirm how it currently handles timezones (likely UTC implicit). Plumb `cfg.timezone` through.

- [ ] **0.13 — Document drift findings**

Add a short "API drift findings" subsection at the top of your working notepad. Each task that needs a fix gets a 1-line note prepended in the task header. If you find that a method signature doesn't match what later tasks assume, fix the assumption in the plan tasks BEFORE writing test code.

---

## File Map

**Create:**
- `opencomputer/agent/redactors.py` — secret-pattern regex set + `redact_secrets_in_text()`
- `opencomputer/gateway/pii.py` — `hash_user_id`, `hash_chat_id`, salt management
- `tests/test_config_env_substitution.py`
- `tests/test_config_set_routing.py`
- `tests/test_config_check.py`
- `tests/test_timezone_config.py`
- `tests/test_aux_compression_slot.py`
- `tests/test_privacy_redact_pii.py`
- `tests/test_redact_secrets.py`
- `tests/test_disabled_toolsets.py`
- `tests/test_api_max_retries.py`
- `tests/test_sessions_vacuum.py`

**Modify:**
- `opencomputer/agent/config_store.py` — add `_expand_env_vars` + call it in `load_config`
- `opencomputer/agent/config.py` — `PrivacyConfig`, `SecurityConfig`, `timezone:` on `Config`, new fields on `LoopConfig`/`SessionConfig`
- `opencomputer/agent/auxiliary_client.py` — `AuxSlotConfig` + `compression: AuxSlotConfig | None`
- `opencomputer/cli.py` — `config set` secret-routing + `--secret`/`--public` + `config check [--fix]`
- `opencomputer/agent/system_prompt.py` (or equivalent) — tz-aware time
- `opencomputer/cron/scheduler.py` (or equivalent) — pass `tzinfo`
- `opencomputer/gateway/<message-ingest>.py` (per Task 0.9 finding) — pii hook
- `opencomputer/agent/<tool output sink>` (per Task 0.10 finding) — redact-secrets hook
- `opencomputer/agent/state.py` (or wherever prune lives, per Task 0.8) — `VACUUM` after prune
- `opencomputer/<tool registry>` (per Task 0.6) — `disabled_toolsets` filter
- `opencomputer/providers/base_provider.py` (per Task 0.7) — `api_max_retries` knob

---

## Task 1: `${VAR}` env-var substitution in config loader

**Files:**
- Create: `tests/test_config_env_substitution.py`
- Modify: `opencomputer/agent/config_store.py`

- [ ] **Step 1.1: Write failing test for basic substitution**

Create `tests/test_config_env_substitution.py`:

```python
"""Tests for ${VAR} substitution in config.yaml.

Hermes config v2 contract:
- Only ${VAR} syntax is expanded; bare $VAR is not.
- Multiple references in one value work: "${HOST}:${PORT}".
- Undefined vars are kept verbatim (${UNDEFINED}).
- Single-pass: no recursive expansion.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.config_store import _expand_env_vars, load_config


def test_substitutes_defined_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abc-123")
    out = _expand_env_vars({"api_key": "${OPENAI_API_KEY}"})
    assert out == {"api_key": "sk-abc-123"}


def test_keeps_undefined_var_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNDEFINED_VAR", raising=False)
    out = _expand_env_vars({"x": "${UNDEFINED_VAR}"})
    assert out == {"x": "${UNDEFINED_VAR}"}


def test_multiple_refs_in_one_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST", "localhost")
    monkeypatch.setenv("PORT", "8080")
    out = _expand_env_vars({"url": "${HOST}:${PORT}"})
    assert out == {"url": "localhost:8080"}


def test_does_not_expand_bare_dollar_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "secret")
    out = _expand_env_vars({"x": "$API_KEY"})
    assert out == {"x": "$API_KEY"}


def test_walks_nested_dicts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEY", "v")
    out = _expand_env_vars({"outer": {"inner": "${KEY}"}})
    assert out == {"outer": {"inner": "v"}}


def test_walks_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("V", "x")
    out = _expand_env_vars({"items": ["${V}", "${V}-suffix"]})
    assert out == {"items": ["x", "x-suffix"]}


def test_leaves_non_string_values_untouched() -> None:
    out = _expand_env_vars({"n": 42, "b": True, "f": 3.14, "none": None})
    assert out == {"n": 42, "b": True, "f": 3.14, "none": None}


def test_single_pass_no_recursion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Value of OUTER is the literal string '${INNER}'. After one pass,
    we should see '${INNER}' (literal); we do NOT recurse to resolve INNER.
    """
    monkeypatch.setenv("OUTER", "${INNER}")
    monkeypatch.setenv("INNER", "deep_value")
    out = _expand_env_vars({"x": "${OUTER}"})
    assert out == {"x": "${INNER}"}


def test_load_config_applies_env_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration: env vars are substituted at load_config() time."""
    monkeypatch.setenv("MY_TEST_KEY", "hello-world")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "memory:\n  provider: ${MY_TEST_KEY}\n", encoding="utf-8"
    )
    cfg = load_config(cfg_path)
    assert cfg.memory.provider == "hello-world"
```

- [ ] **Step 1.2: Run the test to verify it fails**

```bash
cd ~/Vscode/claude/.claude/worktrees/hermes-config-v2-2026-05-08
pytest tests/test_config_env_substitution.py -v
```

Expected: All tests fail with `ImportError: cannot import name '_expand_env_vars'`.

- [ ] **Step 1.3: Implement `_expand_env_vars`**

Edit `opencomputer/agent/config_store.py`. Add at top (after existing imports):

```python
import os
import re
from typing import Any

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env_vars(value: Any) -> Any:
    """Recursively walk a dict/list, substituting ${VAR} in string values.

    Hermes config v2 contract:
    - ``${VAR}`` syntax only — bare ``$VAR`` not expanded.
    - Multiple references per value supported.
    - Undefined vars kept verbatim.
    - Single-pass: no recursive expansion.
    """
    if isinstance(value, str):
        def _sub(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(0))
        return _ENV_VAR_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    return value
```

- [ ] **Step 1.4: Wire into `load_config`**

Find this block in `opencomputer/agent/config_store.py:236-266` (verify exact lines after Task 0):

```python
    if not isinstance(raw, dict):
        raise RuntimeError(f"Config file {cfg_path} must be a YAML mapping")

    # Extract and parse the hooks block before applying regular overrides
    hooks_block = raw.pop("hooks", None)
    parsed_hooks = _parse_hooks_block(hooks_block)
```

Insert one line BEFORE the `hooks_block` extraction:

```python
    if not isinstance(raw, dict):
        raise RuntimeError(f"Config file {cfg_path} must be a YAML mapping")

    # Hermes-v2 ${VAR} substitution — applied before any further parsing.
    raw = _expand_env_vars(raw)

    # Extract and parse the hooks block before applying regular overrides
    hooks_block = raw.pop("hooks", None)
    parsed_hooks = _parse_hooks_block(hooks_block)
```

- [ ] **Step 1.5: Run the test to verify it passes**

```bash
pytest tests/test_config_env_substitution.py -v
```

Expected: 9 PASSED.

- [ ] **Step 1.6: Commit**

```bash
git add tests/test_config_env_substitution.py opencomputer/agent/config_store.py
git commit -m "feat(config): \${VAR} substitution in config.yaml loader

Hermes config v2 contract:
- \${VAR} only; bare \$VAR not expanded.
- Multiple references per value.
- Undefined vars kept verbatim.
- Single-pass; no recursion.

Lets users keep secrets in .env and reference them from config.yaml
(e.g., api_key: \${OPENAI_API_KEY})."
```

---

## Task 2: `oc config set` secret-routing → .env

**Files:**
- Create: `tests/test_config_set_routing.py`
- Modify: `opencomputer/cli.py`
- New helper: `opencomputer/agent/env_writer.py`

- [ ] **Step 2.1: Write failing tests**

Create `tests/test_config_set_routing.py`:

```python
"""Tests for `oc config set` secret-routing.

Heuristic: keys matching API_KEY|TOKEN|SECRET|PASSWORD|WEBHOOK_URL pattern
go to .env. Everything else goes to config.yaml. Override with --secret/--public.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.env_writer import (
    is_secret_key,
    write_env_var,
)
from opencomputer.cli import app


runner = CliRunner()


def test_is_secret_key_recognizes_api_key() -> None:
    assert is_secret_key("OPENAI_API_KEY")
    assert is_secret_key("openai_api_key")
    assert is_secret_key("custom.api_key")


def test_is_secret_key_recognizes_token() -> None:
    assert is_secret_key("GITHUB_TOKEN")
    assert is_secret_key("github_token")


def test_is_secret_key_recognizes_password() -> None:
    assert is_secret_key("DB_PASSWORD")


def test_is_secret_key_recognizes_secret() -> None:
    assert is_secret_key("CLIENT_SECRET")
    assert is_secret_key("APP_SECRET")


def test_is_secret_key_recognizes_webhook_url() -> None:
    assert is_secret_key("SLACK_WEBHOOK_URL")


def test_is_secret_key_rejects_non_secret_keys() -> None:
    assert not is_secret_key("memory.provider")
    assert not is_secret_key("max_iterations")
    assert not is_secret_key("language")


def test_write_env_var_creates_file_with_0600(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    write_env_var(env_path, "TEST_KEY", "abc")
    assert env_path.exists()
    assert env_path.read_text() == "TEST_KEY=abc\n"
    mode = env_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_env_var_appends_new_key(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=v\n")
    write_env_var(env_path, "TEST_KEY", "abc")
    contents = env_path.read_text()
    assert "EXISTING=v" in contents
    assert "TEST_KEY=abc" in contents


def test_write_env_var_updates_existing_key(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=old_value\nBAR=other\n")
    write_env_var(env_path, "FOO", "new_value")
    contents = env_path.read_text()
    assert "FOO=new_value" in contents
    assert "FOO=old_value" not in contents
    assert "BAR=other" in contents


def test_write_env_var_quotes_values_with_spaces(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    write_env_var(env_path, "MULTI", "hello world")
    assert env_path.read_text() == 'MULTI="hello world"\n'


def test_cli_config_set_routes_api_key_to_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["config", "set", "OPENAI_API_KEY", "sk-test"])
    assert result.exit_code == 0
    assert "Wrote OPENAI_API_KEY to" in result.stdout
    assert ".env" in result.stdout
    env_file = tmp_path / "default" / ".env"
    assert env_file.exists()
    assert "OPENAI_API_KEY=sk-test" in env_file.read_text()


def test_cli_config_set_routes_non_secret_to_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["config", "set", "memory.provider", "honcho"])
    assert result.exit_code == 0
    assert "Wrote memory.provider to" in result.stdout
    assert "config.yaml" in result.stdout
    env_file = tmp_path / "default" / ".env"
    assert not env_file.exists() or "memory" not in env_file.read_text()


def test_cli_config_set_secret_flag_forces_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(
        app, ["config", "set", "--secret", "memory.provider", "honcho"]
    )
    assert result.exit_code == 0
    assert ".env" in result.stdout
    env_file = tmp_path / "default" / ".env"
    assert env_file.exists()


def test_cli_config_set_public_flag_forces_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(
        app, ["config", "set", "--public", "OPENAI_API_KEY", "sk-test"]
    )
    assert result.exit_code == 0
    assert "config.yaml" in result.stdout
    # Warning should be visible
    assert "warning" in result.stdout.lower() or "WARN" in result.stdout
```

- [ ] **Step 2.2: Run test to verify it fails**

```bash
pytest tests/test_config_set_routing.py -v
```

Expected: All fail (`ImportError` on `env_writer`).

- [ ] **Step 2.3: Implement env_writer module**

Create `opencomputer/agent/env_writer.py`:

```python
"""Atomic .env file reader/writer for `oc config set` secret-routing.

The Hermes config v2 contract:
- Keys matching ``API_KEY|TOKEN|SECRET|PASSWORD|WEBHOOK_URL`` go to .env.
- Existing values updated in place; new values appended.
- File written with mode 0600 (owner-only).
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_SECRET_PATTERN = re.compile(
    r"(?i)(^|[._])(api[_-]?key|token|secret|password|webhook[_-]?url)$"
)


def is_secret_key(key: str) -> bool:
    """Return True if ``key`` matches the conservative secret-name heuristic.

    Matches: ``OPENAI_API_KEY``, ``GITHUB_TOKEN``, ``CLIENT_SECRET``,
    ``DB_PASSWORD``, ``SLACK_WEBHOOK_URL``, dotted ``custom.api_key``.
    Rejects: ``memory.provider``, ``max_iterations``, ``language``.
    """
    return bool(_SECRET_PATTERN.search(key))


def _quote_if_needed(value: str) -> str:
    """Quote a value for .env if it contains whitespace, quotes, or =."""
    if not value:
        return '""'
    if any(c in value for c in (" ", "\t", "\n", '"', "'", "=", "#")):
        # Use double quotes; escape any embedded double quotes.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def write_env_var(env_path: Path, key: str, value: str) -> None:
    """Write KEY=VALUE to ``env_path`` atomically.

    - If ``env_path`` exists, update existing line for ``key`` or append.
    - File created with mode 0600.
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    quoted = _quote_if_needed(value)
    new_line = f"{key}={quoted}"
    found = False
    out_lines: list[str] = []
    key_prefix = f"{key}="
    for line in existing_lines:
        # Preserve comments and blank lines.
        stripped = line.lstrip()
        if stripped.startswith("#") or not stripped:
            out_lines.append(line)
            continue
        if stripped.startswith(key_prefix):
            if not found:
                out_lines.append(new_line)
                found = True
            # Drop further duplicates.
            continue
        out_lines.append(line)
    if not found:
        out_lines.append(new_line)

    body = "\n".join(out_lines) + "\n"
    # Atomic write: tempfile in same dir + rename.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(env_path.parent), prefix=".env.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, env_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
```

- [ ] **Step 2.4: Wire into `oc config set`**

After Task 0.4, you know where the existing `config set` lives in `cli.py`. Modify it to add `--secret` / `--public` flags + the routing heuristic + visible "Wrote X to Y" message.

Locate the existing `config set` Typer command (around `cli.py:3614`). Add two parameters:

```python
@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key (dotted) or env-var name."),
    value: str = typer.Argument(..., help="Value to set."),
    secret: bool = typer.Option(False, "--secret", help="Force write to .env."),
    public: bool = typer.Option(False, "--public", help="Force write to config.yaml."),
    profile: str | None = typer.Option(None, "--profile", help="Profile name."),
) -> None:
    """Set a config value. Secrets auto-route to .env; others to config.yaml."""
    from opencomputer.agent.env_writer import is_secret_key, write_env_var
    from opencomputer.agent.config_store import (
        config_dir,
        config_file_path,
        env_file_path,
    )

    if secret and public:
        typer.secho(
            "error: --secret and --public are mutually exclusive",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    profile_name = profile or _active_profile_name()  # existing helper
    env_path = env_file_path(profile_name)  # ~/.opencomputer/<profile>/.env
    yaml_path = config_file_path(profile_name)

    # Decide route.
    if secret:
        target = "env"
    elif public:
        target = "yaml"
    else:
        target = "env" if is_secret_key(key) else "yaml"

    if target == "env":
        write_env_var(env_path, key, value)
        typer.secho(
            f"✓ Wrote {key} to {env_path} (.env, mode 0600)",
            fg=typer.colors.GREEN,
        )
        if public:
            typer.secho(
                "WARNING: --public was used to force a likely secret into config.yaml",
                fg=typer.colors.YELLOW,
            )
    else:
        # Existing YAML write path — call the existing helper.
        _config_set_yaml(yaml_path, key, value)  # existing helper from Task 0.4
        typer.secho(
            f"✓ Wrote {key} to {yaml_path} (config.yaml)",
            fg=typer.colors.GREEN,
        )
        if public and is_secret_key(key):
            typer.secho(
                f"WARNING: {key} looks like a secret but was written to config.yaml "
                f"because --public was used. Consider .env for secrets.",
                fg=typer.colors.YELLOW,
            )
```

If `env_file_path` doesn't exist in `config_store.py`, add it (small helper alongside `config_file_path`):

```python
def env_file_path(profile: str | None = None) -> Path:
    """Return ``~/.opencomputer/<profile>/.env``."""
    return config_dir(profile) / ".env"
```

If `_active_profile_name()` and `_config_set_yaml()` are named differently in the actual codebase (Task 0.4 finding), substitute in.

- [ ] **Step 2.5: Run the tests to verify they pass**

```bash
pytest tests/test_config_set_routing.py -v
```

Expected: 12 PASSED.

- [ ] **Step 2.6: Commit**

```bash
git add tests/test_config_set_routing.py opencomputer/agent/env_writer.py opencomputer/cli.py opencomputer/agent/config_store.py
git commit -m "feat(config): oc config set secret-routing → .env

- Heuristic: keys matching API_KEY|TOKEN|SECRET|PASSWORD|WEBHOOK_URL
  auto-route to .env (mode 0600).
- --secret / --public flags override the heuristic.
- Visible 'Wrote X to Y' message; warning when --public moves a likely
  secret into config.yaml.
- env_writer.py: atomic write (tempfile + rename), update-or-append."
```

---

## Task 3: `oc config check`

**Files:**
- Create: `tests/test_config_check.py`
- Modify: `opencomputer/cli.py`

- [ ] **Step 3.1: Write failing tests**

Create `tests/test_config_check.py`:

```python
"""Tests for `oc config check` — find missing options post-update."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli import app


runner = CliRunner()


def test_check_reports_no_missing_when_full_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config.yaml with every nested block present has no missing items."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_dir = tmp_path / "default"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text(
        "memory:\n  provider: builtin\n"
        "privacy:\n  redact_pii: false\n"
        "security:\n  redact_secrets: false\n"
        "timezone: \"America/New_York\"\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0
    assert "no missing" in result.stdout.lower() or "all expected" in result.stdout.lower()


def test_check_reports_missing_top_level_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare config.yaml flags `privacy`, `security`, `timezone` as missing."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_dir = tmp_path / "default"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text(
        "memory:\n  provider: builtin\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0
    assert "privacy" in result.stdout
    assert "security" in result.stdout
    assert "timezone" in result.stdout


def test_check_fix_writes_default_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`config check --fix` adds missing nested blocks with their defaults."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_dir = tmp_path / "default"
    cfg_dir.mkdir(parents=True)
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text("memory:\n  provider: builtin\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "check", "--fix"])
    assert result.exit_code == 0
    contents = cfg_path.read_text()
    assert "privacy:" in contents
    assert "security:" in contents
    assert "timezone:" in contents


def test_check_fix_does_not_overwrite_user_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`config check --fix` is purely additive."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg_dir = tmp_path / "default"
    cfg_dir.mkdir(parents=True)
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(
        "memory:\n  provider: honcho\n"   # user value
        "timezone: \"America/Los_Angeles\"\n",
        encoding="utf-8",
    )
    runner.invoke(app, ["config", "check", "--fix"])
    contents = cfg_path.read_text()
    assert "provider: honcho" in contents       # user value preserved
    assert "America/Los_Angeles" in contents    # user value preserved
    assert "privacy:" in contents                # newly added
```

- [ ] **Step 3.2: Run test to verify it fails**

```bash
pytest tests/test_config_check.py -v
```

Expected: All fail (no `check` subcommand).

- [ ] **Step 3.3: Implement `config check` subcommand**

Add to `opencomputer/cli.py` under the `config` Typer group. Identify which top-level Config dataclass blocks should be in a "minimal" YAML manifest by introspecting the dataclass tree:

```python
@config_app.command("check")
def config_check(
    fix: bool = typer.Option(
        False, "--fix", help="Add missing top-level blocks with their default values."
    ),
    profile: str | None = typer.Option(None, "--profile"),
) -> None:
    """Find config.yaml keys that are missing relative to the bundled defaults.

    Reports nested top-level blocks (``privacy``, ``security``, ``timezone``,
    etc.) that aren't present in the user's config.yaml. With ``--fix``, adds
    them with their dataclass defaults — purely additive.
    """
    import yaml

    from opencomputer.agent.config_store import (
        config_file_path,
        default_config,
        save_config,
    )

    # The set of top-level keys we expect users to have a stake in
    # configuring. Skip noisy/derived blocks (e.g. internal hook lists).
    EXPECTED_TOP_LEVEL = {
        "memory", "model", "loop", "session", "mcp", "tools", "deepening",
        "gateway", "system_control", "auxiliary", "privacy", "security",
        "timezone", "sessions", "checkpoints", "worktree",
    }
    profile_name = profile or _active_profile_name()
    cfg_path = config_file_path(profile_name)
    raw: dict = {}
    if cfg_path.exists():
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            typer.secho(f"error: cannot parse {cfg_path}: {exc}", err=True, fg=typer.colors.RED)
            raise typer.Exit(1)
    if not isinstance(raw, dict):
        raw = {}

    present = set(raw.keys())
    missing = sorted(EXPECTED_TOP_LEVEL - present)

    if not missing:
        typer.secho("✓ no missing top-level config blocks", fg=typer.colors.GREEN)
        return

    typer.echo(f"Missing top-level config blocks ({len(missing)}):")
    for key in missing:
        typer.echo(f"  ✗ {key}")
    typer.echo()
    if fix:
        defaults = default_config()
        # Add minimal default blocks. Use _to_yaml_dict to serialize.
        from opencomputer.agent.config_store import _to_yaml_dict
        defaults_dict = _to_yaml_dict(defaults)
        for key in missing:
            if key in defaults_dict:
                raw[key] = defaults_dict[key]
            elif key == "timezone":
                raw["timezone"] = ""
            elif key == "privacy":
                raw["privacy"] = {"redact_pii": False}
            elif key == "security":
                raw["security"] = {"redact_secrets": False}
        # Write back atomically using existing save path or yaml.safe_dump.
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            yaml.safe_dump(raw, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        typer.secho(
            f"✓ added {len(missing)} block(s) to {cfg_path}",
            fg=typer.colors.GREEN,
        )
    else:
        typer.echo("Run `oc config check --fix` to add them with defaults.")
```

- [ ] **Step 3.4: Run the tests to verify they pass**

```bash
pytest tests/test_config_check.py -v
```

Expected: 4 PASSED.

- [ ] **Step 3.5: Commit**

```bash
git add tests/test_config_check.py opencomputer/cli.py
git commit -m "feat(config): oc config check [--fix]

Walks the bundled Config dataclass tree and reports top-level blocks
absent from config.yaml. --fix adds them with their dataclass defaults
(purely additive — never overwrites user values)."
```

---

## Task 4: Top-level `timezone:` IANA

**Files:**
- Create: `tests/test_timezone_config.py`
- Modify: `opencomputer/agent/config.py`, `opencomputer/agent/config_store.py`, system-prompt site (per Task 0.11), cron site (per Task 0.12)

- [ ] **Step 4.1: Write failing tests**

Create `tests/test_timezone_config.py`:

```python
"""Tests for top-level `timezone:` IANA config."""
from __future__ import annotations

import zoneinfo
from datetime import datetime
from pathlib import Path

import pytest

from opencomputer.agent.config import Config, default_config
from opencomputer.agent.config_store import load_config


def test_default_timezone_is_empty() -> None:
    cfg = default_config()
    assert cfg.timezone == ""


def test_load_config_accepts_valid_iana(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("timezone: \"America/New_York\"\n", encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.timezone == "America/New_York"
    # Confirm zoneinfo accepts it.
    zoneinfo.ZoneInfo(cfg.timezone)


def test_load_config_rejects_invalid_iana(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("timezone: \"Mars/Olympus_Mons\"\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="timezone"):
        load_config(cfg_path)


def test_load_config_accepts_empty_timezone(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("timezone: \"\"\n", encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.timezone == ""


def test_resolve_tzinfo_returns_zoneinfo_when_set() -> None:
    from opencomputer.agent.config import resolve_tzinfo
    cfg = Config(timezone="America/New_York")
    tz = resolve_tzinfo(cfg)
    assert isinstance(tz, zoneinfo.ZoneInfo)
    assert str(tz) == "America/New_York"


def test_resolve_tzinfo_returns_none_when_empty() -> None:
    from opencomputer.agent.config import resolve_tzinfo
    cfg = Config(timezone="")
    assert resolve_tzinfo(cfg) is None


def test_now_in_tz_uses_configured_zone() -> None:
    from opencomputer.agent.config import now_in_tz
    cfg = Config(timezone="UTC")
    now = now_in_tz(cfg)
    assert now.tzinfo is not None
    assert str(now.tzinfo) == "UTC"
```

- [ ] **Step 4.2: Run the tests to verify they fail**

```bash
pytest tests/test_timezone_config.py -v
```

Expected: All fail (`Config has no field timezone`, helpers missing).

- [ ] **Step 4.3: Add `timezone:` field + helpers**

Edit `opencomputer/agent/config.py`. Find the `@dataclass class Config` block and add:

```python
@dataclass(frozen=True, slots=True)
class Config:
    # ... existing fields ...
    timezone: str = ""  # IANA name; empty = server-local time.
```

At the bottom of the same file (or in a helpers block), add:

```python
def resolve_tzinfo(cfg: "Config") -> "zoneinfo.ZoneInfo | None":
    """Return ZoneInfo for ``cfg.timezone`` or None when unset."""
    import zoneinfo
    if not cfg.timezone:
        return None
    return zoneinfo.ZoneInfo(cfg.timezone)


def now_in_tz(cfg: "Config") -> "datetime":
    """``datetime.now()`` in ``cfg.timezone`` or naive when unset."""
    from datetime import datetime
    tz = resolve_tzinfo(cfg)
    if tz is None:
        return datetime.now()
    return datetime.now(tz)
```

- [ ] **Step 4.4: Validate at `load_config`**

In `opencomputer/agent/config_store.py:load_config`, after `_apply_overrides`:

```python
    cfg = _apply_overrides(base, raw)
    # Hermes-v2 timezone validation.
    if cfg.timezone:
        try:
            import zoneinfo
            zoneinfo.ZoneInfo(cfg.timezone)
        except Exception as exc:
            raise RuntimeError(
                f"Invalid timezone {cfg.timezone!r} in {cfg_path}: {exc}"
            ) from exc
    if parsed_hooks:
        # ... existing logic ...
```

- [ ] **Step 4.5: Use `now_in_tz` in system prompt**

Per Task 0.11, find the `datetime.now()` call in the system prompt builder. Replace:

```python
# OLD:
current_time = datetime.now()

# NEW:
from opencomputer.agent.config import now_in_tz
current_time = now_in_tz(cfg)
```

If the system-prompt builder doesn't currently take `cfg`, plumb it through (it almost certainly does; if not, pass via the existing `RuntimeContext`).

- [ ] **Step 4.6: Use `resolve_tzinfo` in cron scheduler**

Per Task 0.12, find the cron scheduler instantiation. Add `tzinfo=resolve_tzinfo(cfg)`. If the scheduler doesn't accept tzinfo (e.g., uses `time` instead of `datetime`), document the gap as a follow-up but at minimum store the cron tz in the config for log timestamps.

- [ ] **Step 4.7: Run the tests to verify they pass**

```bash
pytest tests/test_timezone_config.py -v
```

Expected: 7 PASSED.

- [ ] **Step 4.8: Commit**

```bash
git add tests/test_timezone_config.py opencomputer/agent/config.py opencomputer/agent/config_store.py opencomputer/agent/system_prompt*.py opencomputer/cron/*.py
git commit -m "feat(config): top-level timezone: IANA

- Validates at load_config (clear error on invalid name).
- resolve_tzinfo(cfg) + now_in_tz(cfg) helpers.
- System prompt time injection respects cfg.timezone.
- Cron scheduler passes tzinfo through.
- Empty string preserves existing server-local fallback."
```

---

## Task 5: `auxiliary.compression.*` nested slot

**Files:**
- Create: `tests/test_aux_compression_slot.py`
- Modify: `opencomputer/agent/auxiliary_client.py`

- [ ] **Step 5.1: Write failing tests**

Create `tests/test_aux_compression_slot.py`:

```python
"""Tests for `auxiliary.compression.*` nested config slot.

Hermes config v2 contract: when `auxiliary.compression.{provider, model,
base_url, api_key, timeout}` is set, takes precedence over flat
`summary_model:`. Backward-compat: flat shape continues to work unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.auxiliary_client import (
    AuxiliaryConfig,
    AuxSlotConfig,
)


def test_aux_slot_config_defaults() -> None:
    slot = AuxSlotConfig()
    assert slot.provider == "auto"
    assert slot.model == ""
    assert slot.base_url == ""
    assert slot.api_key == ""
    assert slot.timeout == 120.0


def test_compression_slot_optional_default_none() -> None:
    cfg = AuxiliaryConfig()
    assert cfg.compression is None


def test_compression_slot_overrides_summary_model() -> None:
    """When `compression` is set, it takes precedence over `summary_model`."""
    from opencomputer.agent.auxiliary_client import effective_compression_model
    cfg = AuxiliaryConfig(
        summary_model="gpt-4o",
        compression=AuxSlotConfig(model="google/gemini-2.5-flash"),
    )
    assert effective_compression_model(cfg) == "google/gemini-2.5-flash"


def test_flat_summary_model_still_works_when_compression_unset() -> None:
    from opencomputer.agent.auxiliary_client import effective_compression_model
    cfg = AuxiliaryConfig(summary_model="gpt-4o")
    assert effective_compression_model(cfg) == "gpt-4o"


def test_default_compression_model_when_neither_set() -> None:
    from opencomputer.agent.auxiliary_client import (
        DEFAULT_MODEL_BY_TASK,
        effective_compression_model,
    )
    cfg = AuxiliaryConfig()
    assert effective_compression_model(cfg) == DEFAULT_MODEL_BY_TASK["summary"]


def test_load_config_parses_nested_compression_block(tmp_path: Path) -> None:
    """Round-trip via config_store: YAML → dataclass."""
    from opencomputer.agent.config_store import load_config
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "auxiliary:\n"
        "  compression:\n"
        "    provider: openrouter\n"
        "    model: google/gemini-2.5-flash\n"
        "    timeout: 90.0\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.auxiliary.compression is not None
    assert cfg.auxiliary.compression.provider == "openrouter"
    assert cfg.auxiliary.compression.model == "google/gemini-2.5-flash"
    assert cfg.auxiliary.compression.timeout == 90.0


def test_provider_main_alias_resolves_to_active(tmp_path: Path) -> None:
    """`provider: main` is an explicit alias of `auto` — both fall back to
    the active main provider; resolver returns the same value for either."""
    from opencomputer.agent.auxiliary_client import resolve_compression_provider
    cfg_main = AuxiliaryConfig(compression=AuxSlotConfig(provider="main"))
    cfg_auto = AuxiliaryConfig(compression=AuxSlotConfig(provider="auto"))
    main_provider = "openrouter"
    assert resolve_compression_provider(cfg_main, main_provider) == "openrouter"
    assert resolve_compression_provider(cfg_auto, main_provider) == "openrouter"


def test_base_url_takes_precedence_over_provider() -> None:
    from opencomputer.agent.auxiliary_client import resolve_compression_endpoint
    cfg = AuxiliaryConfig(
        compression=AuxSlotConfig(
            provider="openrouter",
            base_url="https://api.z.ai/api/coding/paas/v4",
        ),
    )
    endpoint = resolve_compression_endpoint(cfg)
    assert endpoint == "https://api.z.ai/api/coding/paas/v4"
```

- [ ] **Step 5.2: Run the tests to verify they fail**

```bash
pytest tests/test_aux_compression_slot.py -v
```

Expected: All fail (`ImportError: AuxSlotConfig`).

- [ ] **Step 5.3: Implement `AuxSlotConfig` + `compression` slot**

Edit `opencomputer/agent/auxiliary_client.py`. Find the `AuxiliaryConfig` dataclass at line 73-85. Replace with:

```python
@dataclass(frozen=True, slots=True)
class AuxSlotConfig:
    """Per-slot auxiliary model configuration (Hermes v2 shape).

    ``provider`` ``"auto"`` defaults to the active main provider; ``"main"``
    is an explicit alias for the same. Setting ``base_url`` takes precedence
    over ``provider`` — the slot uses an OpenAI-compatible client pointed at
    the URL.
    """

    provider: str = "auto"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout: float = 120.0


@dataclass(frozen=True, slots=True)
class AuxiliaryConfig:
    """Per-task model overrides. ``None`` falls back to ``DEFAULT_MODEL_BY_TASK``.

    Hermes v2 adds nested per-slot config (currently: ``compression``). When
    set, the nested form takes precedence over the flat ``summary_model``.
    """

    summary_model: str | None = None
    classify_model: str | None = None
    extract_model: str | None = None
    title_model: str | None = None
    temperature: float = 0.3
    # Hermes v2 nested compression slot. None = use flat summary_model.
    compression: AuxSlotConfig | None = None


def effective_compression_model(cfg: AuxiliaryConfig) -> str:
    """Resolve the compression model: nested.compression.model → flat
    summary_model → DEFAULT_MODEL_BY_TASK['summary']."""
    if cfg.compression is not None and cfg.compression.model:
        return cfg.compression.model
    if cfg.summary_model:
        return cfg.summary_model
    return DEFAULT_MODEL_BY_TASK["summary"]


def resolve_compression_provider(
    cfg: AuxiliaryConfig, main_provider: str
) -> str:
    """Resolve the compression provider name. ``auto``/``main``/empty →
    ``main_provider``; otherwise use the configured value."""
    if cfg.compression is None:
        return main_provider
    p = cfg.compression.provider
    if p in ("", "auto", "main"):
        return main_provider
    return p


def resolve_compression_endpoint(cfg: AuxiliaryConfig) -> str | None:
    """Return ``base_url`` when set; None means "use provider default"."""
    if cfg.compression is None or not cfg.compression.base_url:
        return None
    return cfg.compression.base_url
```

Also add `AuxSlotConfig` and the helper functions to the `__all__` block at the bottom of the file:

```python
__all__ = [
    "AuxiliaryClient",
    "AuxiliaryConfig",
    "AuxSlotConfig",
    "DEFAULT_MODEL_BY_TASK",
    "effective_compression_model",
    "resolve_compression_endpoint",
    "resolve_compression_provider",
]
```

- [ ] **Step 5.4: Wire `effective_compression_model` into the compaction caller**

The CompactionEngine (or wherever `_summarize` lives) currently calls `aux.config.summary_model`. Update to:

```python
from opencomputer.agent.auxiliary_client import effective_compression_model
model = effective_compression_model(self.aux.config)
```

If the call is inside `AuxiliaryClient.model_for("summary")`, update the resolver there to use `effective_compression_model(self.config)` first, then fall through to the flat field.

- [ ] **Step 5.5: Run the tests to verify they pass**

```bash
pytest tests/test_aux_compression_slot.py -v
```

Expected: 8 PASSED.

- [ ] **Step 5.6: Commit**

```bash
git add tests/test_aux_compression_slot.py opencomputer/agent/auxiliary_client.py
git commit -m "feat(config): auxiliary.compression nested slot (Hermes v2 shape)

- AuxSlotConfig dataclass: {provider, model, base_url, api_key, timeout}.
- AuxiliaryConfig.compression: AuxSlotConfig | None (nested form).
- Resolver order: nested.compression.model → flat summary_model →
  DEFAULT_MODEL_BY_TASK['summary'].
- provider 'auto'/'main' fall back to active main provider.
- base_url takes precedence over provider.
- Backward-compat: flat summary_model continues to work unchanged."
```

---

## Task 6: `privacy.redact_pii`

**Files:**
- Create: `opencomputer/gateway/pii.py`, `tests/test_privacy_redact_pii.py`
- Modify: `opencomputer/agent/config.py`, gateway message-ingest site (per Task 0.9)

- [ ] **Step 6.1: Write failing tests**

Create `tests/test_privacy_redact_pii.py`:

```python
"""Tests for privacy.redact_pii — gateway PII hashing."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_pii_salt_loaded_or_generated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import _load_or_create_salt
    salt1 = _load_or_create_salt()
    assert len(salt1) == 32
    salt_file = tmp_path / ".pii_salt"
    assert salt_file.exists()
    salt2 = _load_or_create_salt()
    assert salt1 == salt2  # deterministic across calls


def test_hash_user_id_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import hash_user_id
    h1 = hash_user_id("+1-555-123-4567")
    h2 = hash_user_id("+1-555-123-4567")
    assert h1 == h2
    assert h1 != "+1-555-123-4567"
    assert len(h1) >= 12  # not too short to avoid collisions


def test_hash_chat_id_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import hash_chat_id
    h1 = hash_chat_id("123456789")
    h2 = hash_chat_id("123456789")
    assert h1 == h2


def test_hash_user_and_chat_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import hash_user_id, hash_chat_id
    u = hash_user_id("123")
    c = hash_chat_id("123")
    assert u != c  # different namespaces


def test_redact_pii_disabled_by_default() -> None:
    from opencomputer.agent.config import default_config
    cfg = default_config()
    assert cfg.privacy.redact_pii is False


def test_apply_pii_redaction_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import maybe_redact_user_id
    assert maybe_redact_user_id("+1-555", redact=False) == "+1-555"


def test_apply_pii_redaction_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import maybe_redact_user_id
    h = maybe_redact_user_id("+1-555", redact=True)
    assert h != "+1-555"
    assert len(h) >= 12


def test_supported_adapter_set() -> None:
    from opencomputer.gateway.pii import SUPPORTED_ADAPTERS
    assert "whatsapp" in SUPPORTED_ADAPTERS
    assert "signal" in SUPPORTED_ADAPTERS
    assert "telegram" in SUPPORTED_ADAPTERS
    # Discord/Slack route IDs are already opaque per Hermes spec — not in scope.
    assert "discord" not in SUPPORTED_ADAPTERS
    assert "slack" not in SUPPORTED_ADAPTERS
```

- [ ] **Step 6.2: Run the tests to verify they fail**

```bash
pytest tests/test_privacy_redact_pii.py -v
```

Expected: All fail (`ImportError: pii`, no `privacy` field).

- [ ] **Step 6.3: Add `PrivacyConfig` to `Config`**

Edit `opencomputer/agent/config.py`. Add new dataclass:

```python
@dataclass(frozen=True, slots=True)
class PrivacyConfig:
    """Gateway-only privacy controls.

    ``redact_pii`` hashes phone/user/chat IDs before they enter the LLM
    context (deterministic — same ID always maps to same hash). Routing
    and delivery still use the original values internally. Supported
    adapters: WhatsApp, Signal, Telegram. Discord/Slack route IDs are
    already opaque and not in scope.
    """

    redact_pii: bool = False
```

Add to the `Config` dataclass:

```python
@dataclass(frozen=True, slots=True)
class Config:
    # ... existing ...
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
```

Update `_to_yaml_dict` in `config_store.py` to include `privacy:`:

```python
result: dict[str, Any] = {
    # ... existing ...
    "privacy": _encode(cfg.privacy),
}
```

Update `_apply_overrides` if it has a hardcoded list of overridable fields (verify in Task 0.1 — usually it iterates dataclass fields automatically and Just Works).

- [ ] **Step 6.4: Implement `pii.py`**

Create `opencomputer/gateway/pii.py`:

```python
"""Gateway-only PII hashing for privacy.redact_pii.

Hashes user/chat IDs deterministically (HMAC-SHA256 with per-installation
salt at ``~/.opencomputer/.pii_salt``). Same ID always maps to the same
hash, so the LLM still sees stable references, but the actual identity
is masked.

Routing/delivery still use original IDs — only the LLM-facing context
sees hashed forms. Supported adapters: WhatsApp, Signal, Telegram.
Discord/Slack route IDs are already opaque per Hermes spec.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

SUPPORTED_ADAPTERS = frozenset({"whatsapp", "signal", "telegram"})

_USER_NAMESPACE = b"user:"
_CHAT_NAMESPACE = b"chat:"


def _salt_path() -> Path:
    home = os.environ.get("OPENCOMPUTER_HOME") or os.path.expanduser("~/.opencomputer")
    return Path(home) / ".pii_salt"


def _load_or_create_salt() -> bytes:
    """Return the per-installation salt, creating it on first call."""
    p = _salt_path()
    if p.exists():
        salt = p.read_bytes()
        if len(salt) >= 32:
            return salt[:32]
    p.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    p.write_bytes(salt)
    p.chmod(0o600)
    return salt


def _hash_with_namespace(namespace: bytes, raw: str) -> str:
    """HMAC-SHA256(salt, namespace || raw) → first 16 hex chars."""
    salt = _load_or_create_salt()
    mac = hmac.new(salt, namespace + raw.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:16]


def hash_user_id(raw: str) -> str:
    """Deterministic user-ID hash (16 hex chars)."""
    return _hash_with_namespace(_USER_NAMESPACE, raw)


def hash_chat_id(raw: str) -> str:
    """Deterministic chat-ID hash (16 hex chars)."""
    return _hash_with_namespace(_CHAT_NAMESPACE, raw)


def maybe_redact_user_id(raw: str, *, redact: bool) -> str:
    """Apply user-ID hashing iff ``redact`` is True."""
    return hash_user_id(raw) if redact else raw


def maybe_redact_chat_id(raw: str, *, redact: bool) -> str:
    """Apply chat-ID hashing iff ``redact`` is True."""
    return hash_chat_id(raw) if redact else raw
```

- [ ] **Step 6.5: Hook into gateway message-ingest**

Per Task 0.9, you've identified the per-adapter ingest chokepoint. For each adapter in `SUPPORTED_ADAPTERS`, locate where the inbound message envelope is constructed for LLM context. Apply:

```python
from opencomputer.gateway.pii import maybe_redact_user_id, maybe_redact_chat_id

# In the ingest path that writes user_id / chat_id into the LLM context envelope:
context_user_id = maybe_redact_user_id(raw_user_id, redact=cfg.privacy.redact_pii)
context_chat_id = maybe_redact_chat_id(raw_chat_id, redact=cfg.privacy.redact_pii)
```

The ROUTING layer (delivery, ack, threading) MUST continue to use `raw_user_id` / `raw_chat_id`. Only the context envelope sees the redacted versions.

- [ ] **Step 6.6: Run the tests to verify they pass**

```bash
pytest tests/test_privacy_redact_pii.py -v
```

Expected: 8 PASSED.

- [ ] **Step 6.7: Commit**

```bash
git add tests/test_privacy_redact_pii.py opencomputer/gateway/pii.py opencomputer/agent/config.py opencomputer/agent/config_store.py opencomputer/gateway/
git commit -m "feat(privacy): privacy.redact_pii — gateway PII hashing

- HMAC-SHA256 with per-installation salt at ~/.opencomputer/.pii_salt.
- Deterministic: same user ID always maps to same hash.
- Supported: WhatsApp, Signal, Telegram (per Hermes v2 spec).
- Routing/delivery use original IDs internally; only LLM context sees hashes.
- Off by default (privacy.redact_pii: false)."
```

---

## Task 7: `security.redact_secrets`

**Files:**
- Create: `opencomputer/agent/redactors.py`, `tests/test_redact_secrets.py`
- Modify: `opencomputer/agent/config.py`, tool-output sink (per Task 0.10)

- [ ] **Step 7.1: Write failing tests**

Create `tests/test_redact_secrets.py`:

```python
"""Tests for security.redact_secrets — strip API key patterns."""
from __future__ import annotations

import pytest


def test_disabled_by_default() -> None:
    from opencomputer.agent.config import default_config
    cfg = default_config()
    assert cfg.security.redact_secrets is False


def test_redact_openai_style_key() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text
    out = redact_secrets_in_text("api_key: sk-abc123def456ghijklmnop")
    assert "sk-abc123def456" not in out
    assert "[REDACTED]" in out


def test_does_not_redact_short_sk_string() -> None:
    """Avoid false positives on short strings like 'sk-1' or 'sk-short'."""
    from opencomputer.agent.redactors import redact_secrets_in_text
    out = redact_secrets_in_text("not a key: sk-1")
    assert "sk-1" in out
    assert "[REDACTED]" not in out


def test_redact_github_pat_classic() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text
    pat = "ghp_" + "a" * 36
    out = redact_secrets_in_text(f"token: {pat}")
    assert pat not in out
    assert "[REDACTED]" in out


def test_redact_github_pat_fine_grained() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text
    pat = "github_pat_" + "a" * 22 + "_" + "b" * 59
    out = redact_secrets_in_text(f"token: {pat}")
    assert "[REDACTED]" in out


def test_redact_aws_access_key() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text
    out = redact_secrets_in_text("aws: AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_redact_slack_bot_token() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text
    tok = "xoxb-" + "a" * 30
    out = redact_secrets_in_text(f"token: {tok}")
    assert "[REDACTED]" in out


def test_redact_bearer_in_authorization_header() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text
    out = redact_secrets_in_text("Authorization: Bearer " + "x" * 40)
    assert "[REDACTED]" in out
    assert "x" * 40 not in out


def test_multiple_secrets_in_one_string() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text
    text = (
        "openai: sk-abcdefghij1234567890\n"
        "github: ghp_" + "z" * 36 + "\n"
    )
    out = redact_secrets_in_text(text)
    assert "[REDACTED]" in out
    assert out.count("[REDACTED]") == 2
```

- [ ] **Step 7.2: Run the tests to verify they fail**

```bash
pytest tests/test_redact_secrets.py -v
```

Expected: All fail (no `redactors` module).

- [ ] **Step 7.3: Add `SecurityConfig` to `Config`**

Edit `opencomputer/agent/config.py`. Add:

```python
@dataclass(frozen=True, slots=True)
class SecurityConfig:
    """Security-related controls.

    ``redact_secrets`` strips API-key patterns from tool output before they
    enter conversation context AND before they land in logs. Off by default
    to avoid false positives on legitimate file reads.
    """

    redact_secrets: bool = False


@dataclass(frozen=True, slots=True)
class Config:
    # ... existing ...
    security: SecurityConfig = field(default_factory=SecurityConfig)
```

Update `_to_yaml_dict` in `config_store.py` to include `security:`.

- [ ] **Step 7.4: Implement `redactors.py`**

Create `opencomputer/agent/redactors.py`:

```python
"""Secret-pattern redaction for security.redact_secrets.

Conservative regex set — opt-in (off by default). Patterns require enough
length to avoid false positives on legitimate strings (e.g., 'sk-1' is
NOT redacted, but 'sk-abc123def456...' IS).
"""
from __future__ import annotations

import re

# Patterns ordered by specificity. Each pattern matches a likely-secret
# substring and is replaced with [REDACTED].
_DEFAULT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenAI / Anthropic style: sk-<20+ alnum>.
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    # GitHub fine-grained PAT.
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    # GitHub classic PAT.
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    # GitHub OAuth.
    re.compile(r"gho_[A-Za-z0-9]{36}"),
    # Slack bot/user tokens.
    re.compile(r"xox[bpoa]-[A-Za-z0-9-]{20,}"),
    # AWS access keys.
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # Bearer in Authorization headers.
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9_.\-]{30,}"),
    # Generic long base64-ish token after `token=` / `key=` / `secret=`.
    re.compile(r"(?i)(?:token|key|secret|password)\s*[=:]\s*['\"]?[A-Za-z0-9_\-+/=]{30,}['\"]?"),
)


def redact_secrets_in_text(
    text: str, patterns: tuple[re.Pattern[str], ...] = _DEFAULT_PATTERNS
) -> str:
    """Replace each match of any pattern with ``[REDACTED]``.

    Conservative: patterns require length thresholds to avoid false positives.
    Multiple patterns may match overlapping text — first match wins.
    """
    for pattern in patterns:
        text = pattern.sub("[REDACTED]", text)
    return text
```

- [ ] **Step 7.5: Wire into tool-output sink**

Per Task 0.10, you've identified the tool-output normalization site. Apply:

```python
from opencomputer.agent.redactors import redact_secrets_in_text

# In the function that formats tool output for LLM context:
if cfg.security.redact_secrets:
    output_text = redact_secrets_in_text(output_text)
```

If logs are written separately (most likely yes), apply the same call before writing to the log line. One canonical chokepoint = one call site is the goal.

- [ ] **Step 7.6: Run the tests to verify they pass**

```bash
pytest tests/test_redact_secrets.py -v
```

Expected: 9 PASSED.

- [ ] **Step 7.7: Commit**

```bash
git add tests/test_redact_secrets.py opencomputer/agent/redactors.py opencomputer/agent/config.py opencomputer/agent/config_store.py opencomputer/agent/
git commit -m "feat(security): security.redact_secrets — strip API key patterns

- Off by default (avoids false positives on legitimate file reads).
- Patterns: sk-, ghp_, github_pat_, gho_, xoxb-, AKIA, Bearer.
- Length thresholds prevent false positives on short strings.
- Applied to tool output before context entry AND log write."
```

---

## Task 8: `agent.disabled_toolsets`

**Files:**
- Create: `tests/test_disabled_toolsets.py`
- Modify: `opencomputer/agent/config.py`, tool registry build site (per Task 0.6)

- [ ] **Step 8.1: Write failing tests**

Create `tests/test_disabled_toolsets.py`:

```python
"""Tests for agent.disabled_toolsets — globally disable named toolsets."""
from __future__ import annotations

import pytest


def test_default_disabled_toolsets_empty() -> None:
    from opencomputer.agent.config import default_config
    cfg = default_config()
    assert cfg.loop.disabled_toolsets == ()


def test_filter_removes_disabled_toolset() -> None:
    """Tools tagged with a disabled toolset are removed from the registry."""
    from opencomputer.agent.config import LoopConfig
    from opencomputer.agent.tool_registry_filter import filter_disabled_toolsets

    tools = [
        {"name": "memory_search", "toolset": "memory"},
        {"name": "memory_save", "toolset": "memory"},
        {"name": "web_search", "toolset": "web"},
        {"name": "execute_code", "toolset": "code"},
    ]
    cfg = LoopConfig(disabled_toolsets=("memory", "web"))
    filtered = filter_disabled_toolsets(tools, cfg.disabled_toolsets)
    names = [t["name"] for t in filtered]
    assert "execute_code" in names
    assert "memory_search" not in names
    assert "memory_save" not in names
    assert "web_search" not in names


def test_no_disabled_keeps_all() -> None:
    from opencomputer.agent.tool_registry_filter import filter_disabled_toolsets
    tools = [{"name": "a", "toolset": "x"}, {"name": "b", "toolset": "y"}]
    out = filter_disabled_toolsets(tools, ())
    assert len(out) == 2


def test_load_config_parses_disabled_toolsets(tmp_path) -> None:
    from opencomputer.agent.config_store import load_config
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "loop:\n  disabled_toolsets:\n    - memory\n    - web\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.loop.disabled_toolsets == ("memory", "web")
```

- [ ] **Step 8.2: Run the tests to verify they fail**

```bash
pytest tests/test_disabled_toolsets.py -v
```

Expected: All fail.

- [ ] **Step 8.3: Add field to `LoopConfig`**

Edit `opencomputer/agent/config.py`. Find `LoopConfig` and add:

```python
@dataclass(frozen=True, slots=True)
class LoopConfig:
    # ... existing ...
    disabled_toolsets: tuple[str, ...] = ()
```

- [ ] **Step 8.4: Implement filter helper**

Create `opencomputer/agent/tool_registry_filter.py`:

```python
"""Tool-registry filter for agent.disabled_toolsets.

Per Hermes config v2 spec: disabled toolsets are removed AFTER per-platform
tool config is applied — so this filter runs at the very end of registry
build, ensuring listed names are gone everywhere regardless of upstream
config.
"""
from __future__ import annotations

from typing import Any, Iterable


def filter_disabled_toolsets(
    tools: Iterable[Any],
    disabled: tuple[str, ...],
) -> list[Any]:
    """Drop tools whose ``toolset`` attribute/key is in ``disabled``."""
    if not disabled:
        return list(tools)
    disabled_set = set(disabled)

    def _toolset_of(t: Any) -> str | None:
        if isinstance(t, dict):
            return t.get("toolset")
        return getattr(t, "toolset", None)

    return [t for t in tools if _toolset_of(t) not in disabled_set]
```

- [ ] **Step 8.5: Wire into the registry build site**

Per Task 0.6, find the registry build call. At the end (after per-platform filters), apply:

```python
from opencomputer.agent.tool_registry_filter import filter_disabled_toolsets

tools = filter_disabled_toolsets(tools, cfg.loop.disabled_toolsets)
```

If individual tools don't currently expose a `toolset` attribute, this becomes a no-op until we add tagging. Document the gap in the commit body. The MVP behavior is: filter exists, schema parses, no-op until tools are tagged. That's acceptable per Hermes spec — we're shipping the knob, not retroactively tagging every tool.

- [ ] **Step 8.6: Run the tests to verify they pass**

```bash
pytest tests/test_disabled_toolsets.py -v
```

Expected: 4 PASSED.

- [ ] **Step 8.7: Commit**

```bash
git add tests/test_disabled_toolsets.py opencomputer/agent/config.py opencomputer/agent/tool_registry_filter.py
git commit -m "feat(agent): agent.disabled_toolsets — global toolset filter

Removes named toolsets from the registry AFTER per-platform tool config,
so listed names are gone everywhere regardless of saved per-tier config.

Note: filter is applied at registry build; tools tagged with a 'toolset'
attribute or dict key are filtered. Untagged tools are unaffected — this
ships the knob; per-tool tagging happens as tools opt in."
```

---

## Task 9: `agent.api_max_retries`

**Files:**
- Create: `tests/test_api_max_retries.py`
- Modify: `opencomputer/agent/config.py`, retry-layer site (per Task 0.7)

- [ ] **Step 9.1: Write failing tests**

Create `tests/test_api_max_retries.py`:

```python
"""Tests for agent.api_max_retries — provider retry knob."""
from __future__ import annotations

import pytest


def test_default_api_max_retries_is_2() -> None:
    """Hermes spec says default is 2."""
    from opencomputer.agent.config import default_config
    cfg = default_config()
    assert cfg.loop.api_max_retries == 2


def test_load_config_parses_api_max_retries(tmp_path) -> None:
    from opencomputer.agent.config_store import load_config
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "loop:\n  api_max_retries: 5\n", encoding="utf-8"
    )
    cfg = load_config(cfg_path)
    assert cfg.loop.api_max_retries == 5


def test_api_max_retries_zero_means_no_retry(tmp_path) -> None:
    """0 = fail-fast: skip retries entirely (Hermes documented behavior)."""
    from opencomputer.agent.config_store import load_config
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "loop:\n  api_max_retries: 0\n", encoding="utf-8"
    )
    cfg = load_config(cfg_path)
    assert cfg.loop.api_max_retries == 0
```

- [ ] **Step 9.2: Run the tests to verify they fail**

```bash
pytest tests/test_api_max_retries.py -v
```

Expected: All fail (no field).

- [ ] **Step 9.3: Add field to `LoopConfig`**

Edit `opencomputer/agent/config.py`. Add to `LoopConfig`:

```python
api_max_retries: int = 2  # Hermes default; 0 = fail-fast.
```

- [ ] **Step 9.4: Plumb into the retry site**

Per Task 0.7, you've identified where the existing retry logic lives. Wire `cfg.loop.api_max_retries` into the retry layer. If a hardcoded retry count exists, replace it with the config value. The retry helper signature should be something like `_retry(call, max_retries=cfg.loop.api_max_retries)`.

- [ ] **Step 9.5: Run the tests to verify they pass**

```bash
pytest tests/test_api_max_retries.py -v
```

Expected: 3 PASSED.

- [ ] **Step 9.6: Commit**

```bash
git add tests/test_api_max_retries.py opencomputer/agent/config.py opencomputer/providers/
git commit -m "feat(agent): agent.api_max_retries — provider retry knob

Default 2 (Hermes spec). Setting 0 = fail-fast to fallback chain on first
transient error. Plumbed into existing retry layer."
```

---

## Task 10: `sessions.vacuum_after_prune`

**Files:**
- Create: `tests/test_sessions_vacuum.py`
- Modify: `opencomputer/agent/config.py`, prune site (per Task 0.8)

- [ ] **Step 10.1: Write failing tests**

Create `tests/test_sessions_vacuum.py`:

```python
"""Tests for sessions.vacuum_after_prune — VACUUM after auto-prune."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def test_default_vacuum_after_prune_is_true() -> None:
    """Hermes spec says default is true."""
    from opencomputer.agent.config import default_config
    cfg = default_config()
    assert cfg.session.vacuum_after_prune is True


def test_vacuum_called_when_enabled(tmp_path: Path) -> None:
    """After prune, VACUUM runs when vacuum_after_prune=True."""
    from opencomputer.agent.session_prune import prune_sessions
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sessions (id TEXT, last_active REAL)")
    # Insert old rows.
    for i in range(100):
        conn.execute(
            "INSERT INTO sessions VALUES (?, 0)", (f"old-{i}",)
        )
    conn.commit()
    conn.close()
    size_before = db_path.stat().st_size
    prune_sessions(
        db_path, retention_days=30, vacuum_after_prune=True, now=1_000_000_000
    )
    size_after = db_path.stat().st_size
    # VACUUM should reclaim space.
    assert size_after < size_before


def test_vacuum_skipped_when_disabled(tmp_path: Path) -> None:
    from opencomputer.agent.session_prune import prune_sessions
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE sessions (id TEXT, last_active REAL)")
    for i in range(100):
        conn.execute(
            "INSERT INTO sessions VALUES (?, 0)", (f"old-{i}",)
        )
    conn.commit()
    conn.close()
    size_before_prune = db_path.stat().st_size
    prune_sessions(
        db_path, retention_days=30, vacuum_after_prune=False, now=1_000_000_000
    )
    size_after = db_path.stat().st_size
    # Without VACUUM, size on disk doesn't shrink (free pages remain).
    # The exact behavior depends on SQLite, but it shouldn't be smaller.
    # Allow tiny shrinkage from page-tail; assert no major reclamation.
    assert size_after >= size_before_prune * 0.95  # within 5%
```

- [ ] **Step 10.2: Run the tests to verify they fail**

```bash
pytest tests/test_sessions_vacuum.py -v
```

Expected: All fail.

- [ ] **Step 10.3: Add field + helper**

Edit `opencomputer/agent/config.py`. Find `SessionConfig` and add:

```python
vacuum_after_prune: bool = True
```

Per Task 0.8, the prune sweep lives somewhere. If it's already a function, modify the signature to accept `vacuum_after_prune: bool` and call `conn.execute("VACUUM")` after the DELETE when True. If it doesn't exist as an extractable function, create a shim:

Create `opencomputer/agent/session_prune.py` (lightweight wrapper):

```python
"""Session-table prune + optional VACUUM. Called from the existing prune
sweep with the configured ``vacuum_after_prune`` flag."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def prune_sessions(
    db_path: Path,
    *,
    retention_days: int,
    vacuum_after_prune: bool,
    now: float | None = None,
) -> int:
    """Delete sessions older than ``retention_days``; optionally VACUUM.

    Returns the number of rows deleted.
    """
    cutoff = (now or time.time()) - retention_days * 86400
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM sessions WHERE last_active < ?", (cutoff,)
        )
        deleted = cursor.rowcount
        conn.commit()
        if vacuum_after_prune and deleted > 0:
            conn.execute("VACUUM")
    finally:
        conn.close()
    return deleted
```

If the existing prune lives in `agent/state.py`, just add the `if vacuum_after_prune: VACUUM` branch to that path and skip creating the new file. Decide based on Task 0.8 finding.

- [ ] **Step 10.4: Run the tests to verify they pass**

```bash
pytest tests/test_sessions_vacuum.py -v
```

Expected: 3 PASSED.

- [ ] **Step 10.5: Commit**

```bash
git add tests/test_sessions_vacuum.py opencomputer/agent/config.py opencomputer/agent/session_prune.py
git commit -m "feat(sessions): sessions.vacuum_after_prune (default true)

After auto-prune deletes old sessions, run VACUUM to reclaim disk. Hermes
v2 documents this as default-true; matches user expectation that
'oc sessions prune' frees space."
```

---

## Task 11: Full-suite verification + push

- [ ] **Step 11.1: Run full pytest suite**

```bash
cd ~/Vscode/claude/.claude/worktrees/hermes-config-v2-2026-05-08
pytest tests/ -x --ignore=tests/test_skills_ide_handoff.py 2>&1 | tail -30
```

Expected: all green except for known-pre-existing flake `test_agent_loop_multi_turn_snapshot_stays_identical_across_different_prefetches` (Honcho test pollution). If anything other than that flake fails, **STOP** — don't push. Investigate and fix.

- [ ] **Step 11.2: Run ruff**

```bash
ruff check . --fix
ruff format --check .
```

Expected: zero errors. If `ruff check --fix` made changes, commit them as `chore(ruff): auto-fix`.

- [ ] **Step 11.3: Self-audit (Karpathy verification)**

Per memory rule "Verify DONE — implementer subagents falsely report DONE without commits", run:

```bash
git log --oneline origin/main..HEAD
git status
git diff origin/main..HEAD --stat | tail -20
```

Confirm:
- 10 commits (one per Task 1-10) plus an optional ruff fixup.
- No uncommitted changes.
- Net delta close to plan estimate (~700 LOC + ~70 tests).

- [ ] **Step 11.4: Push branch**

```bash
git push -u origin feat/hermes-config-v2-2026-05-08
```

- [ ] **Step 11.5: Open PR**

```bash
gh pr create \
  --title "feat(config-v2): \${VAR} substitution + secret-routing + privacy/security + 7 polish knobs" \
  --body "$(cat <<'EOF'
## Summary

Hermes config-v2 foundation — closes the ~/.opencomputer config-management gap. 10 features in one PR.

**Foundation:**
- \`\${VAR}\` substitution in config.yaml loader (single-pass; undefined kept verbatim)
- \`oc config set\` secret-routing → .env (--secret/--public override; mode 0600)
- \`oc config check [--fix]\` (find/add missing top-level config blocks)

**Privacy/Security:**
- \`privacy.redact_pii: bool\` — gateway PII hashing (WhatsApp/Signal/Telegram)
- \`security.redact_secrets: bool\` — strip API key patterns from tool output

**Auxiliary:**
- \`auxiliary.compression.{provider, model, base_url, api_key, timeout}\` nested slot
  (backward-compat: flat \`summary_model:\` continues to work)

**Polish (one-liners):**
- Top-level \`timezone:\` IANA (system prompt + cron tz-aware)
- \`agent.disabled_toolsets\` (toolset-level tool filter)
- \`agent.api_max_retries\` (default 2; 0 = fail-fast)
- \`sessions.vacuum_after_prune\` (default true)

## Spec & plan

- Spec: \`docs/superpowers/specs/2026-05-08-hermes-config-v2-foundation-design.md\`
- Plan: \`docs/superpowers/plans/2026-05-08-hermes-config-v2-foundation.md\`

## Test plan

- [x] All 10 new test files green (~70 tests).
- [x] Full pytest suite green (Honcho flake pre-existing per memory).
- [x] \`ruff check\` clean.
- [x] Backward-compat: flat \`summary_model\`, existing config.yaml without privacy/security blocks load unchanged.
- [x] \${VAR} substitution smoke test: write \`auxiliary.compression.api_key: \${OPENROUTER_KEY}\`, confirm provider client receives the env value.

## Out of scope (parked, see spec §6 reopen triggers)

\`oc config migrate\` interactive wizard, aux slots beyond compression, \`approvals.mode: smart\`, \`command_allowlist\`, \`quick_commands\`, \`code_execution.mode\`, \`browser.dialog_policy\`, \`credential_pool_strategies\` per-provider, modal/daytona/vercel/singularity backends, \`human_delay\`, group session isolation, dashboard kanban toggle.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 11.6: Watch CI**

```bash
gh pr view --json number,statusCheckRollup --jq '.statusCheckRollup'
```

Wait for green. If something red, investigate and fix in this branch (no admin-merge — per memory rule "No Push Without Deep Testing").

- [ ] **Step 11.7: Update tasks**

Mark Tasks 18-22 complete in TaskList. Save a project memory entry for the PR number once merged.

---

## Self-review

- **Spec coverage:** §3.1-3.8 of spec → Tasks 1-10 of plan. Each spec design block has a matching task. Verified.
- **Placeholder scan:** No "TBD"/"TODO"/"appropriately"/"similar to". Each step has a code block when it changes code. Each test step lists the expected count of tests passing.
- **Type consistency:** `_expand_env_vars`, `is_secret_key`, `write_env_var`, `AuxSlotConfig`, `effective_compression_model`, `resolve_compression_provider`, `resolve_compression_endpoint`, `hash_user_id`, `hash_chat_id`, `maybe_redact_user_id`, `maybe_redact_chat_id`, `redact_secrets_in_text`, `filter_disabled_toolsets`, `prune_sessions`, `now_in_tz`, `resolve_tzinfo` — names match across tasks and across the spec.
- **Backward-compat:** every dataclass change uses `default_factory` or `Optional`/None sentinel. Existing config.yaml files load unchanged.
- **Verification gates:** Task 0 (API verification) catches drift BEFORE writing code, in line with the Karpathy memory rule.
- **No order traps:** Tasks 1-10 are independent (no Task N depends on a function defined in Task N+1). Order is for narrative flow only; could parallelize 1, 2, 3, 4, 5, 6, 7 (the "polish" tasks 8/9/10 are tiny and quick at the end).
- **YAGNI:** every parked feature is listed in spec §6 with reopen trigger.
- **Karpathy "ship with callsite":** every new helper has at least one test that exercises it AND a wire-in step (Task X.4 / X.5). No orphaned modules.
- **Worktree hygiene:** worktree exists; branch tracks origin/main. Per parallel-sessions memory rule.
- **Commit hygiene:** 10 commits + optional ruff fixup. No squash; squash happens at PR merge.
