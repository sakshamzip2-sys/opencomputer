# Messaging Gateway Parity (Hermes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship production-grade Hermes-style messaging gateway for OpenComputer — unified `oc gateway *` command verb, DM-pairing-code system, per-platform reset policies, per-platform display config, runtime metadata footer, background-process notifications knob, first-time busy-input tip, sophisticated `status` command, multi-installation service-name hashing, restart-with-drain, delivery routing, cross-session mirror, missing slash commands, contextvars-based session state, and interrupt-semantics finalization.

**Architecture:** All 16 components are additive to the existing OC gateway (5,125 LOC). The Typer subcommand group (`opencomputer/cli_gateway.py`) is the new entry point; existing dispatcher/queue-manager/agent-loop signatures are preserved. Two PRs, each independently shippable on its own worktree off `origin/main`. PR-1 = user-visible UX surface + security gates; PR-2 = display polish + delivery polish + slash commands + foundation fixes.

**Tech Stack:** Python 3.12+, Typer, pydantic v2, asyncio, contextvars, secrets/hashlib (crypto), fcntl/msvcrt (locking), pytest.

**Spec:** `OpenComputer/docs/superpowers/specs/2026-05-08-messaging-gateway-parity-design.md`

**Reference upstream:** `/Users/saksham/Vscode/claude/sources/hermes-agent/gateway/` (21,298 LOC) and `/Users/saksham/Vscode/claude/sources/hermes-agent/hermes_cli/gateway.py` (4,468 LOC) — port semantics, not source.

---

## File Structure

### PR-1 (Gateway UX + Security)

| Action | Path | Responsibility |
|---|---|---|
| Create | `opencomputer/cli_gateway.py` | Typer group `oc gateway *` (run / setup / install / uninstall / start / stop / restart / status / logs / sethome / pairing) + back-compat hooks |
| Create | `opencomputer/cli_gateway_status.py` | `GatewayRuntimeSnapshot` dataclass + `get_gateway_runtime_snapshot()` (systemd/launchd/schtasks probe + manual-PID detection + foreign-home detection) |
| Create | `opencomputer/channels/pairing_codes.py` | `PairingCodeStore` + `PairingCode` + crypto-random codes + lockout/rate-limit + atomic 0600 writes + deep-link URL generator |
| Create | `opencomputer/channels/allowlist.py` | `AllowlistGate` + `AllowlistDecision` + 19 platform env-var conventions |
| Create | `opencomputer/gateway/reset_policy.py` | `ResetPolicy` + `ResetPolicyConfig` + `ResetPolicyChecker` (idle/daily/both/off + per-platform overrides) |
| Create | `opencomputer/service/_naming.py` | `service_label(profile)` — multi-install hash suffix |
| Modify | `opencomputer/cli.py` | Replace `@app.command def gateway(...)` with `app.add_typer(gateway_app, name="gateway")` + back-compat shim |
| Modify | `opencomputer/agent/config.py` | `GatewayConfig` adds reset-policy fields |
| Modify | `opencomputer/gateway/dispatch.py` | Pre-session-lookup hooks: AllowlistGate.check + ResetPolicyChecker.should_reset + last_seen tracking |
| Modify | `opencomputer/gateway/server.py` | Wire `AllowlistGate`, `PairingCodeStore`, `ResetPolicyChecker`; expose drain.flag handling |
| Modify | `opencomputer/service/_linux_systemd.py` | Use `service_label()` for unit name |
| Modify | `opencomputer/service/_macos_launchd.py` | Use `service_label()` for plist label |
| Modify | `opencomputer/service/_windows_schtasks.py` | Use `service_label()` for task name |
| Create | `tests/cli/test_cli_gateway_group.py` | 12 tests: Typer group structure + back-compat |
| Create | `tests/cli/test_cli_gateway_subcommands.py` | 11 tests: per-subcommand smoke |
| Create | `tests/channels/test_pairing_codes.py` | 18 tests: mint/approve/revoke/regen/list/sweep/lockout/rate-limit/corruption-recovery/deep-link |
| Create | `tests/channels/test_allowlist.py` | 14 tests: gate composition (env + file + pairing) |
| Create | `tests/gateway/test_reset_policy.py` | 14 tests: idle/daily/both/off + per-platform overrides + dispatcher integration + archive |
| Create | `tests/cli/test_gateway_status.py` | 12 tests: process-service mismatch + manual-PID + foreign-home + multi-installation |
| Create | `tests/service/test_service_naming.py` | 6 tests: canonical preserved + multi-install hashing + collision detection |
| Modify | `CHANGELOG.md` | New entry under `[Unreleased]` |

### PR-2 (Display polish + Delivery polish + Foundation fixes)

| Action | Path | Responsibility |
|---|---|---|
| Create | `opencomputer/gateway/display_config.py` | `resolve_display_setting()` + tier-based per-platform defaults |
| Create | `opencomputer/gateway/delivery.py` | `DeliveryTarget.parse()` + `DeliveryRouter` |
| Create | `opencomputer/gateway/mirror.py` | `mirror_to_session()` — append delivery-mirror entries to target session |
| Create | `opencomputer/gateway/session_context.py` | `contextvars.ContextVar` per-task session state |
| Modify | `opencomputer/gateway/runtime_footer.py` | Per-platform `resolve_footer_config` + `format_runtime_footer` + `_OnboardingLatch` for first-time tip |
| Modify | `opencomputer/agent/bg_notify.py` | `_should_emit` filter using `display_config.resolve_display_setting` |
| Modify | `opencomputer/cli_ui/slash.py` | Add `/sethome`, `/voice`, `/approve`, `/deny`, `/status` (session-info), `/footer` to SLASH_REGISTRY |
| Create | `opencomputer/agent/slash_commands_impl/sethome_cmd.py` | `/sethome <platform> <chat_id>` handler |
| Create | `opencomputer/agent/slash_commands_impl/status_cmd.py` | `/status` session-info handler |
| Create | `opencomputer/agent/slash_commands_impl/footer_cmd.py` | `/footer on/off/status` handler |
| Create | `extensions/voice-mode/slash_commands/voice_cmd.py` | `/voice on/off/tts/join/leave/status` handler |
| Create | `extensions/coding-harness/slash_commands/approve_cmd.py` | `/approve` handler |
| Create | `extensions/coding-harness/slash_commands/deny_cmd.py` | `/deny` handler |
| Modify | `opencomputer/cli_gateway.py` | Add `--drain-timeout` to `restart` (Phase 2 — needs PR-1 base) |
| Modify | `opencomputer/gateway/dispatch.py` | Drain-flag check loop |
| Modify | `opencomputer/agent/loop.py` | Interrupt-semantics audit (SIGTERM→1s→SIGKILL via existing cancel; tool-batch-cancel verification) |
| Modify | `extensions/discord/adapter.py` (and others as needed) | Consume `resolve_display_setting()` instead of direct `cfg.display.tool_progress` |
| Modify | `opencomputer/agent/config.py` | `DisplayConfig` adds `runtime_footer`, `background_process_notifications`, `platforms` keys |
| Create | `tests/gateway/test_display_config.py` | 16 tests: resolution order + tier defaults + platform overrides + migration |
| Create | `tests/gateway/test_runtime_footer.py` | 9 tests: per-platform resolve + format + streaming-trailing |
| Create | `tests/gateway/test_bg_notify_filter.py` | 8 tests: modes × platforms |
| Create | `tests/gateway/test_first_time_tip.py` | 5 tests: latch + flock + once-only |
| Create | `tests/gateway/test_drain_restart.py` | 6 tests: signal + wait + timeout-fallback |
| Create | `tests/gateway/test_delivery.py` | 11 tests: parse all formats + truncation + cron auto-deliver |
| Create | `tests/gateway/test_mirror.py` | 7 tests: session-find + JSONL append + SQLite append + best-effort |
| Create | `tests/agent/test_slash_messaging_extras.py` | 12 tests: /sethome /voice /approve /deny /status /footer |
| Create | `tests/gateway/test_session_context.py` | 7 tests: contextvars task-isolation |
| Create | `tests/agent/test_interrupt_semantics.py` | 8 tests: SIGTERM→SIGKILL, tool-cancel cascade, message coalesce |
| Modify | `CHANGELOG.md` | Add entry |

