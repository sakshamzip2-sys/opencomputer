# Always-On Daemon — Cross-Platform Design

**Date:** 2026-05-03
**Status:** approved (ready for plan)
**Author:** Saksham (with Claude)
**Scope:** C — full cross-platform parity (macOS + Linux + Windows) with onboarding integration, doctor health checks, and a runbook
**Reference:** OpenClaw `--install-daemon` mechanic (launchd / systemd / Windows Services)

---

## 1. Motivation

OpenClaw "feels" never-stopped because its `--install-daemon` flag registers it as an OS-level service: launchd on macOS, systemd-user on Linux, Windows Services on Windows. The OS — not OpenClaw itself — keeps the agent alive across crashes, terminal sessions, and reboots. Combined with credentials persisted to `~/.openclaw/credentials/`, this lets an OpenClaw laptop power on after months of being off and immediately ping the user on Telegram with no re-auth.

OpenComputer already has the **Linux half** of this story:
- `oc gateway` long-running daemon ✅
- `oc --headless` flag ✅
- `service/templates/opencomputer.service` systemd unit (Restart=always, journald) ✅
- `oc service install/uninstall/status` CLI (Linux-only) ✅

What's missing: the **macOS half** (the gateway plist exists only inside the setup wizard as inline XML, not exposed via `oc service install`), the **Windows half** entirely, and a **unified cross-platform CLI** that doesn't require the user to know what their OS's service manager is called.

This spec closes that gap.

## 2. Goals

1. `oc service install/uninstall/status/start/stop/logs/doctor` works uniformly on **macOS, Linux, Windows**.
2. **macOS gateway plist** uses the modern `launchctl bootstrap gui/<uid>` API (not the deprecated `launchctl load`) and lives in `service/templates/` like its Linux counterpart, not as inline XML in the wizard.
3. **Windows Task Scheduler** registration via `schtasks.exe`, user scope (no admin elevation).
4. `oc setup --install-daemon` and `oc gateway --install-daemon` convenience flags for first-run onboarding and power-user one-shot install.
5. Setup wizard's launchd-service section becomes platform-agnostic — it prompts on every platform and calls the same factory, removing ~80 LOC of inline plist XML.
6. `oc service status` returns rich, structured information: enabled, running, PID, uptime, last 5 log lines.
7. `docs/runbooks/always-on-daemon.md` documents the install/verify/uninstall flow per platform plus the `enable-linger` Linux-VPS trick.
8. **Backward compat:** existing `service/__init__.py` public functions and `oc service install/uninstall/status` keep working unchanged; existing tests stay green.

## 3. Non-goals

- **System-wide install (Linux `/etc/systemd/system`, macOS `/Library/LaunchDaemons`, Windows Services via `sc.exe`).** All install paths in this spec are user-scope. System-scope requires root/admin and is out of scope; document as a manual-fallback recipe in the runbook only.
- **Docker/Kubernetes deployment.** Mentioned as alternative in the runbook but not implemented as a backend.
- **Homebrew formula `brew services` integration.** Park as future possibility.
- **Plugin extension point for service backends.** Service install is core infra; circular bootstrap. YAGNI.
- **`@reboot` cron.** Wrong primitive (no restart-on-crash, deprecated on macOS, missing on Windows).
- **Touching `service/launchd.py`** (the daily profile-analyze cron). Different concern; explicitly preserved.

## 4. Architecture

### 4.1 Module layout

```
opencomputer/service/
├── __init__.py                 # backward-compat shims (delegate to factory.get_backend())
├── base.py                     # ServiceBackend Protocol + result dataclasses
├── factory.py                  # get_backend() — sys.platform dispatch
├── _common.py                  # resolve_executable(), log_paths(), workdir() helpers
├── _linux_systemd.py           # systemd-user backend (existing logic moved here)
├── _macos_launchd.py           # NEW — gateway plist backend (modern bootstrap API)
├── _windows_schtasks.py        # NEW — Task Scheduler backend (user scope, no admin)
├── launchd.py                  # UNCHANGED — daily profile-analyze cron (different concern)
└── templates/
    ├── opencomputer.service              # KEPT (Linux systemd unit)
    ├── com.opencomputer.gateway.plist    # NEW (macOS launchd plist for gateway)
    ├── opencomputer-task.xml             # NEW (Windows Task Scheduler XML)
    ├── com.opencomputer.profile-analyze.plist   # KEPT
    ├── opencomputer-profile-analyze.timer       # KEPT
    └── opencomputer-profile-analyze.service     # KEPT
```

