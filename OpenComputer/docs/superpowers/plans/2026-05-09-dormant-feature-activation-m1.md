# Dormant-Feature Activation M1 — Real Bugs + Aliases + Cron Noise Prune

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 6 concrete framework bugs identified in the dormant-feature audit so `oc doctor` runs traceback-free, missing CLI aliases work, and the cron table can be cleaned of noise.

**Architecture:** Surgical edits across 5 files (no new modules); unit tests under `tests/`. Worktree: `.worktrees/m1-dormant-bugs/`. Branch: `fix/dormant-bugs-m1-2026-05-09`. Target PR: `fix(activation): 6 dormant-feature bugs (M1)`.

**Tech Stack:** Python 3.13, typer, pytest, ruff. Follows existing OpenComputer patterns (synthetic-module loader, MemoryProvider contract, `service_app` typer group).

**Files modified (5):**
- `extensions/memory-mem0/plugin.py` (B1)
- `extensions/voice-mode/plugin.py` (B2)
- `opencomputer/doctor.py` (B3, B5)
- `opencomputer/cli.py` (B4 — 5 aliases)
- `opencomputer/cli_cron.py` (B6)

**Files added (5 tests):**
- `tests/test_memory_mem0_collision.py`
- `tests/test_voice_slash_registration.py`
- `tests/test_doctor_telegram_advice.py`
- `tests/test_cli_aliases_dormant.py`
- `tests/test_cron_prune_noise.py`

---

## Task 1: B1 — memory-mem0 graceful collision skip

**Files:**
- Modify: `extensions/memory-mem0/plugin.py:104-112` (end of `register()`)
- Test: `tests/test_memory_mem0_collision.py` (new)

**Why:** `register_fn(provider)` raises `ValueError: a memory provider is already registered: 'memory-honcho:self-hosted'` when honcho registered first. Stack trace spews on every `oc doctor` and gateway boot. Should log warning + skip; honcho-as-default is the design.

- [ ] **Step 1: Write the failing test**

Create `tests/test_memory_mem0_collision.py`:

```python
"""B1: memory-mem0 graceful skip when another provider already registered."""
from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

EXT_DIR = Path(__file__).resolve().parent.parent / "extensions" / "memory-mem0"


def _load_plugin_module():
    """Load extensions/memory-mem0/plugin.py by file path (matches loader)."""
    name = "_test_memory_mem0_plugin"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, EXT_DIR / "plugin.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_register_logs_warning_and_skips_when_provider_already_registered(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When register_memory_provider raises ValueError (collision),
    plugin must log a WARNING and return — NOT propagate the exception.
    """
    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "default")
    plugin = _load_plugin_module()

    api = MagicMock()
    api.register_memory_provider.side_effect = ValueError(
        "a memory provider is already registered: 'memory-honcho:self-hosted'"
    )

    with caplog.at_level(logging.WARNING, logger="memory-mem0"):
        plugin.register(api)  # MUST NOT raise

    msgs = [r.getMessage() for r in caplog.records if r.name == "memory-mem0"]
    assert any("already registered" in m.lower() for m in msgs), msgs
    api.register_memory_provider.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/saksham/Vscode/claude/.worktrees/m1-dormant-bugs/OpenComputer
.venv/bin/pytest tests/test_memory_mem0_collision.py -v
```

Expected: FAIL — `ValueError: a memory provider is already registered` propagates from `plugin.register(api)`.

- [ ] **Step 3: Implement the fix**

Edit `extensions/memory-mem0/plugin.py`. Replace the last 3 lines of `register()`:

```python
    register_fn(provider)
```

with:

```python
    try:
        register_fn(provider)
    except ValueError as exc:
        # Another provider (typically memory-honcho) already won
        # registration. By design only ONE external provider may be
        # active — honcho-as-default is the documented choice. Skip
        # gracefully so `oc doctor` doesn't spew a traceback on every
        # invocation.
        import logging

        logging.getLogger("memory-mem0").warning(
            "Mem0 plugin enabled but another memory provider is already "
            "active (%s); skipping registration. Disable the conflicting "
            "provider to use Mem0.",
            exc,
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_memory_mem0_collision.py -v
```