---

## Phase 0 — Setup

### Task 0.1: Create PR-1 worktree

**Files:** none (worktree creation only)

- [ ] **Step 1: Confirm parent repo state**

Run: `cd /Users/saksham/Vscode/claude && git status --short`
Expected: only the documented in-flight `M opencomputer/gateway/outgoing_drainer.py`, `M opencomputer/gateway/outgoing_queue.py`, plus the just-written spec/plan, plus `??` artifacts. Do NOT touch the modified files.

- [ ] **Step 2: Fetch latest main**

Run: `cd /Users/saksham/Vscode/claude && git fetch origin main`
Expected: clean fetch.

- [ ] **Step 3: Create PR-1 worktree off origin/main**

Run: `cd /Users/saksham/Vscode/claude && git worktree add .claude/worktrees/gateway-parity-pr1 -b feat/gateway-parity-pr1-2026-05-08 origin/main`
Expected: worktree created at `.claude/worktrees/gateway-parity-pr1`.

- [ ] **Step 4: Install editable inside the worktree**

Run: `cd .claude/worktrees/gateway-parity-pr1/OpenComputer && python -m venv .venv && source .venv/bin/activate && pip install -e .[dev] 2>&1 | tail -3`
Expected: install completes; `oc --help` works.

- [ ] **Step 5: Baseline pytest + ruff**

Run: `cd .claude/worktrees/gateway-parity-pr1/OpenComputer && source .venv/bin/activate && python -m pytest -x --tb=short -q 2>&1 | tail -5 && ruff check opencomputer/ plugin_sdk/ extensions/ tests/ 2>&1 | tail -3`
Expected: tests green, ruff clean.

---

## Phase 1 (PR-1) — Gateway UX + Security

### Task 1.1: Service-naming hash util

**Files:**
- Create: `opencomputer/service/_naming.py`
- Test: `tests/service/test_service_naming.py`

- [ ] **Step 1: Write failing tests**

Create `tests/service/test_service_naming.py`:

```python
"""Tests for service-name hashing (PR-1)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from opencomputer.service import _naming


def test_canonical_home_default_profile_returns_canonical_label(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    assert _naming.service_label("default") == "opencomputer-gateway"


def test_canonical_home_named_profile_returns_hashed(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    label = _naming.service_label("work")
    assert label.startswith("opencomputer-gateway-")
    assert len(label) == len("opencomputer-gateway-") + 8


def test_non_canonical_home_returns_hashed(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    label = _naming.service_label("default")
    assert label.startswith("opencomputer-gateway-")


def test_distinct_homes_distinct_labels(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "a"))
    a = _naming.service_label("default")
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "b"))
    b = _naming.service_label("default")
    assert a != b


def test_same_home_same_profile_deterministic(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    a = _naming.service_label("default")
    b = _naming.service_label("default")
    assert a == b


def test_collision_helper_returns_distinct_labels_for_distinct_inputs():
    # Cannot find a true sha256[:8] collision in test, but verify the
    # generator function is deterministic and well-defined.
    label = _naming._hash_label_suffix("/home/u/.opencomputer", "default")
    assert len(label) == 8
    assert all(c in "0123456789abcdef" for c in label)
```

- [ ] **Step 2: Run tests to verify all fail**

Run: `cd OpenComputer && source .venv/bin/activate && pytest tests/service/test_service_naming.py -v 2>&1 | tail -10`
Expected: 6 errors / `ImportError: cannot import name '_naming'`.

- [ ] **Step 3: Implement `_naming.py`**

Create `opencomputer/service/_naming.py`:

```python
"""Service-label generation for systemd / launchd / schtasks backends.

Single-install (canonical OPENCOMPUTER_HOME + 'default' profile) keeps
the historical 'opencomputer-gateway' label so existing service files
don't need re-installing. Multi-install (non-canonical HOME OR named
profile) appends a sha256[:8] hash so two daemons can coexist.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

_CANONICAL_LABEL = "opencomputer-gateway"
_DEFAULT_HOME = str(Path.home() / ".opencomputer")


def _resolved_home() -> str:
    return os.environ.get("OPENCOMPUTER_HOME") or _DEFAULT_HOME


def _hash_label_suffix(home: str, profile: str) -> str:
    digest = hashlib.sha256(f"{home}|{profile}".encode("utf-8")).hexdigest()
    return digest[:8]


def service_label(profile: str = "default") -> str:
    home = _resolved_home()
    if home == _DEFAULT_HOME and profile == "default":
        return _CANONICAL_LABEL
    suffix = _hash_label_suffix(home, profile)
    return f"{_CANONICAL_LABEL}-{suffix}"
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/service/test_service_naming.py -v 2>&1 | tail -10`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/service/_naming.py tests/service/test_service_naming.py
git commit -m "feat(service): hash-suffixed labels for multi-install support"
```

---

### Task 1.2: Wire naming into systemd / launchd / schtasks backends

**Files:**
- Modify: `opencomputer/service/_linux_systemd.py`
- Modify: `opencomputer/service/_macos_launchd.py`
- Modify: `opencomputer/service/_windows_schtasks.py`

- [ ] **Step 1: Read the three backend files** to find where the unit name is hardcoded.

Run: `cd OpenComputer && grep -nE "opencomputer-gateway|ai\.opencomputer\.gateway|service_label" opencomputer/service/*.py`

- [ ] **Step 2: Replace hardcoded `"opencomputer-gateway"` with `service_label(profile)`** in each backend's install/uninstall/start/stop/status methods. Add `from opencomputer.service._naming import service_label` at top.

- [ ] **Step 3: Verify existing service tests still pass** (no migration required for canonical home).

Run: `pytest tests/service/ -v 2>&1 | tail -10`
Expected: all green; `service_label("default")` returns canonical label so existing tests don't need updates.

- [ ] **Step 4: Commit**

```bash
git add opencomputer/service/
git commit -m "feat(service): plumb hash-suffix label through systemd/launchd/schtasks backends"
```

---

### Task 1.3: Reset-policy module + GatewayConfig fields

**Files:**
- Create: `opencomputer/gateway/reset_policy.py`
- Modify: `opencomputer/agent/config.py`
- Test: `tests/gateway/test_reset_policy.py`

- [ ] **Step 1: Write failing tests** for `ResetPolicyChecker` covering:
  - `mode="off"` never resets
  - `mode="idle"` resets when `(now - last_seen) >= idle_minutes * 60`
  - `mode="daily"` resets when `now` crosses `daily_at_hour` boundary since `last_seen`
  - `mode="both"` resets on either condition
  - per-platform override resolved before default
  - `policy_for(platform)` returns platform override when present, else default
  - tuple return — `(do_reset: bool, reason: str)`

Tests at `tests/gateway/test_reset_policy.py` (~14 tests, follow existing test style in repo).

- [ ] **Step 2: Run tests to verify fail**

Run: `pytest tests/gateway/test_reset_policy.py -v 2>&1 | tail -15`
Expected: import errors.

- [ ] **Step 3: Implement `reset_policy.py`**

Create `opencomputer/gateway/reset_policy.py` with the contracts from spec §5.3:

```python
"""Per-platform session-reset policies.

Used by Dispatch.handle_message before session lookup. Can reset based on
inactivity (idle_minutes since last_seen), wall-clock crossings (daily
at hour H local time), both, or off.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Literal

ResetMode = Literal["off", "daily", "idle", "both"]


@dataclass(frozen=True, slots=True)
class ResetPolicy:
    mode: ResetMode = "both"
    daily_at_hour: int = 4
    idle_minutes: int = 1440


@dataclass(frozen=True, slots=True)
class ResetPolicyConfig:
    default: ResetPolicy = field(default_factory=ResetPolicy)
    by_platform: dict[str, ResetPolicy] = field(default_factory=dict)


class ResetPolicyChecker:
    def __init__(
        self,
        cfg: ResetPolicyConfig,
        *,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = cfg
        self._now_fn = now_fn

    def policy_for(self, platform: str) -> ResetPolicy:
        return self._cfg.by_platform.get(platform, self._cfg.default)

    def should_reset(
        self, platform: str, chat_id: str, last_seen: float
    ) -> tuple[bool, str]:
        policy = self.policy_for(platform)
        if policy.mode == "off":
            return (False, "off")
        now = self._now_fn()
        if policy.mode in ("idle", "both"):
            if (now - last_seen) >= policy.idle_minutes * 60:
                return (True, f"idle:{policy.idle_minutes}m")
        if policy.mode in ("daily", "both"):
            if self._crossed_daily_boundary(last_seen, now, policy.daily_at_hour):
                return (True, f"daily:{policy.daily_at_hour}")
        return (False, policy.mode)

    @staticmethod
    def _crossed_daily_boundary(last_seen: float, now: float, hour: int) -> bool:
        last_dt = datetime.fromtimestamp(last_seen).astimezone()
        now_dt = datetime.fromtimestamp(now).astimezone()
        # Last reset boundary is today's `hour`, or yesterday's if we haven't
        # reached today's boundary yet.
        boundary = now_dt.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now_dt < boundary:
            from datetime import timedelta
            boundary = boundary - timedelta(days=1)
        return last_dt < boundary <= now_dt
```

- [ ] **Step 4: Add reset-policy fields to `GatewayConfig`**

Edit `opencomputer/agent/config.py:GatewayConfig` (after `startup_ping_message`):

```python
    # ─── Per-platform session reset (PR-1, 2026-05-08) ────────────────
    reset_mode: str = "both"
    reset_daily_at_hour: int = 4
    reset_idle_minutes: int = 1440
    reset_by_platform: dict = field(default_factory=dict)
```

- [ ] **Step 5: Run tests pass**

Run: `pytest tests/gateway/test_reset_policy.py -v 2>&1 | tail -20`
Expected: all 14 pass.

- [ ] **Step 6: Cron-sweep registration**

  The pairing-code expired-sweep needs a periodic tick. Verify which API exists:

  Run: `grep -rn "register_periodic\|register_cron\|@cron\|schedule_every" opencomputer/cron/ opencomputer/cli_cron.py 2>&1 | head`

  - If a public registration API exists (e.g., `cron.register_periodic(name, interval_s, fn)`), use it: register `PairingCodeStore.expired_sweep_all` at 60s.
  - If not, register an asyncio task in `Gateway.serve_forever` that calls the sweep every 60s. (Falls back to in-process scheduling — survives across reboots since the daemon restarts boot the loop.)

  Either path is acceptable; the test for it lives in Task 1.4 (expired-sweep tests already cover the function logic).

- [ ] **Step 7: Commit**

```bash
git add opencomputer/gateway/reset_policy.py opencomputer/agent/config.py tests/gateway/test_reset_policy.py
git commit -m "feat(gateway): per-platform session reset policies (idle/daily/both)"
```

---

### Task 1.4: PairingCodeStore (DM Pairing core)

**Files:**
- Create: `opencomputer/channels/pairing_codes.py`
- Test: `tests/channels/test_pairing_codes.py`

- [ ] **Step 1: Write failing tests** for ~20 cases:
  - `generate_code` returns 8-char code from ALPHABET (no 0/O/1/I)
  - rate-limit: 2nd `generate_code` for same `(platform, user_id)` within 600s returns None
  - max pending: 4th request when 3 pending → None
  - lockout: 5 failed `approve_code(...)` → platform locked for 3600s
  - approve flow: `generate_code` → `approve_code(code)` → `is_approved` True
  - approve unknown code → None + records failure
  - revoke: `approve` then `revoke` → `is_approved` False
  - regenerate_code: bypasses rate-limit, honors lockout
  - list_pending: returns rows with age_minutes
  - list_approved: cross-platform listing
  - clear_pending: returns count and empties
  - expired sweep: codes >1h old removed by `_cleanup_expired`
  - corruption recovery: malformed JSON → empty + `.corrupt.<ts>` backup
  - file permissions: 0600 on all data files (POSIX)
  - atomic write: tmpfile + os.replace
  - threading: parallel `generate_code` from N threads doesn't double-mint
  - **cross-process** (`flock`): two simultaneous processes calling `generate_code` for the same platform serialize via flock; pending count never exceeds the cap
  - deep_link returns Telegram URL for telegram, None for unsupported platforms

- [ ] **Step 2: Run tests, verify fail**

- [ ] **Step 3: Implement `pairing_codes.py`** porting Hermes `gateway/pairing.py` semantics 1:1, with these adaptations:
  - `PAIRING_DIR` = `<profile>/pairing/` (not `~/.hermes/pairing/`).
  - Add `regenerate_code()` method (Hermes upstream lacks this).
  - Add `deep_link(platform, code)` returning Telegram deep-link URL.
  - Use `opencomputer.agent.config_store.profile_home()` for path.
  - **Cross-process locking via `flock`** (POSIX) / `msvcrt.locking` (Windows): `_save_json` acquires a per-platform `<platform>.lock` flock for the duration of the read-modify-write cycle. RLock alone is in-process; flock prevents two daemons (or a daemon + a CLI invocation of `oc gateway pairing approve`) from racing.
  - On Windows where flock isn't available, fall back to a 5-retry-with-jitter advisory lock around `os.replace` (best-effort).

```python
# opencomputer/channels/pairing_codes.py
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_TTL_SECONDS = 3600
RATE_LIMIT_SECONDS = 600
LOCKOUT_SECONDS = 3600
MAX_PENDING_PER_PLATFORM = 3
MAX_FAILED_ATTEMPTS = 5

logger = logging.getLogger("opencomputer.channels.pairing_codes")


@dataclass(frozen=True, slots=True)
class PairingCode:
    platform: str
    user_id: str
    code: str
    created_at: float
    expires_at: float


def _atomic_write(path: Path, data: str) -> None:
    """tmpfile + os.replace + chmod 0600 (POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class PairingCodeStore:
    """Owner-restricted file-backed pairing-code store.

    Files per platform under ``<profile>/pairing/``:
      ``<platform>-pending.json``    pending requests
      ``<platform>-approved.json``   approved users
      ``_rate_limits.json``          rate-limit + lockout state

    All writes atomic + chmod 0600. Threading-safe via RLock.
    """

    def __init__(self, profile_home: Path):
        self._dir = profile_home / "pairing"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # … (implement: is_approved, list_approved, revoke, generate_code,
    #     regenerate_code, approve_code, list_pending, clear_pending,
    #     deep_link, _is_rate_limited, _record_rate_limit, _is_locked_out,
    #     _record_failed_attempt, _cleanup_expired, _all_platforms,
    #     _load_json (with corruption recovery), _save_json, paths)

    def deep_link(self, platform: str, code: str) -> Optional[str]:
        """Return one-click admin-approval URL when supported.

        Telegram: https://t.me/<bot_username>?start=approve_<code>
                  (requires TELEGRAM_BOT_USERNAME env or config.)
        Discord:  https://discord.com/users/<bot_id> with a /approve slash
                  hint (Discord doesn't support deep-link query params for
                  bot DMs; we return a copy-pasteable command instead).
        Other:    None (no platform-native deep-link).
        """
        ...
```

(Full implementation follows the Hermes pairing.py shape; the test suite drives any divergence.)

- [ ] **Step 4: Run tests pass**

- [ ] **Step 5: Commit**

```bash
git add opencomputer/channels/pairing_codes.py tests/channels/test_pairing_codes.py
git commit -m "feat(channels): DM pairing-code store with lockout/rate-limit/0600 perms"
```

---

### Task 1.5: AllowlistGate (env-vars + file-overlay + pairing-store composition)

**Files:**
- Create: `opencomputer/channels/allowlist.py`
- Test: `tests/channels/test_allowlist.py`

- [ ] **Step 1: Write failing tests** for ~14 cases:
  - `GATEWAY_ALLOW_ALL_USERS=true` → `allowed=True, source="allow-all"`
  - `<PLATFORM>_ALLOWED_USERS=123,456` + user_id=`123` → `allowed=True, source="env-platform"`
  - `GATEWAY_ALLOWED_USERS=789` (catch-all) + user_id=`789` → `allowed=True, source="env-global"`
  - `<profile>/allowlist.json` overlay → `allowed=True, source="file"`
  - PairingCodeStore approved → `allowed=True, source="pairing-approved"`
  - none of above → `allowed=False, pairing_code=<8 chars>`
  - rate-limit hit → `allowed=False, pairing_code=None`
  - one test per platform-env-var (19 tests would over-bloat; one per family suffices, plus one composing all 19)

- [ ] **Step 2: Run tests, verify fail**

- [ ] **Step 3: Implement `allowlist.py`**

```python
# opencomputer/channels/allowlist.py
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from opencomputer.channels.pairing_codes import PairingCodeStore

logger = logging.getLogger("opencomputer.channels.allowlist")

_PLATFORM_ENV_VARS: dict[str, str] = {
    "telegram":       "TELEGRAM_ALLOWED_USERS",
    "discord":        "DISCORD_ALLOWED_USERS",
    "slack":          "SLACK_ALLOWED_USERS",
    "signal":         "SIGNAL_ALLOWED_USERS",
    "sms":            "SMS_ALLOWED_USERS",
    "email":          "EMAIL_ALLOWED_USERS",
    "mattermost":     "MATTERMOST_ALLOWED_USERS",
    "matrix":         "MATRIX_ALLOWED_USERS",
    "dingtalk":       "DINGTALK_ALLOWED_USERS",
    "feishu":         "FEISHU_ALLOWED_USERS",
    "wecom":          "WECOM_ALLOWED_USERS",
    "wecom_callback": "WECOM_CALLBACK_ALLOWED_USERS",
    "whatsapp":       "WHATSAPP_ALLOWED_USERS",
    "weixin":         "WEIXIN_ALLOWED_USERS",
    "yuanbao":        "YUANBAO_ALLOWED_USERS",
    "qq":             "QQ_ALLOWED_USERS",
    "bluebubbles":    "BLUEBUBBLES_ALLOWED_USERS",
    "homeassistant":  "HOMEASSISTANT_ALLOWED_USERS",
    "irc":            "IRC_ALLOWED_USERS",
    "teams":          "TEAMS_ALLOWED_USERS",
}

_ALLOW_ALL_ENV = "GATEWAY_ALLOW_ALL_USERS"
_GLOBAL_ENV = "GATEWAY_ALLOWED_USERS"


@dataclass(frozen=True, slots=True)
class AllowlistDecision:
    allowed: bool
    source: str
    pairing_code: Optional[str] = None
    user_id: str = ""


def _parse_csv(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _load_file_overlay(profile_home: Path) -> dict[str, list[str]]:
    path = profile_home / "allowlist.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("allowlist.json unreadable: %s — treating as empty", exc)
        return {}


class AllowlistGate:
    def __init__(
        self,
        *,
        profile_home: Path,
        pairing_store: PairingCodeStore | None = None,
    ):
        self._profile_home = profile_home
        self._pairing = pairing_store or PairingCodeStore(profile_home)

    def check(
        self,
        platform: str,
        user_id: str,
        *,
        user_name: str = "",
    ) -> AllowlistDecision:
        if os.getenv(_ALLOW_ALL_ENV, "").lower() in ("1", "true", "yes"):
            return AllowlistDecision(True, "allow-all", user_id=user_id)

        env_var = _PLATFORM_ENV_VARS.get(platform)
        if env_var:
            allowed = _parse_csv(os.getenv(env_var, ""))
            if user_id in allowed:
                return AllowlistDecision(True, "env-platform", user_id=user_id)

        global_allowed = _parse_csv(os.getenv(_GLOBAL_ENV, ""))
        if user_id in global_allowed:
            return AllowlistDecision(True, "env-global", user_id=user_id)

        file_overlay = _load_file_overlay(self._profile_home).get(platform, [])
        if user_id in file_overlay:
            return AllowlistDecision(True, "file", user_id=user_id)

        if self._pairing.is_approved(platform, user_id):
            return AllowlistDecision(True, "pairing-approved", user_id=user_id)

        # Miss — mint a pairing code (None when rate-limited or locked out).
        code = self._pairing.generate_code(platform, user_id, user_name)
        return AllowlistDecision(
            False, "denied", pairing_code=code, user_id=user_id
        )
```

- [ ] **Step 4: Run tests pass**

- [ ] **Step 5: Commit**

```bash
git add opencomputer/channels/allowlist.py tests/channels/test_allowlist.py
git commit -m "feat(channels): AllowlistGate composing env+file+pairing sources"
```

---

### Task 1.6: Wire AllowlistGate + ResetPolicy into Dispatch

**Files:**
- Modify: `opencomputer/gateway/dispatch.py`
- Modify: `opencomputer/gateway/server.py`
- Test: `tests/gateway/test_reset_policy.py` (extend with dispatch integration tests, ~6 more)

- [ ] **Step 1: Read `dispatch.py:Dispatch.handle_message`** to find the deterministic `(platform, chat_id) → session_id` mapping point.

- [ ] **Step 2: Add `_chat_last_seen: dict[tuple[str, str], float]`** as a `Dispatch` field initialized in `__init__`. Persist to/from `<profile>/gateway/last_seen.json` on every Nth update (N=10) and on graceful shutdown.

- [ ] **Step 3: Inject `AllowlistGate` and `ResetPolicyChecker`** as constructor parameters of `Dispatch`. Default-construct them in `Gateway.__init__` from `GatewayConfig` + profile home.

- [ ] **Step 4: At the top of `handle_message`**:

```python
# AllowlistGate: deny + pairing-code reply on miss.
decision = self.allowlist.check(event.platform, event.user_id, user_name=event.user_name)
if not decision.allowed:
    if decision.pairing_code:
        reply = self._format_pairing_reply(event.platform, decision.pairing_code)
        await self._adapter_for(event.platform).send(event.chat_id, reply)
    # silent on rate-limit / lockout
    return None

# ResetPolicy: reset before session-id derivation.
last_seen = self._chat_last_seen.get((event.platform, event.chat_id), 0.0)
do_reset, reason = self.reset_policy.should_reset(
    event.platform, event.chat_id, last_seen
)
if do_reset:
    self._archive_and_reset(event.platform, event.chat_id, reason)
self._chat_last_seen[(event.platform, event.chat_id)] = time.time()
```

- [ ] **Step 5: Implement `_format_pairing_reply` and `_archive_and_reset`** as private helpers on `Dispatch`.

`_archive_and_reset` defensive shape (handles `SessionDB.archive` not existing):

```python
def _archive_and_reset(self, platform: str, chat_id: str, reason: str) -> None:
    session_id = self._session_for(platform, chat_id)
    if session_id is None:
        return
    try:
        # Preferred path — typed archive that records the reset reason.
        archive = getattr(self.session_db, "archive", None)
        if callable(archive):
            archive(session_id, reason=reason)
        else:
            # Fallback — move JSONL transcript file under sessions/archive/.
            self._move_transcript_to_archive(session_id, reason)
    except Exception:
        logger.warning(
            "session archive failed for %s; dropping session row anyway",
            session_id,
            exc_info=True,
        )
    self.session_db.delete(session_id)
    self._session_map.pop((platform, chat_id), None)
```

- [ ] **Step 6: Add 6 dispatch-integration tests**:
  - allowlist deny + pairing-code reply path
  - allowlist allow proceeds to session lookup
  - reset policy fires on idle threshold
  - reset policy fires on daily boundary
  - per-platform override resolved
  - last_seen file persisted across restart

- [ ] **Step 7: Run tests pass + ensure existing dispatch tests still pass**

Run: `pytest tests/gateway/ -v 2>&1 | tail -30`

- [ ] **Step 8: Commit**

```bash
git add opencomputer/gateway/dispatch.py opencomputer/gateway/server.py tests/gateway/test_reset_policy.py
git commit -m "feat(gateway): wire AllowlistGate + ResetPolicy into Dispatch.handle_message"
```

---

### Task 1.7: Status command snapshot (process-vs-service mismatch + foreign-home)

**Files:**
- Create: `opencomputer/cli_gateway_status.py`
- Test: `tests/cli/test_gateway_status.py`

- [ ] **Step 1: Write failing tests** for ~12 cases:
  - manager detection (systemd-user, systemd-system, launchd, schtasks)
  - service-installed (returns true when unit exists)
  - service-running (active vs inactive)
  - manual PIDs (`pgrep -f opencomputer.*gateway` finds it)
  - foreign-home detection (PID belongs to another OPENCOMPUTER_HOME)
  - process-service mismatch (installed AND running but service inactive)
  - empty state (nothing running)
  - `running` property correctness
  - `has_process_service_mismatch` property correctness

- [ ] **Step 2: Implement `cli_gateway_status.py`** porting Hermes status logic:

```python
"""Gateway runtime-status snapshot.

Composes systemd / launchd / schtasks probes + manual-PID detection
into a single GatewayRuntimeSnapshot dataclass that the `oc gateway
status` command renders.
"""
from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from opencomputer.service._naming import service_label


@dataclass(frozen=True)
class ProfileGatewayProcess:
    profile: str
    home: Path
    pid: int


@dataclass(frozen=True)
class GatewayRuntimeSnapshot:
    manager: str  # "systemd-user" | "systemd-system" | "launchd" | "schtasks" | "none"
    service_installed: bool = False
    service_running: bool = False
    main_pid: int | None = None
    gateway_pids: tuple[int, ...] = ()           # manual PIDs (no service)
    foreign_home_pids: tuple[ProfileGatewayProcess, ...] = ()
    service_scope: str | None = None             # "user" | "system" | None

    @property
    def running(self) -> bool:
        return self.service_running or bool(self.gateway_pids)

    @property
    def has_process_service_mismatch(self) -> bool:
        return self.service_installed and self.running and not self.service_running


def get_gateway_runtime_snapshot(profile: str = "default") -> GatewayRuntimeSnapshot:
    label = service_label(profile)
    sysname = platform.system()
    if sysname == "Linux":
        return _systemd_snapshot(label)
    if sysname == "Darwin":
        return _launchd_snapshot(label)
    if sysname == "Windows":
        return _schtasks_snapshot(label)
    return GatewayRuntimeSnapshot(manager="none")
```

(Implementations of `_systemd_snapshot`, `_launchd_snapshot`, `_schtasks_snapshot` follow Hermes `hermes_cli/gateway.py:_get_service_pids` and friends. Mock subprocess in tests.)

- [ ] **Step 3: Tests pass**

- [ ] **Step 4: Commit**

```bash
git add opencomputer/cli_gateway_status.py tests/cli/test_gateway_status.py
git commit -m "feat(cli): GatewayRuntimeSnapshot — service vs manual-PID + mismatch detection"
```

---

### Task 1.8: Typer group `oc gateway *` + back-compat

**Files:**
- Create: `opencomputer/cli_gateway.py`
- Modify: `opencomputer/cli.py` — replace `@app.command def gateway(...)` with `app.add_typer(gateway_app, name="gateway")`
- Test: `tests/cli/test_cli_gateway_group.py`, `tests/cli/test_cli_gateway_subcommands.py`

- [ ] **Step 1: Write failing tests** (~23 cases):
  - bare `oc gateway` exits 0 and runs foreground (mock-runner intercepts)
  - `oc gateway run` same as bare
  - `oc gateway --install-daemon` warns + delegates to `install`
  - `oc gateway install` calls `service.factory.install`
  - `oc gateway install --system` (Linux) → system scope
  - `oc gateway uninstall` → service backend uninstall
  - `oc gateway start/stop/restart/status/logs` smoke
  - `oc gateway setup` invokes `cli_setup.wizard` with sections filtered to messaging-platforms
  - `oc gateway sethome <platform> <chat>` writes home_channels.json
  - `oc gateway pairing list` empty → "no pending"
  - `oc gateway pairing approve <platform> <code>` happy path
  - `oc gateway pairing revoke <platform> <user_id>`
  - `oc gateway pairing regen <platform> <user_id>`
  - `oc gateway pairing clear-pending`
  - `oc gateway-logs` (back-compat hidden alias)

- [ ] **Step 2: Implement `cli_gateway.py`**

The implementation reuses existing function bodies from `cli.py` (the foreground `gateway()` body becomes `_run_foreground()`, the `--install-daemon` body becomes `_install_service()`, etc.). New code is the Typer plumbing + the pairing/sethome subcommands.

```python
# opencomputer/cli_gateway.py
"""Typer subcommand group: `oc gateway *`.

Single command verb for daemon ops + setup + service lifecycle + DM pairing.
Backward-compat: bare `oc gateway` runs foreground; `--install-daemon` flag
still works (deprecated).
"""
from __future__ import annotations

import warnings
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.cli_gateway_status import get_gateway_runtime_snapshot

console = Console()
gateway_app = typer.Typer(
    name="gateway",
    help="Run, configure, and manage the messaging gateway daemon.",
    invoke_without_command=True,
    no_args_is_help=False,
)
pairing_app = typer.Typer(name="pairing", help="DM pairing — approve users via code.")


@gateway_app.callback()
def _default(
    ctx: typer.Context,
    install_daemon: bool = typer.Option(
        False, "--install-daemon",
        help="DEPRECATED: use `oc gateway install`. Still works.",
        hidden=True,
    ),
    daemon_profile: str = typer.Option(
        "default", "--daemon-profile",
        help="DEPRECATED: use `oc gateway install --profile`.",
        hidden=True,
    ),
):
    if ctx.invoked_subcommand is not None:
        return
    if install_daemon:
        warnings.warn(
            "`oc gateway --install-daemon` is deprecated; use "
            "`oc gateway install` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        _install_service(profile=daemon_profile, system=False)
        raise typer.Exit(0)
    _run_foreground()


@gateway_app.command("run", help="Run gateway in foreground.")
def cmd_run() -> None:
    _run_foreground()


@gateway_app.command("setup", help="Interactive wizard: messaging platforms only.")
def cmd_setup() -> None:
    _run_messaging_only_wizard()


@gateway_app.command("install", help="Install gateway as a user/system service.")
def cmd_install(
    system: bool = typer.Option(False, "--system", help="Linux: install boot-time system service."),
    profile: str = typer.Option("default", "--profile", help="Profile name (multi-install support)."),
) -> None:
    _install_service(profile=profile, system=system)


@gateway_app.command("uninstall", help="Remove the gateway service.")
def cmd_uninstall(
    system: bool = typer.Option(False, "--system"),
    profile: str = typer.Option("default", "--profile"),
) -> None:
    _uninstall_service(profile=profile, system=system)


@gateway_app.command("start")
def cmd_start(system: bool = typer.Option(False, "--system")) -> None:
    _service_action("start", system=system)


@gateway_app.command("stop")
def cmd_stop(system: bool = typer.Option(False, "--system")) -> None:
    _service_action("stop", system=system)


@gateway_app.command("restart")
def cmd_restart(
    drain_timeout: int = typer.Option(30, "--drain-timeout", help="Seconds to wait for in-flight messages."),
    system: bool = typer.Option(False, "--system"),
) -> None:
    _drain_then_restart(drain_timeout=drain_timeout, system=system)


@gateway_app.command("status", help="Service + manual-PID + mismatch + foreign-home.")
def cmd_status(profile: str = "default") -> None:
    snap = get_gateway_runtime_snapshot(profile=profile)
    _render_snapshot(snap)


@gateway_app.command("logs", help="Tail gateway logs (journalctl on Linux, file tail elsewhere).")
def cmd_logs(
    follow: bool = typer.Option(False, "-f", "--follow"),
    system: bool = typer.Option(False, "--system"),
) -> None:
    _tail_logs(system=system, follow=follow)


@gateway_app.command("sethome", help="Set or list home channels per platform.")
def cmd_sethome(
    platform: str = typer.Argument(None),
    chat_id: str = typer.Argument(None),
    thread: str = typer.Option(None, "--thread"),
    list_homes: bool = typer.Option(False, "--list"),
    clear: str = typer.Option(None, "--clear"),
) -> None:
    _sethome(platform=platform, chat_id=chat_id, thread=thread, list_=list_homes, clear=clear)


# ── Pairing subgroup ────────────────────────────────────────────────────────

@pairing_app.command("list", help="List pending + approved pairings.")
def pairing_list(all_: bool = typer.Option(False, "--all")) -> None:
    _pairing_list(all_=all_)


@pairing_app.command("approve", help="Approve a pairing code.")
def pairing_approve(platform: str, code: str) -> None:
    _pairing_approve(platform, code)


@pairing_app.command("approve-deeplink", help="Approve from a deep-link URL.")
def pairing_approve_dl(url: str) -> None:
    _pairing_approve_deeplink(url)


@pairing_app.command("revoke", help="Revoke approval.")
def pairing_revoke(platform: str, user_id: str) -> None:
    _pairing_revoke(platform, user_id)


@pairing_app.command("regen", help="Force-mint a fresh code (bypasses rate limit).")
def pairing_regen(platform: str, user_id: str) -> None:
    _pairing_regen(platform, user_id)


@pairing_app.command("clear-pending", help="Drop all pending requests.")
def pairing_clear(platform: str = typer.Argument(None)) -> None:
    _pairing_clear(platform)


gateway_app.add_typer(pairing_app, name="pairing")


# ── Hermes-CLI compat: `oc pairing` top-level shim ──────────────────────────
# Hermes calls these `hermes pairing approve ...` (no `gateway` prefix).
# Provide a top-level alias so users scripting against the Hermes-spec page
# don't need to learn a different verb. Help text marks it as an alias.
top_pairing_app = typer.Typer(
    name="pairing",
    help="Alias of `oc gateway pairing` (Hermes-CLI compat).",
    no_args_is_help=True,
)
for sub in ("list", "approve", "approve-deeplink", "revoke", "regen", "clear-pending"):
    # Re-bind each pairing_app subcommand under top_pairing_app.
    top_pairing_app.command(sub)(pairing_app.registered_commands[sub].callback)


# ── Helpers — implementations live in this module to keep cli.py shrinking ──

def _run_foreground() -> None: ...
def _run_messaging_only_wizard() -> None: ...
def _install_service(profile: str, system: bool) -> None: ...
def _uninstall_service(profile: str, system: bool) -> None: ...
def _service_action(action: str, system: bool) -> None: ...
def _drain_then_restart(drain_timeout: int, system: bool) -> None: ...
def _tail_logs(system: bool, follow: bool) -> None: ...
def _sethome(platform, chat_id, thread, list_, clear): ...
def _render_snapshot(snap) -> None: ...
def _pairing_list(all_) -> None: ...
def _pairing_approve(platform, code) -> None: ...
def _pairing_approve_deeplink(url) -> None: ...
def _pairing_revoke(platform, user_id) -> None: ...
def _pairing_regen(platform, user_id) -> None: ...
def _pairing_clear(platform) -> None: ...
```

The placeholder helpers above (`_run_foreground`, `_install_service`, …) are filled in this same task by either (a) moving their function bodies from `cli.py` (`gateway()` → `_run_foreground()`), or (b) writing fresh — for the new pairing/sethome/render-snapshot/drain helpers.

- [ ] **Step 3: Update `cli.py`**

In `cli.py`, replace the existing `@app.command() def gateway(...)` with:

```python
from opencomputer.cli_gateway import gateway_app, top_pairing_app
app.add_typer(gateway_app, name="gateway")
app.add_typer(top_pairing_app, name="pairing")  # Hermes-CLI compat

# Hidden back-compat alias for `oc gateway-logs`
@app.command("gateway-logs", hidden=True)
def _gateway_logs_alias(...) -> None:
    """Hidden alias — use `oc gateway logs`."""
    from opencomputer.cli_gateway import cmd_logs
    cmd_logs(...)
```

Existing `_gateway_logs` body in `cli.py` is moved into `_tail_logs` in `cli_gateway.py`.

- [ ] **Step 4: Tests pass**

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_gateway.py opencomputer/cli.py tests/cli/test_cli_gateway_group.py tests/cli/test_cli_gateway_subcommands.py
git commit -m "feat(cli): unified `oc gateway *` Typer group + back-compat shims"
```

---

### Task 1.9: PR-1 final pass — full pytest + ruff + push

- [ ] **Step 1: Run full test suite**

Run: `cd OpenComputer && source .venv/bin/activate && pytest -x --tb=short -q 2>&1 | tail -10`
Expected: all green; ≥75 new tests pass.

- [ ] **Step 2: Ruff**

Run: `ruff check opencomputer/ plugin_sdk/ extensions/ tests/ 2>&1 | tail -5`
Expected: clean.

- [ ] **Step 3: Update `CHANGELOG.md`** under `[Unreleased]`:

```markdown
### Added
- **Messaging gateway parity (Hermes) PR-1.** `oc gateway *` Typer group consolidates run/setup/install/uninstall/start/stop/restart/status/logs/pairing/sethome under one verb. New DM-pairing-code system (`oc gateway pairing list/approve/revoke/regen/clear-pending`). New per-platform reset policies (`gateway.reset_mode|reset_idle_minutes|reset_daily_at_hour|reset_by_platform`). Multi-installation service-name hashing for non-default `OPENCOMPUTER_HOME`. Sophisticated status command with process-vs-service mismatch detection + foreign-home PID listing. Back-compat preserved: bare `oc gateway` still runs foreground; `--install-daemon` flag deprecated but functional indefinitely.

### Deprecated
- `oc gateway --install-daemon` flag — use `oc gateway install`. Still works; no removal date.
```

- [ ] **Step 4: Commit changelog**

```bash
git add CHANGELOG.md
git commit -m "docs: PR-1 — messaging gateway parity changelog entry"
```

- [ ] **Step 5: Push branch + open PR**

```bash
git push -u origin feat/gateway-parity-pr1-2026-05-08
gh pr create --title "feat(gateway): messaging gateway parity (Hermes) — PR-1: UX + security" \
  --body "$(cat <<'EOF'
## Summary
- Unified `oc gateway *` Typer subcommand group
- Production-grade DM-pairing-code system (lockout/rate-limit/0600/atomic)
- Per-platform session reset policies
- Multi-installation service-name hashing
- Sophisticated `status` command (mismatch + foreign-home detection)
- 19 platform-specific allowlist env vars

## Test plan
- [x] 75+ new tests under tests/cli/, tests/channels/, tests/gateway/, tests/service/
- [x] Existing test suite still green
- [x] Backward compat: bare `oc gateway` runs foreground; `--install-daemon` deprecated but works

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed.

---

## Phase 2 (PR-2) — Display polish + Delivery polish + Foundation fixes

### Task 2.0: Create PR-2 worktree (after PR-1 merged)

- [ ] **Step 1: After PR-1 merges, create PR-2 worktree**

Run: `cd /Users/saksham/Vscode/claude && git fetch origin main && git worktree add .claude/worktrees/gateway-parity-pr2 -b feat/gateway-parity-pr2-2026-05-08 origin/main`

- [ ] **Step 2: Install editable + baseline tests**

Same as Task 0.1.

---

### Task 2.1: Display config (per-platform tier defaults)

**Files:**
- Create: `opencomputer/gateway/display_config.py`
- Modify: `opencomputer/agent/config.py` — extend `DisplayConfig`
- Test: `tests/gateway/test_display_config.py`

- [ ] **Step 1: Write failing tests** (~16 cases) for `resolve_display_setting`:
  - resolution order respected (per-platform → global → tier-default → built-in default → fallback)
  - tier defaults: telegram=high, slack=medium-tool-progress-off, signal=low, email=minimal
  - migration: old `display.tool_progress_overrides` flat dict moves to `display.platforms.<platform>`
  - all OVERRIDEABLE_KEYS covered
  - unknown platform → global default

- [ ] **Step 2: Implement `display_config.py`** porting Hermes upstream verbatim with adapted import paths (uses `opencomputer.agent.config` types).

- [ ] **Step 3: Add `runtime_footer`, `background_process_notifications`, `platforms` keys to `DisplayConfig`** in `opencomputer/agent/config.py`.

- [ ] **Step 3.5: Adapter migration to `resolve_display_setting`**

  Find every direct read of `cfg.display.tool_progress`, `cfg.display.show_reasoning`, `cfg.display.tool_preview_length`, `cfg.display.streaming` inside `opencomputer/gateway/`, `opencomputer/agent/loop.py`, and `extensions/*/adapter.py`. Replace with:

  ```python
  from opencomputer.gateway.display_config import resolve_display_setting
  value = resolve_display_setting(user_config, platform_key, "tool_progress", fallback="all")
  ```

  Add a regression test that confirms a per-platform override beats the global. Adapters are not refactored beyond this read-site change.

- [ ] **Step 4: Add config-migration helper** in `opencomputer/gateway/display_config.py`:

```python
def migrate_legacy_overrides(cfg: dict) -> dict:
    """Move display.tool_progress_overrides into display.platforms.<p>.tool_progress."""