### 4.2 Protocol contract

```python
# service/base.py
from __future__ import annotations
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol

@dataclass(frozen=True)
class InstallResult:
    backend: str            # "systemd" | "launchd" | "schtasks"
    config_path: Path
    enabled: bool
    started: bool
    notes: list[str]        # actionable hints (e.g., enable-linger reminder)

@dataclass(frozen=True)
class StatusResult:
    backend: str
    file_present: bool
    enabled: bool
    running: bool
    pid: int | None
    uptime_seconds: float | None
    last_log_lines: list[str]    # ~5 lines for at-a-glance health

@dataclass(frozen=True)
class UninstallResult:
    backend: str
    file_removed: bool
    config_path: Path | None
    notes: list[str]

class ServiceUnsupportedError(RuntimeError):
    """Raised when the current platform has no service backend."""

class ServiceBackend(Protocol):
    NAME: ClassVar[str]
    def supported(self) -> bool: ...
    def install(self, *, profile: str, extra_args: str, restart: bool = True) -> InstallResult: ...
    def uninstall(self) -> UninstallResult: ...
    def status(self) -> StatusResult: ...
    def start(self) -> bool: ...
    def stop(self) -> bool: ...
    def follow_logs(self, *, lines: int = 100, follow: bool = False) -> Iterator[str]: ...
```

Each backend module exports module-level functions matching this Protocol. The factory returns the *module object* — Pythonic structural polymorphism, no class scaffolding.

### 4.3 Factory dispatch

```python
# service/factory.py
import sys
from . import base

def get_backend() -> base.ServiceBackend:
    if sys.platform == "darwin":
        from . import _macos_launchd as backend
    elif sys.platform.startswith("linux"):
        from . import _linux_systemd as backend
    elif sys.platform.startswith("win"):
        from . import _windows_schtasks as backend
    else:
        raise base.ServiceUnsupportedError(
            f"no service backend for sys.platform={sys.platform!r}"
        )
    return backend  # mypy/pyright validates the Protocol structurally
```

### 4.4 Backend responsibilities

| Backend | NAME | install registers via | status probe via | logs source |
|---|---|---|---|---|
| `_linux_systemd` | `"systemd"` | `systemctl --user daemon-reload && enable --now opencomputer.service` | `is-active`, `show -p MainPID,ActiveEnterTimestamp` | `journalctl --user -u opencomputer.service` |
| `_macos_launchd` | `"launchd"` | `launchctl bootstrap gui/<uid> <plist>` | `launchctl print gui/<uid>/<label>` (parse `pid`, `state`) | tail `~/.opencomputer/<profile>/logs/gateway.{stdout,stderr}.log` |
| `_windows_schtasks` | `"schtasks"` | `schtasks /create /xml task.xml /tn OpenComputerGateway /f` | `schtasks /query /v /fo list` (parse `Status`) + `tasklist /FI "PID eq <pid>"` | tail `%USERPROFILE%\.opencomputer\<profile>\logs\gateway.{stdout,stderr}.log` |

### 4.5 Common helpers (`_common.py`)