Expected: PASS.

- [ ] **Step 5: Run doctor to verify the spew is gone**

```bash
.venv/bin/python -m opencomputer doctor 2>&1 | grep -A 1 "memory-mem0\|already registered" | head -10
```

Expected: a single `WARNING memory-mem0: Mem0 plugin enabled...` line, no Python traceback.

- [ ] **Step 6: Commit**

```bash
git add tests/test_memory_mem0_collision.py extensions/memory-mem0/plugin.py
git commit -m "fix(memory-mem0): graceful skip on duplicate-provider collision (M1.B1)"
```

---

## Task 2: B2 — /voice slash command file-path import

**Files:**
- Modify: `extensions/voice-mode/plugin.py:7-22`
- Test: `tests/test_voice_slash_registration.py` (new)

**Why:** `from slash_commands.voice_cmd import VoiceCommand` resolves against whichever plugin's `slash_commands/` is in `sys.modules` — usually coding-harness's, which has no `voice_cmd.py`. The existing try/except catches the error but `/voice` never registers. Fix: file-path import like other plugins do for cross-plugin module-name collisions.

- [ ] **Step 1: Write the failing test**

Create `tests/test_voice_slash_registration.py`:

```python
"""B2: /voice slash command must register from voice-mode/slash_commands/."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

VOICE_DIR = Path(__file__).resolve().parent.parent / "extensions" / "voice-mode"


def _load_voice_plugin():
    """Load extensions/voice-mode/plugin.py by file path (matches loader)."""
    name = "_test_voice_plugin"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, VOICE_DIR / "plugin.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_voice_slash_registration_finds_voice_cmd_when_other_slash_commands_module_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even when another plugin's slash_commands package is cached in
    sys.modules (sibling-name collision), voice-mode must still find
    its own VoiceCommand."""
    # Simulate a different plugin having cached its slash_commands/ — without
    # voice_cmd in it. This is what coding-harness does in real life.
    fake_other = type(sys)("slash_commands")
    fake_other.__path__ = [str(tmp_path)]  # empty dir, no voice_cmd.py
    monkeypatch.setitem(sys.modules, "slash_commands", fake_other)

    plugin = _load_voice_plugin()
    api = MagicMock()
    api.slash_commands = {}

    plugin.register(api)

    api.register_slash_command.assert_called_once()
    cmd = api.register_slash_command.call_args[0][0]
    assert getattr(cmd, "name", None) == "voice"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_voice_slash_registration.py -v
```

Expected: FAIL — `register_slash_command` not called because the import `from slash_commands.voice_cmd` raises ModuleNotFoundError, swallowed by existing except-clause.

- [ ] **Step 3: Implement the fix**

Edit `extensions/voice-mode/plugin.py`. Replace the entire `register()` body (lines 7-22) with:

```python
"""Voice-mode plugin — continuous push-to-talk audio loop."""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

_log = logging.getLogger("opencomputer.voice_mode.plugin")


def register(api) -> None:  # noqa: ANN001
    """Plugin entry; CLI command does the work + register /voice slash."""
    _log.debug("voice-mode plugin registered (use `opencomputer voice talk` to start)")

    if not hasattr(api, "register_slash_command"):
        return

    try:
        # File-path import bypasses sys.modules collisions when another
        # plugin's slash_commands package is cached. coding-harness has
        # the same pattern need; voice-mode used a plain `from
        # slash_commands.voice_cmd import VoiceCommand` which silently
        # resolved against a sibling plugin's slash_commands/ (no
        # voice_cmd.py there) and never registered.
        voice_cmd_path = Path(__file__).resolve().parent / "slash_commands" / "voice_cmd.py"
        synthetic = "_voice_mode_voice_cmd"
        if synthetic in sys.modules:
            del sys.modules[synthetic]
        spec = importlib.util.spec_from_file_location(synthetic, voice_cmd_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"no spec for {voice_cmd_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[synthetic] = mod
        spec.loader.exec_module(mod)
        api.register_slash_command(mod.VoiceCommand())
    except Exception as exc:  # noqa: BLE001 — never break voice-mode load
        _log.warning("/voice slash command registration failed: %s", exc)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_voice_slash_registration.py -v
```

Expected: PASS.

- [ ] **Step 5: Verify doctor no longer prints the import-error warning**

```bash
.venv/bin/python -m opencomputer doctor 2>&1 | grep "voice slash" || echo "OK — no voice import warning"
```

Expected: `OK — no voice import warning` (or no match).

- [ ] **Step 6: Commit**

```bash
git add tests/test_voice_slash_registration.py extensions/voice-mode/plugin.py
git commit -m "fix(voice-mode): file-path import for /voice slash, defeats sibling-cache collision (M1.B2)"
```

---

## Task 3: B3 — doctor surfaces actionable kill commands for telegram dual-daemons

**Files:**
- Modify: `opencomputer/doctor.py` (telegram polling slot section)
- Test: `tests/test_doctor_telegram_advice.py` (new)

**Why:** Existing doctor warning says "2 other gateway process(es) running" with PIDs but no actionable advice. Saksham can't easily resolve — needs `kill <PID>` commands quoted in-line.

- [ ] **Step 1: Locate the existing telegram-polling-slot warning**

```bash
grep -n "telegram polling slot\|other gateway process" opencomputer/doctor.py
```

Expected: a line range to modify.

- [ ] **Step 2: Write the failing test**

Create `tests/test_doctor_telegram_advice.py`:

```python
"""B3: doctor must include actionable `kill PID` advice when multiple
opencomputer gateway processes hold the same telegram bot token slot."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from opencomputer.doctor import _check_telegram_polling_slot


def test_doctor_includes_kill_command_for_each_rogue_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given two rogue PIDs, the warning must contain `kill <PID>` for both."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
    fake_procs = [
        {"pid": 73440, "exe": "/opt/homebrew/Cellar/python@3.11/.../opencomputer"},
        {"pid": 99999, "exe": "/Users/saksham/.local/bin/opencomputer"},
    ]
    with patch(
        "opencomputer.doctor._enumerate_gateway_processes",
        return_value=fake_procs,
    ):
        result = _check_telegram_polling_slot()

    assert result.kind == "warning"
    assert "kill 73440" in result.message
    assert "kill 99999" in result.message
```

NOTE: The test references `_enumerate_gateway_processes` and `_check_telegram_polling_slot`. If those names don't exist, look at the existing doctor.py and adapt this test to match the actual private API.

- [ ] **Step 3: Run test (will likely fail with import or assertion error)**

```bash
.venv/bin/pytest tests/test_doctor_telegram_advice.py -v
```

Expected: import error OR assertion failure. If `_check_telegram_polling_slot` / `_enumerate_gateway_processes` don't exist with those names, find the equivalent and rename in the test.

- [ ] **Step 4: Refactor + extend doctor.py**

Open `opencomputer/doctor.py`. Find the existing telegram-polling-slot check (likely a function that calls `psutil` to enumerate processes). Adjust it to:

1. Extract the process enumeration into a separate function `_enumerate_gateway_processes()` that returns a list of dicts (`pid`, `exe`).
2. Have `_check_telegram_polling_slot()` consume that and produce a result with `message` containing each `kill <PID>` line.

The expected new format of the `message` field (when 2 rogue PIDs found):

```
2 other gateway process(es) running — if any uses the same bot token, only one will receive replies.
  kill 73440  # /opt/homebrew/Cellar/python@3.11/.../opencomputer
  kill 99999  # /Users/saksham/.local/bin/opencomputer
```

- [ ] **Step 5: Run test to verify it passes**

```bash
.venv/bin/pytest tests/test_doctor_telegram_advice.py -v
```

Expected: PASS.

- [ ] **Step 6: Manually verify in the live tool**

```bash
.venv/bin/python -m opencomputer doctor 2>&1 | grep -A 4 "telegram polling slot"
```

Expected: warning lines now include `kill <PID>` per rogue process.

- [ ] **Step 7: Commit**

```bash
git add tests/test_doctor_telegram_advice.py opencomputer/doctor.py
git commit -m "fix(doctor): print actionable kill commands for telegram dual-daemons (M1.B3)"
```

---

## Task 4: B4 — Add 5 missing CLI aliases

**Files:**
- Modify: `opencomputer/cli.py` (5 alias registrations)
- Test: `tests/test_cli_aliases_dormant.py` (new)

**Why:** Saksham hit `oc webhooks`, `oc eval list`, `oc checkpoints list`, `oc routing`, `oc adapter list` — all UX-natural names that 404. The actual commands are `webhook`, `eval history`, `checkpoints status`, `bindings`, and (for adapter) the listing function exists but isn't bound to `list`. Add aliases: typer-group aliases via `app.add_typer(..., name="alias")`; sub-command aliases via a thin alias wrapper.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_aliases_dormant.py`:

```python
"""B4: CLI aliases for natural-name commands users hit first."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from opencomputer.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_oc_webhooks_plural_alias_exists(runner: CliRunner) -> None:
    """`oc webhooks --help` must work the same as `oc webhook --help`."""
    plural = runner.invoke(app, ["webhooks", "--help"])
    singular = runner.invoke(app, ["webhook", "--help"])
    assert plural.exit_code == 0, plural.stdout
    assert singular.exit_code == 0, singular.stdout
    # Both list the same subcommands
    for sub in ("list", "create", "revoke"):
        assert sub in plural.stdout


def test_oc_routing_alias_of_bindings(runner: CliRunner) -> None:
    routing = runner.invoke(app, ["routing", "--help"])
    bindings = runner.invoke(app, ["bindings", "--help"])
    assert routing.exit_code == 0, routing.stdout
    assert bindings.exit_code == 0, bindings.stdout


def test_oc_eval_list_alias_for_history(runner: CliRunner) -> None:
    """`oc eval list` should at least print without 'No such command' error."""
    result = runner.invoke(app, ["eval", "list", "--help"])
    assert result.exit_code == 0
    assert "No such command" not in result.stdout


def test_oc_checkpoints_list_alias_for_status(runner: CliRunner) -> None:
    result = runner.invoke(app, ["checkpoints", "list", "--help"])
    assert result.exit_code == 0
    assert "No such command" not in result.stdout


def test_oc_adapter_list_subcommand_exists(runner: CliRunner) -> None:
    result = runner.invoke(app, ["adapter", "list", "--help"])
    assert result.exit_code == 0
    assert "No such command" not in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_cli_aliases_dormant.py -v
```

Expected: 4-5 failures (alias commands raise typer error / don't exist).

- [ ] **Step 3: Add the typer-group aliases**

Open `opencomputer/cli.py`. Find the `app.add_typer(webhook_app, name="webhook")` line and ADD a sibling line for the alias.

```python
# Original:
app.add_typer(webhook_app, name="webhook")

# Add directly below:
app.add_typer(webhook_app, name="webhooks")  # plural alias — UX parity
```

Same pattern for bindings → routing:

```python
# Original:
app.add_typer(bindings_app, name="bindings")

# Add:
app.add_typer(bindings_app, name="routing")  # alias — terminology parity
```

- [ ] **Step 4: Add `eval list` and `checkpoints list` sub-command aliases**

Locate `eval_app` and `checkpoints_app` in `cli.py` (or their respective `cli_eval.py` / `cli_checkpoints.py`). For each, add a thin command that forwards to the existing function:

```python
# In whichever file defines eval_app, after the @eval_app.command("history") definition:
@eval_app.command("list")
def _eval_list_alias(
    site: str = typer.Argument(None),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """Alias for `oc eval history`."""
    return _eval_history(site=site, limit=limit)  # name of the actual history function
```

Repeat for `checkpoints`:

```python
@checkpoints_app.command("list")
def _checkpoints_list_alias() -> None:
    """Alias for `oc checkpoints status`."""
    return _checkpoints_status()
```

(Adapt the parameter signatures to match the underlying function.)

- [ ] **Step 5: Add `oc adapter list` if missing**

Run `grep -n "@adapter_app.command" opencomputer/cli*.py` to find adapter_app's commands. If `list` is missing, find the existing `ls`/`scaffold`/etc. listing function and add:

```python
@adapter_app.command("list")
def _adapter_list_alias() -> None:
    """Alias for the canonical adapter listing command."""
    return _adapter_ls()  # or whatever the canonical name is
```

If no listing command exists at all, implement a minimal one that prints the registered adapters from `AdapterRegistry`:

```python
@adapter_app.command("list")
def _adapter_list() -> None:
    """List registered channel adapters."""
    from opencomputer.gateway.adapter_registry import AdapterRegistry
    for ad in AdapterRegistry.installed():
        typer.echo(f"{ad.name}\t{ad.kind}\t{ad.version}")
```

- [ ] **Step 6: Run tests to verify all 5 aliases pass**

```bash
.venv/bin/pytest tests/test_cli_aliases_dormant.py -v
```

Expected: 5/5 PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/test_cli_aliases_dormant.py opencomputer/cli.py
# Plus any cli_eval.py / cli_checkpoints.py / cli_adapter.py files touched
git commit -m "feat(cli): add 5 dormant-feature aliases (webhooks/routing/eval list/checkpoints list/adapter list) (M1.B4)"
```

---

## Task 5: B5 — doctor advice for "service enabled but not running"

**Files:**
- Modify: `opencomputer/doctor.py` (service health check)
- Test: extend `tests/test_doctor_telegram_advice.py` OR new `tests/test_doctor_service_advice.py`

**Why:** `oc service start` already does the right thing (bootstrap+kickstart). The actual gap is doctor saying "enabled but not running" without telling the user the fix. Add `→ run \`oc service start\`` to the warning.

- [ ] **Step 1: Locate the service-status doctor block**

```bash
grep -n "enabled but not running\|service.*launchd" opencomputer/doctor.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_doctor_service_advice.py`:

```python
"""B5: doctor advises `oc service start` when service is enabled but not running."""
from __future__ import annotations

from unittest.mock import patch

from opencomputer.doctor import _check_service_status


def test_doctor_advises_service_start_when_enabled_not_running() -> None:
    fake_status = type("S", (), {
        "enabled": True,
        "running": False,
        "file_present": True,
        "backend": "launchd",
        "pid": None,
    })()
    with patch("opencomputer.service.factory.get_backend") as gb:
        gb.return_value.status.return_value = fake_status
        result = _check_service_status()

    assert result.kind == "warning"
    assert "oc service start" in result.message
```

NOTE: If `_check_service_status` doesn't exist, find the equivalent in doctor.py and update the test to match.

- [ ] **Step 3: Run test, expect failure**

```bash
.venv/bin/pytest tests/test_doctor_service_advice.py -v
```

- [ ] **Step 4: Update doctor's service check**

In doctor.py, find the line that produces `"launchd enabled but not running"` and append the actionable hint:

```python
# Before:
return Check(name="service", kind="warning",
             message=f"{backend} enabled but not running")
# After:
return Check(name="service", kind="warning",
             message=f"{backend} enabled but not running — run `oc service start` to launch it")
```

- [ ] **Step 5: Run test to verify pass**

```bash
.venv/bin/pytest tests/test_doctor_service_advice.py -v
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_doctor_service_advice.py opencomputer/doctor.py
git commit -m "fix(doctor): suggest \`oc service start\` when service is enabled but not running (M1.B5)"
```

---

## Task 6: B6 — `oc cron prune --noise` filter

**Files:**
- Modify: `opencomputer/cli_cron.py` (new `prune` command)
- Test: `tests/test_cron_prune_noise.py` (new)

**Why:** 13 of 19 cron jobs are 1-2-letter test names (`a`, `x`, `T`, `b`) and exact duplicates. Manual cleanup via `oc cron remove <id>` × 13 is tedious. New `--noise` flag identifies jobs by heuristic (name length < 4 OR (name, schedule, prompt) duplicate) and offers interactive deletion.

- [ ] **Step 1: Examine existing prune helpers in cli_cron.py**

```bash
grep -n "def cron_\|def _\|cron_app.command" opencomputer/cli_cron.py | head -30
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_cron_prune_noise.py`:

```python
"""B6: oc cron prune --noise removes short-named + duplicate cron jobs."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cron_jobs_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profile_home = tmp_path / "profile"
    (profile_home / "cron").mkdir(parents=True)
    jobs = profile_home / "cron" / "jobs.json"
    jobs.write_text(json.dumps([
        # Noise: short names
        {"id": "1", "name": "a", "schedule": "every 60m", "prompt": ""},
        {"id": "2", "name": "x", "schedule": "every 60m", "prompt": "x"},
        # Noise: exact duplicate of job 4
        {"id": "3", "name": "blogwa", "schedule": "every 60m", "prompt": "blogw"},
        {"id": "4", "name": "blogwa", "schedule": "every 60m", "prompt": "blogw"},
        # Real job
        {"id": "5", "name": "Monday stock briefing", "schedule": "30 8 * * 1", "prompt": "..."},
    ]))
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(profile_home))
    return jobs


def test_prune_dry_run_lists_noise_jobs(
    runner: CliRunner, cron_jobs_file: Path
) -> None:
    """Default behavior is dry-run; lists candidates without deleting."""
    result = runner.invoke(app, ["cron", "prune", "--noise"])
    assert result.exit_code == 0
    out = result.stdout
    # Three noise jobs flagged (short name 'a', 'x', dup 'blogwa')
    assert "1" in out  # short name
    assert "2" in out
    assert "3" in out or "4" in out  # one of the dup
    # Real job NOT flagged
    assert "Monday stock briefing" not in out


def test_prune_apply_removes_noise(
    runner: CliRunner, cron_jobs_file: Path
) -> None:
    """`--apply` actually deletes flagged jobs from jobs.json."""
    result = runner.invoke(app, ["cron", "prune", "--noise", "--apply", "--yes"])
    assert result.exit_code == 0
    remaining = json.loads(cron_jobs_file.read_text())
    names = [j["name"] for j in remaining]
    assert "Monday stock briefing" in names
    assert "a" not in names
    assert "x" not in names
    # Only one blogwa dup remains
    assert names.count("blogwa") <= 1
```

- [ ] **Step 3: Run test, expect failure (no `prune` command)**

```bash
.venv/bin/pytest tests/test_cron_prune_noise.py -v
```

Expected: `No such command 'prune'`.

- [ ] **Step 4: Implement `cron_app.command("prune")`**

Add to `opencomputer/cli_cron.py` (after `cron_remove` ~line 247):

```python
@cron_app.command("prune")
def cron_prune(
    noise: Annotated[bool, typer.Option("--noise", help="Flag short-named or duplicate jobs.")] = False,
    apply_changes: Annotated[bool, typer.Option("--apply", help="Actually delete flagged jobs.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip interactive confirmation.")] = False,
) -> None:
    """Identify and (optionally) remove cron-job noise: short names + exact duplicates.

    Default is DRY RUN — prints flagged jobs without deleting. Use --apply
    to actually delete; pair with --yes to skip the confirmation prompt.

    Heuristic for "noise":
    - Job name length < 4 characters (test/garbage names like "a", "x", "T", "b")
    - Exact duplicate of another job by (name, schedule, prompt) tuple
    """
    if not noise:
        typer.echo("No filter selected. Use --noise to flag short-named + duplicate jobs.")
        raise typer.Exit(0)

    from opencomputer.cron.jobs import load_jobs, save_jobs

    jobs = load_jobs()
    flagged: list[dict] = []
    seen: dict[tuple, dict] = {}
    for j in jobs:
        name = (j.get("name") or "").strip()
        sched = j.get("schedule", "")
        prompt = j.get("prompt", "")
        key = (name, sched, prompt)
        if len(name) < 4:
            flagged.append(j)
            continue
        if key in seen:
            flagged.append(j)
            continue
        seen[key] = j

    if not flagged:
        typer.echo("No noise jobs found.")
        raise typer.Exit(0)

    typer.echo(f"{len(flagged)} noise job(s) flagged:")
    for j in flagged:
        typer.echo(f"  {j.get('id', '?'):<10} {j.get('name', ''):<20} {j.get('schedule', '')}")

    if not apply_changes:
        typer.echo("\n(dry run — pass --apply to delete)")
        raise typer.Exit(0)

    if not yes:
        confirm = typer.confirm("Delete these jobs?", default=False)
        if not confirm:
            raise typer.Exit(1)

    flagged_ids = {j["id"] for j in flagged}
    keep = [j for j in jobs if j["id"] not in flagged_ids]
    save_jobs(keep)
    typer.echo(f"deleted {len(flagged)} noise job(s); {len(keep)} remain.")
```

If `opencomputer.cron.jobs.load_jobs / save_jobs` don't exist, look at the existing `cron_list` (line 60) and `cron_remove` (line 248) to find the canonical helpers — likely something like `JobStore.load()` / `JobStore.save()` — and adapt.

- [ ] **Step 5: Run tests to verify pass**

```bash
.venv/bin/pytest tests/test_cron_prune_noise.py -v
```

Expected: 2/2 PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_cron_prune_noise.py opencomputer/cli_cron.py
git commit -m "feat(cron): \`oc cron prune --noise\` removes short-name + duplicate jobs (M1.B6)"
```

---

## Task 7: Full suite + ruff + push + open PR

- [ ] **Step 1: Run the full pytest suite from worktree root**

```bash
cd /Users/saksham/Vscode/claude/.worktrees/m1-dormant-bugs/OpenComputer
.venv/bin/pytest tests/ -x --timeout=60
```

Expected: ALL pass. If any pre-existing flake (e.g. honcho-default test pollution per memory) shows up, run twice to confirm pre-existing — do not fix unrelated failures here.

- [ ] **Step 2: Run ruff**

```bash
.venv/bin/ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: clean. Fix any new findings.

- [ ] **Step 3: Push**

```bash
git push -u origin fix/dormant-bugs-m1-2026-05-09
```

- [ ] **Step 4: Open PR**

```bash
gh pr create --title "fix(activation): 6 dormant-feature bugs (M1)" --body "$(cat <<'EOF'
## Summary
Closes 6 of the 32 items on Saksham's dormant-feature audit (M1):
- B1: memory-mem0 graceful skip on duplicate-provider collision (no more startup traceback)
- B2: /voice slash command file-path import (defeats sibling-cache collision with coding-harness)
- B3: doctor surfaces actionable \`kill PID\` commands for telegram dual-daemons
- B4: 5 missing CLI aliases — \`oc webhooks\` / \`oc routing\` / \`oc eval list\` / \`oc checkpoints list\` / \`oc adapter list\`
- B5: doctor suggests \`oc service start\` when service is enabled but not running
- B6: \`oc cron prune --noise\` flags short-named + duplicate cron jobs (Saksham has 13 of 19 to clean up)

## Spec
\`docs/superpowers/specs/2026-05-09-dormant-feature-activation-design.md\`

## Plan
\`docs/superpowers/plans/2026-05-09-dormant-feature-activation-m1.md\`

## Test plan
- [x] \`pytest tests/\` full suite green
- [x] \`ruff check\` green
- [x] Manual: \`oc doctor\` shows zero traceback
- [x] Manual: \`oc webhooks --help\` works
- [x] Manual: \`oc cron prune --noise\` flags the noise jobs

## Out of scope (M2-M4)
- M2: \`oc activate\` wizard
- M3: sensible defaults bundle
- M4: service helpers (\`oc langfuse up\` / \`oc wire start --bg\`)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed. Do NOT mark task #2 complete until CI is green; user-merge required.

---

## Self-review before execution

- ✅ Spec coverage: B1, B2, B3, B4, B5, B6 each have a numbered task.
- ✅ Placeholder scan: no "TBD" / "TODO" / "fill in details" — every step has concrete code or commands.
- ✅ Type consistency: `register_fn`, `_check_telegram_polling_slot`, `_check_service_status`, `cron_prune` referenced consistently.
- ✅ Worktree isolation: branch `fix/dormant-bugs-m1-2026-05-09` on origin/main.
- ⚠️ Caveat: Tasks 3 + 5 reference internal doctor function names (`_check_telegram_polling_slot` / `_check_service_status`) that may not exist with those exact names — both tasks include a "find the equivalent and adapt" instruction.