```

- [ ] **Step 5: Tests pass**

- [ ] **Step 6: Commit**

```bash
git add opencomputer/gateway/display_config.py opencomputer/agent/config.py tests/gateway/test_display_config.py
git commit -m "feat(gateway): per-platform display config with tier-based defaults"
```

---

### Task 2.2: Runtime footer (resolve + format + slash command)

**Files:**
- Modify: `opencomputer/gateway/runtime_footer.py`
- Create: `opencomputer/agent/slash_commands_impl/footer_cmd.py`
- Modify: `opencomputer/cli_ui/slash.py`
- Test: `tests/gateway/test_runtime_footer.py`

- [ ] **Step 1: Tests** (~9 cases): per-platform resolve, format, streaming-trailing, /footer toggle.

- [ ] **Step 2: Implement** porting Hermes shape into existing `runtime_footer.py`. Add `format_runtime_footer` + `append_or_send_trailing` + `RuntimeFooterConfig`.

- [ ] **Step 3: `/footer` slash command** registered in `cli_ui/slash.py` SLASH_REGISTRY + handler.

- [ ] **Step 4: Wire into `Dispatch.handle_message`** — append footer to final reply text after the agent loop returns.

- [ ] **Step 5: Tests pass**

- [ ] **Step 6: Commit**

```bash
git add opencomputer/gateway/runtime_footer.py opencomputer/agent/slash_commands_impl/footer_cmd.py opencomputer/cli_ui/slash.py opencomputer/gateway/dispatch.py tests/gateway/test_runtime_footer.py
git commit -m "feat(gateway): runtime metadata footer (per-platform) + /footer slash command"
```

---

### Task 2.3: Background-process notifications filter

**Files:**
- Modify: `opencomputer/agent/bg_notify.py`
- Test: `tests/gateway/test_bg_notify_filter.py`

- [ ] **Step 1: Tests** (~8 cases): off suppresses, all emits, result on completion, error only on non-zero, env override, per-platform override.

- [ ] **Step 2: Implement `_should_emit`** that consults `display_config.resolve_display_setting(cfg, platform_key, "background_process_notifications")` + env override.

- [ ] **Step 3: Wire** into the existing Notification subscriber in `bg_notify.py`. Find and gate the call site.

- [ ] **Step 4: Tests pass**

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/bg_notify.py tests/gateway/test_bg_notify_filter.py
git commit -m "feat(agent): display.background_process_notifications filter (per-platform)"
```