- `resolve_executable() -> str` — search `shutil.which("oc")`, then `shutil.which("opencomputer")`, then known fallbacks: `/opt/homebrew/bin/oc`, `~/.local/bin/oc`, `~/.pyenv/shims/oc`, `Path(sys.executable).parent / "oc"`. Raise `RuntimeError` with actionable message if none found.
- `log_paths(profile: str) -> tuple[Path, Path]` — returns `(stdout_log, stderr_log)`, both under `~/.opencomputer/<profile>/logs/`.
- `workdir(profile: str) -> Path` — `~/.opencomputer/<profile>` (created if absent).
- `tail_lines(path: Path, n: int) -> list[str]` — efficient last-N-lines tail (used by every backend's `follow_logs`).

## 5. CLI surface

### 5.1 New / extended subcommands

```
oc service install   [--profile=default] [--extra-args=""] [--no-start] [--json]
oc service uninstall [--json]
oc service status    [--json]      # rich: enabled/running/pid/uptime/last 5 log lines
oc service start                   # OS-level start (no install)
oc service stop                    # OS-level stop (don't uninstall)
oc service logs      [--follow] [-n LINES]
oc service doctor                  # diagnostic
```

### 5.2 Convenience flags

- **`oc setup --install-daemon`** — runs the wizard with the service-install section forced to "yes". Installs the service after wizard completion.
- **`oc gateway --install-daemon`** — calls `service install` and exits (does NOT run gateway in foreground).

### 5.3 `oc service doctor` diagnostic checks

Returns a structured health report. Each check is one of `OK | WARN | FAIL | N/A`:

| Check | OK condition |
|---|---|
| `executable_resolvable` | `resolve_executable()` finds `oc`/`opencomputer` |
| `config_file_present` | service config file exists at expected path |
| `service_enabled` | OS reports the service as enabled (per backend) |
| `service_running` | OS reports it as currently running |
| `rpc_probe` | best-effort: connect to wire server (`ws://127.0.0.1:18789`) — `WARN` if unreachable, `N/A` if wire server disabled |
| `linger_enabled` (Linux only) | `loginctl show-user $USER --property=Linger` returns `Linger=yes` — `WARN` if not, `N/A` on macOS/Windows |
| `recent_crashes` | last 5 log lines do not contain `Traceback` / `panic` patterns |
| `executable_matches_install` | `resolve_executable()` matches the path written into the config file (catches stale install after `pip install --upgrade`) |

### 5.4 Wizard consolidation

`opencomputer/cli_setup/section_handlers/launchd_service.py`:
- Renamed to `service_install.py`
- Becomes platform-agnostic (was: macOS-only no-op on Linux/Windows)
- Loses ~80 LOC of inline plist XML
- Calls `service.factory.get_backend().install(...)` once
- Old import path (`launchd_service`) kept as alias module for one release with a deprecation warning

## 6. Data flow

### 6.1 Boot sequence — the "magic Telegram message"

```
1. OS finishes booting / user logs in (~10–30s)
2. OS reads service config:
   • Linux  : ~/.config/systemd/user/opencomputer.service       (linger required if no GUI)
   • macOS  : ~/Library/LaunchAgents/com.opencomputer.gateway.plist
   • Windows: registered Task Scheduler entry "OpenComputerGateway"
3. OS executes:  oc --headless --profile <p> gateway run
4. oc loads ~/.opencomputer/<profile>/config.yaml + per-profile credentials
5. Gateway loads channel adapters → each adapter reconnects (bot identity is server-side persistent)
6. Adapters announce "online"; heartbeat scheduler ticks
7. (Optional) First heartbeat sends a "back online" message — the "magic ping"
```

The only state that needs to persist across shutdown: the **service-manager config file** + the **per-profile credentials in `~/.opencomputer/<profile>/`**. Both are just files. On boot, the agent fully reconstitutes — no re-auth, no re-pairing.

### 6.2 Install sequence (`oc service install`)

```
1. resolve_executable()                    → finds `oc` on PATH; fallback to known locations
2. resolve_workdir() + log_paths()         → ~/.opencomputer/<profile>, .../logs/{stdout,stderr}.log
3. render template with substitutions      → backend-specific (.service / .plist / .xml)
4. write to OS-managed location            → atomic via tmp-file + rename
5. backend "register" call                 → systemctl / launchctl / schtasks
6. probe status                            → InstallResult with notes
   (Linux: "On a headless server, run `sudo loginctl enable-linger $USER`...")
```

### 6.3 Idempotency

`install()` called twice in a row must succeed both times. Each backend's `install()`:
1. If config file exists, calls its own `uninstall()` first (best effort — ignore "not loaded" errors)
2. Writes new config file
3. Calls backend register command
4. Returns fresh `InstallResult`

## 7. Error handling

| Failure mode | Behavior |
|---|---|
| Unsupported platform (e.g., FreeBSD) | `ServiceUnsupportedError(f"no service backend for sys.platform={...}")` — actionable message |
| File-write permission denied | clear message + manual install snippet printed |
| `launchctl bootstrap` / `systemctl enable` exits non-zero | file remains on disk; `InstallResult.enabled=False`; `notes` carries the manual command |
| Idempotent re-install | second call unloads/disables old, then writes/reloads new — no error |
| `oc` not on PATH and no fallback works | actionable error: `"could not find oc executable. Tried: $PATH, /opt/homebrew/bin, ~/.local/bin, ~/.pyenv/shims, sys.executable parent. Set OC_EXECUTABLE env var or run `which oc` to debug."` |
| Hung subprocess (`launchctl print` hangs) | 10s timeout via `subprocess.run(timeout=10)` — degrades to "unknown" status, never wedges CLI |
| Existing wizard plist (legacy `launchctl load`-installed) detected on macOS | install detects it, calls `launchctl bootout` first, then re-bootstraps with new plist |
| Windows: Task Scheduler service stopped (rare on user machines) | `schtasks` errors → degrade to "running=False, enabled=False"; no crash |

## 8. Testing strategy

### 8.1 Unit tests per backend

Mock `subprocess.run` with `pytest.MonkeyPatch`. For each backend, assert:
- Correct CLI args passed to `systemctl`/`launchctl`/`schtasks`
- Rendered file content matches fixture
- Status parsing handles every observed real-world output shape

### 8.2 Format validation (no subprocess)

- **plist:** `xml.etree.ElementTree.fromstring(rendered_plist)` — round-trip parse
- **systemd unit:** `configparser.ConfigParser()` parses (treat as INI)
- **Task XML:** `xml.etree.ElementTree.fromstring(rendered_xml)`

### 8.3 Factory dispatch

`monkeypatch.setattr(sys, "platform", "darwin"|"linux"|"win32")` then assert:
- correct backend module returned
- Protocol attributes exist (`NAME`, `install`, `uninstall`, `status`, `start`, `stop`, `follow_logs`, `supported`)
- unsupported platforms raise `ServiceUnsupportedError`

### 8.4 CLI tests

`typer.testing.CliRunner` invokes each subcommand. Backend mocked at factory level. Tests assert exit code + JSON-mode output shape.

### 8.5 Wizard test

Updated `service_install` section calls factory once; backend recorded by mock. Verifies platform-agnostic prompt rendering on Linux/macOS/Windows.

### 8.6 Existing tests must stay green

| Existing test file | Change |
|---|---|
| `tests/test_service_install.py` | unchanged (covers Linux systemd shims via `service/__init__.py`) |
| `tests/test_launchd_install.py` | unchanged (covers daily-cron launchd in `service/launchd.py` — different concern) |
| `tests/test_launchd_plist.py` | unchanged (daily-cron plist rendering) |
| `tests/test_cli_setup_section_launchd_service.py` | **renamed** to `tests/test_cli_setup_section_service_install.py`; test body adapted to call the renamed wizard section function |
| `tests/test_logging_journald.py` | unchanged |
| `tests/test_cli_setup_wizard_e2e.py` | unchanged (wizard end-to-end shape unchanged) |
| `tests/test_headless.py`, `tests/test_cli_headless_flag.py` | unchanged |

A new test, `tests/test_service_alias_deprecation.py`, asserts that importing the legacy `opencomputer.cli_setup.section_handlers.launchd_service` module emits a `DeprecationWarning`.

### 8.7 CI matrix

GitHub Actions `.github/workflows/test.yml` matrix expansion (CI infra change):
- **Linux runner** (ubuntu-latest): runs full suite including systemd backend tests
- **macOS runner** (macos-latest, NEW addition): runs full suite including launchd backend tests (subprocess-mocked — no real launchctl invocation)
- **Windows runner** (windows-latest, NEW addition): runs schtasks backend tests; skips systemd/launchd via `pytest.mark.skipif(sys.platform != "win32", ...)`

The matrix file currently runs Python 3.12 + 3.13 on Linux only. We extend the matrix to add `os: [ubuntu-latest, macos-latest, windows-latest]`. Since per-platform test files are mostly subprocess-mocked, real wall-time impact is ~3 min for macOS + ~5 min for Windows runners on every PR.

If macOS/Windows runner cost becomes a problem, fall back to running them only on PRs that touch `opencomputer/service/**` via path-filter rules.

### 8.8 Smoke test on Linux CI

After unit tests, run actual install + status + uninstall against the CI runner's user-systemd. Verify file written, `daemon-reload` invoked, and `is-active` returns expected state. Skip on macOS/Windows runners (subprocess-mocked tests cover them).

## 9. Backward compatibility

### 9.1 Public API preserved

`service/__init__.py` keeps every current public function. Two categories:

**Gateway-related shims (delegate to the new `_linux_systemd` module):**
- `install_systemd_unit(*, executable, workdir, profile, extra_args) -> Path`
- `uninstall_systemd_unit() -> Path | None`
- `is_active() -> bool`
- `render_systemd_unit(*, executable, workdir, profile, extra_args) -> str`

**Profile-analyze cron functions — NOT touched, stay in `__init__.py` unchanged:**
- `install_profile_analyze_timer(*, executable) -> tuple[Path, Path]`
- `uninstall_profile_analyze_timer() -> tuple[Path | None, Path | None]`
- `is_profile_analyze_timer_active() -> bool`

The profile-analyze cron is a daily scheduled timer (different concern from the always-on gateway daemon) and shares a code path with `service/launchd.py` (macOS counterpart of the same cron). It is explicitly out of scope for this refactor.

```python
# service/__init__.py — gateway shim example
def install_systemd_unit(*, executable, workdir, profile, extra_args) -> Path:
    """Deprecated: use opencomputer.service.factory.get_backend().install(...)."""
    from . import _linux_systemd
    return _linux_systemd.install_with_legacy_args(
        executable=executable, workdir=workdir,
        profile=profile, extra_args=extra_args,
    ).config_path
```

All 14+ existing import sites in tests and CLI continue to work without changes.

### 9.2 `service/launchd.py` preserved

The existing `service/launchd.py` (daily profile-analyze cron) is **not touched**. It is a different concern (scheduled job, not always-on supervisor). Future readers must distinguish:
- `service/launchd.py` → daily profile-analyze cron (existing)
- `service/_macos_launchd.py` → gateway always-on plist (new)

The naming is deliberate: leading-underscore for new internal modules; non-underscore for the existing public-facing module.

### 9.3 CLI signatures preserved

`oc service install/uninstall/status` keep their current required args. New optional flags (`--profile`, `--extra-args`, `--no-start`, `--json`) are additive.

### 9.4 Wizard alias

`opencomputer/cli_setup/section_handlers/launchd_service.py` is replaced with a 5-line alias module that re-exports `service_install.run_service_install_section as run_launchd_service_section` and emits a `DeprecationWarning` on import. Removed in next major release.

## 10. Rollout — single PR, staged commits

| # | Commit subject | Net LOC | Tests |
|---|---|---|---|
| 1 | `feat(service): Protocol + result dataclasses + common helpers` | +200 | unit tests for helpers |
| 2 | `feat(service): factory dispatch on sys.platform` | +50 | factory tests |
| 3 | `refactor(service): extract systemd-gateway code into _linux_systemd; shim __init__` (profile-analyze cron functions stay in __init__.py untouched) | +150 / -60 | existing tests pass |
| 4 | `feat(service): macOS launchd backend for gateway` | +250 | new launchd tests |
| 5 | `feat(service): Windows Task Scheduler backend` | +250 | new schtasks tests (Windows-gated) |
| 6 | `feat(cli): service start/stop/logs/doctor + --install-daemon flags` | +180 | CLI tests |
| 7 | `refactor(setup): platform-agnostic service_install wizard section` | +120 / -100 | adapted wizard test |
| 8 | `feat(doctor): service health section in oc doctor` | +80 | doctor test |
| 9 | `docs: always-on-daemon runbook + README link + CLAUDE.md update` | +400 (markdown) | n/a |

**Net code change:** ~+1,800 / -160 LOC. **Tests:** +30-40 new test cases.

**Single PR:** `feat(service): cross-platform always-on daemon (macOS launchd + Windows schtasks + unified CLI)`

## 11. Documentation

### 11.1 New runbook: `docs/runbooks/always-on-daemon.md`

Sections:
1. **The mental model** — sleep ≠ shutdown ≠ crash; what survives each
2. **Install per platform** — exact commands for Linux/macOS/Windows
3. **Verify it's running** — `oc service status` + per-platform native commands
4. **Headless servers** — `enable-linger` for Linux VPS; macOS LaunchDaemons recipe; Windows server-edition note
5. **Credentials persistence** — what's in `~/.opencomputer/<profile>/`; backup recommendations
6. **The "magic Telegram message" reconnect explanation** — boot → systemd/launchd loads → gateway loads config → channel adapters reconnect → first heartbeat
7. **Troubleshooting** — log locations, common errors, `oc service doctor`
8. **Uninstall** — clean removal, where else state lives

### 11.2 README update

Add a one-liner under Quick Start:
> `oc setup --install-daemon` — runs the setup wizard and registers OpenComputer as an always-on system service.

### 11.3 CLAUDE.md update

Add a new row to §4 ("What's been built") at the bottom of the phase table:

```
| Always-on daemon (cross-platform) | PR #?? | Cross-platform `oc service install/uninstall/status/start/stop/logs/doctor` (Linux systemd + macOS launchd + Windows Task Scheduler), `--install-daemon` flags on `oc setup` and `oc gateway`, wizard consolidation, runbook |
```

Also append a paragraph to §7 ("Non-obvious gotchas"):

```
N. Two launchd modules co-exist with deliberately distinct purposes:
   • opencomputer/service/launchd.py — daily profile-analyze cron (StartCalendarInterval)
   • opencomputer/service/_macos_launchd.py — always-on gateway plist (KeepAlive, RunAtLoad)
   The leading underscore marks the new module as backend-internal, called via the
   service.factory.get_backend() Protocol dispatch, not directly.
```

## 12. Open questions / risks

- **Risk: macOS Sequoia / Sonoma launchctl behavior shifts.** `launchctl bootstrap` semantics changed across macOS versions. Mitigation: integration test on macos-latest CI runner; pin minimum macOS version in `pyproject.toml` if needed.
- **Risk: Windows Defender flags `schtasks /create` as suspicious.** Some endpoint protection software heuristically blocks new scheduled tasks. Mitigation: document the SmartScreen warning in the runbook; user can manually create the task using `taskschd.msc` and the rendered XML.
- **Risk: `--install-daemon` flag during onboarding could fail mid-wizard.** If `service install` fails after wizard config is written, user has a half-state. Mitigation: `--install-daemon` is best-effort; on failure, wizard completes successfully and prints manual install instructions.
- **Open: should we register as `User=...` in systemd to allow service-account installs?** Deferred to follow-up.
- **Open: do we want a `--scope=system` flag in v2?** Documented as non-goal here; punt.

## 13. Acceptance criteria

A reviewer should be able to verify completion by:

1. **Linux (CI runner):** `oc service install --profile test`, then `oc service status` shows `running=True`, then `oc service uninstall`. All commands succeed; full pytest suite green.
2. **macOS (local manual test):** same sequence; `launchctl print gui/<uid>/com.opencomputer.gateway` shows the loaded service.
3. **Windows (local manual test):** same sequence; `schtasks /query /tn OpenComputerGateway /v` shows the registered task.
4. **Wizard:** `oc setup --install-daemon` on each platform completes without prompts for the service section and ends with the service installed.
5. **`oc service doctor`** reports green checks across all 8 diagnostic items on a freshly-installed system.
6. **Backward compat:** every test in `tests/test_service_install.py`, `tests/test_launchd_install.py`, `tests/test_launchd_plist.py`, `tests/test_cli_setup_section_launchd_service.py` (renamed), `tests/test_logging_journald.py` passes unchanged or with only the test-rename diff.
7. **Runbook** at `docs/runbooks/always-on-daemon.md` exists and is linked from README.