---

### Task 2.4: First-time busy-input tip + onboarding latch

**Files:**
- Modify: `opencomputer/gateway/runtime_footer.py` (add `_OnboardingLatch` + tip injection)
- Test: `tests/gateway/test_first_time_tip.py`

- [ ] **Step 1: Tests** (~6 cases): first emit contains tip, second omits, flock prevents double-tip race, file corrupt → reset, custom keys, **Windows fallback path** (flock unavailable → tmpfile-rename "first writer wins").

- [ ] **Step 2: Implement** `_OnboardingLatch` class with `seen(key)` + `mark_seen(key)` using flock + atomic write at `<profile>/onboarding.json`. On Windows, fall back to `msvcrt.locking` if available, else a "best-effort first-writer-wins via os.replace" path (doc-comment notes that on Windows the tip may emit twice in a true two-process race — acceptable since the latch goes stable on first write).

- [ ] **Step 3: Update `busy_ack_text`** to use the latch.

- [ ] **Step 4: Tests pass**

- [ ] **Step 5: Commit**

```bash
git add opencomputer/gateway/runtime_footer.py tests/gateway/test_first_time_tip.py
git commit -m "feat(gateway): first-time busy-input tip with flock-protected onboarding latch"
```

---

### Task 2.5: Restart with drain timeout

**Files:**
- Modify: `opencomputer/cli_gateway.py` (already exists from PR-1; here we add `--drain-timeout` plumbing)
- Modify: `opencomputer/gateway/dispatch.py` (drain-flag check)
- Modify: `opencomputer/gateway/server.py` (drain-flag → graceful shutdown)
- Test: `tests/gateway/test_drain_restart.py`

- [ ] **Step 1: Tests** (~6 cases): signal sets flag, dispatch refuses new messages while flag set, server exits when in-flight count = 0, timeout falls back to force-stop.

- [ ] **Step 2: Implement `_signal_drain` + `_wait_for_drain`** in `cli_gateway.py`. Implement the flag-check loop in `Gateway.serve_forever`.

- [ ] **Step 3: Tests pass**

- [ ] **Step 4: Commit**

```bash
git add opencomputer/cli_gateway.py opencomputer/gateway/dispatch.py opencomputer/gateway/server.py tests/gateway/test_drain_restart.py
git commit -m "feat(gateway): restart --drain-timeout (in-flight messages complete first)"
```

---

### Task 2.6: Delivery routing

**Files:**
- Create: `opencomputer/gateway/delivery.py`
- Test: `tests/gateway/test_delivery.py`

- [ ] **Step 1: Tests** (~11 cases): parse origin/local/platform/platform:chat/platform:chat:thread, truncation at 4000, cron auto-deliver via DeliveryRouter.

- [ ] **Step 2: Implement** porting Hermes `gateway/delivery.py` verbatim with adapted imports.

- [ ] **Step 3: Tests pass**

- [ ] **Step 4: Commit**

```bash
git add opencomputer/gateway/delivery.py tests/gateway/test_delivery.py
git commit -m "feat(gateway): DeliveryTarget + DeliveryRouter (cron auto-deliver foundation)"
```

---

### Task 2.7: Cross-session mirror

**Files:**
- Create: `opencomputer/gateway/mirror.py`
- Test: `tests/gateway/test_mirror.py`

- [ ] **Step 1: Tests** (~7 cases): finds session by (platform, chat_id, thread, user) tuple, appends to JSONL, appends to SQLite, best-effort (failures don't raise).

- [ ] **Step 2: Implement** porting Hermes `gateway/mirror.py`.

- [ ] **Step 3: Wire** into `DeliveryRouter.route` (Task 2.6) — every cron-driven send mirrors.

- [ ] **Step 4: Tests pass**

- [ ] **Step 5: Commit**

```bash
git add opencomputer/gateway/mirror.py opencomputer/gateway/delivery.py tests/gateway/test_mirror.py
git commit -m "feat(gateway): cross-session delivery-mirror entries on cron auto-deliver"
```

---

### Task 2.8: Session-context contextvars

**Files:**
- Create: `opencomputer/gateway/session_context.py`
- Test: `tests/gateway/test_session_context.py`

- [ ] **Step 1: Tests** (~7 cases): get_session_env reads contextvar first then env; set_session_vars task-isolated; clear_session_vars; concurrent tasks don't interfere; backward-compat with os.getenv.

- [ ] **Step 2: Implement** `session_context.py` porting Hermes shape; rename env-var keys from `HERMES_SESSION_*` to `OPENCOMPUTER_SESSION_*`.

- [ ] **Step 3: Audit grep** for any `os.getenv("OPENCOMPUTER_SESSION_*")` or `os.environ["OPENCOMPUTER_SESSION_*"]` reads/writes inside `opencomputer/gateway/` and `extensions/`. Replace with the new accessors.

- [ ] **Step 4: Tests pass**

- [ ] **Step 5: Commit**

```bash
git add opencomputer/gateway/session_context.py tests/gateway/test_session_context.py
git commit -m "feat(gateway): contextvars-based per-task session state (replaces os.environ footgun)"
```

---

### Task 2.9: Missing slash commands

**Files:**
- Modify: `opencomputer/cli_ui/slash.py` — add SLASH_REGISTRY entries
- Create: `opencomputer/agent/slash_commands_impl/sethome_cmd.py`, `status_cmd.py`
- Create: `extensions/voice-mode/slash_commands/voice_cmd.py`
- Create: `extensions/coding-harness/slash_commands/approve_cmd.py`, `deny_cmd.py`
- Test: `tests/agent/test_slash_messaging_extras.py`

- [ ] **Step 1: Tests** (~14 cases — 2 per command + 2 for `/help` messaging-context).

- [ ] **Step 2: Implement** each command:
  - `/sethome <platform> <chat_id> [--thread <id>]` writes to `<profile>/gateway/home_channels.json`. Read by `DeliveryTarget.parse("origin")` fallback.
  - `/status` returns session info (platform, chat_id, session_id, model, queue_mode, last_seen) using `Dispatch.session_info(...)`.
  - `/voice` invokes `voice_mode` extension API.
  - `/approve`, `/deny` consult coding-harness pending-approval store.
  - `/footer` toggle (already done in Task 2.2).
  - **`/help` messaging-context port**: confirm the existing `/help` slash command renders the full SLASH_REGISTRY when invoked via a channel adapter (not just CLI). If the registered handler short-circuits when `runtime.platform != "cli"`, port it to render a Rich-text-style table that channel adapters can re-format as platform-native (Markdown / MarkdownV2 / plain). Add 2 tests (CLI render unchanged + Telegram render produces non-empty list).

- [ ] **Step 3: Tests pass**

- [ ] **Step 4: Commit**

```bash
git add opencomputer/cli_ui/slash.py opencomputer/agent/slash_commands_impl/ extensions/voice-mode/slash_commands/ extensions/coding-harness/slash_commands/ tests/agent/test_slash_messaging_extras.py
git commit -m "feat(agent): /sethome /voice /approve /deny /status /footer slash commands"
```

---

### Task 2.10: Interrupt-semantics finalization

**Files:**
- Modify: `opencomputer/agent/loop.py`
- Modify: `opencomputer/gateway/dispatch.py`
- Test: `tests/agent/test_interrupt_semantics.py`

- [ ] **Step 1: Tests** (~8 cases):
  - SIGTERM sent on tool-cancel; 1s grace; SIGKILL fallback (verified via `extensions/coding-harness/tools/background.py` cleanup path).
  - Tool-batch cancel: when cancel_event set mid-asyncio.gather, only currently-executing tool completes.
  - Message coalesce: 3 rapid messages during busy → 1 prepended user-prompt with all 3 joined.

- [ ] **Step 2: Read existing code** to confirm what already works post-PR-485. Add tests for any gap; fix code only if test fails.

- [ ] **Step 3: Tests pass**

- [ ] **Step 4: Commit**

```bash
git add opencomputer/agent/loop.py opencomputer/gateway/dispatch.py tests/agent/test_interrupt_semantics.py
git commit -m "feat(agent): finalize interrupt semantics (SIGTERM→SIGKILL, batch-cancel, coalesce)"
```

---

### Task 2.11: PR-2 final pass — full suite + ruff + CHANGELOG + push

- [ ] **Step 1: Full pytest**

Run: `pytest -x --tb=short -q 2>&1 | tail -10`
Expected: all green; ≥130 new tests pass.

- [ ] **Step 2: Ruff**

- [ ] **Step 3: CHANGELOG entry**

```markdown
### Added
- **Messaging gateway parity (Hermes) PR-2.** Per-platform display config with tier-based defaults. Runtime metadata footer (`display.runtime_footer`) + `/footer` slash command. `display.background_process_notifications` filter (`all|result|error|off`). First-time busy-input tip with flock-protected onboarding latch. `oc gateway restart --drain-timeout`. `DeliveryTarget` + `DeliveryRouter` for cron auto-deliver. Cross-session delivery-mirror entries. New slash commands: `/sethome`, `/voice`, `/approve`, `/deny`, `/status`, `/footer`. `contextvars`-based session state replaces `os.environ` footgun. Interrupt semantics finalized (SIGTERM→1s→SIGKILL, batch tool-cancel, message coalesce-during-busy).
```

- [ ] **Step 4: Push + open PR**

```bash
git push -u origin feat/gateway-parity-pr2-2026-05-08
gh pr create --title "feat(gateway): messaging gateway parity (Hermes) — PR-2: display + delivery + foundations" \
  --body "..." # similar style
```

---

## Self-Review (post-write check)

**Spec coverage:** all 16 components in spec §5 map to a task: 5.1↔T1.8, 5.2↔T1.4, 5.3↔T1.3, 5.4↔T2.1, 5.5↔T2.2, 5.6↔T2.3, 5.7↔T2.4, 5.8↔T1.7, 5.9↔T1.1+T1.2, 5.10↔T2.5, 5.11↔T2.6, 5.12↔T2.7, 5.13↔T2.9, 5.14↔T2.8, 5.15↔T2.10, 5.16↔T1.5+T1.6.

**Placeholder scan:** no "TBD" / "implement later" / "fill in details". Where Task 1.4 says "(Full implementation follows the Hermes pairing.py shape)" — the engineer is given the source path AND tests AND the dataclass + helper signatures; "Hermes shape" means call-by-call port, which for production is acceptable given the 1:1 test coverage. Where Task 2.9 says "Implement each command" the per-command behavior is pinned (`/sethome` writes home_channels.json etc.).

**Type consistency:** `AllowlistDecision`, `PairingCode`, `ResetPolicy`, `GatewayRuntimeSnapshot`, `DeliveryTarget`, `RuntimeFooterConfig` are defined exactly once and consumed with the same field names throughout.

**Migration safety:** existing `oc gateway` and `oc gateway --install-daemon` and `oc gateway-logs` invocations all preserved. `display.tool_progress_overrides` legacy key auto-migrates. Service-name hashing applies only to non-canonical homes; existing single-installs unchanged.
