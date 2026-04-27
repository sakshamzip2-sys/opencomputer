# Ambient Foreground Sensor — Phase 1 Design

**Date:** 2026-04-27
**Status:** Design (ready for plan)
**Branch:** `feat/ambient-foreground-sensor`
**Worktree:** `/tmp/oc-ambient/`
**Supersedes:** 2026-04-27-ambient-awareness-design.md (multi-phase document — too broad, replaced by this scoped Phase-1 spec)

---

## 1. Goal (one sentence)

Add a cross-platform, opt-in foreground-app sensor that publishes hashed `ForegroundAppEvent`s to OpenComputer's existing F2 typed event bus — closing the only ambient-awareness gap not already covered by V2.B/V2.C/cron/idle-detection while preserving the project's local-first, privacy-respecting posture for any user on any platform.

## 2. Why this and only this

After running every ambient-awareness idea through the "good enough already?" test against OpenComputer's existing infrastructure, exactly one passes:

- File-activity push events → covered by V2.B Spotlight rescans on idle.
- Screen capture (continuous) → on-demand `screenshot` tool already covers all current use cases; high cost/privacy/value asymmetry.
- Audio capture → niche; existing `voice/stt.py` covers user-supplied files.
- Proactive reactor → premature without sensor-driven motifs to react on.

Foreground polling is the gap. The persona classifier reads foreground app **once per session** at conversation start (`opencomputer/awareness/personas/classifier.py:33`). Long Telegram sessions, gateway-mode all-day usage, and mid-session context switches are invisible. A continuous sensor publishing to the F2 bus lets V2.B/V2.C build proper time-binned activity histograms — not "user is in TradingView right now" but "user has been in trading mode for 70% of the last 4 hours."

## 3. Framework-lens defaults (non-negotiable)

These are contracts, not preferences:

- **Default OFF.** Random user pip-installing OpenComputer never gets a surprise daemon. Opt in via `oc ambient on`.
- **Cross-platform first-class.** macOS, Linux X11, Windows all ship together. Wayland gracefully reports `unsupported` and stays quiet.
- **Local-only.** No data leaves the machine. No cloud LLM, no telemetry, no analytics. Hard contract baked into tests.
- **Hashed titles.** Window titles are SHA-256'd before publish. Raw titles never enter the bus, audit log, or storage.
- **Sensitive-app filter.** Banking, password-manager, healthcare, private-browsing apps are filtered to `<filtered>` before publish. Default regex list ships; user can append.
- **One-shot pause.** `oc ambient off` and the daemon stops within one tick (≤10s).
- **Audit-logged.** Every publish goes through F1 (`ambient.foreground.observe` capability) and lands in the existing tamper-evident audit chain.

## 4. Architecture

### 4.1 Module shape (3 files, ~600 LOC)

```
extensions/ambient-sensors/
├── plugin.json              # manifest
├── plugin.py                # registration + start daemon if state.enabled
├── foreground.py            # cross-platform detector (3 OS paths)
├── daemon.py                # asyncio poll loop
├── sensitive_apps.py        # default regex list + user override
├── pause_state.py           # state.json read/write helpers
└── README.md                # what / what not / how to disable
```

### 4.2 New SDK type — `ForegroundAppEvent`

Add to `plugin_sdk/ingestion.py`:

```python
@dataclass(frozen=True, slots=True)
class ForegroundAppEvent(SignalEvent):
    """Foreground app or window-title change observed by ambient-sensors plugin.

    Privacy: ``window_title_hash`` is SHA-256 of the title — raw title NEVER
    leaves the sensor. Sensitive-app filter replaces ``app_name`` with
    ``"<filtered>"`` and ``window_title_hash`` with empty string when the
    deny-list matches; ``is_sensitive=True`` records that filtering happened.
    """

    event_type: str = "foreground_app"
    app_name: str = ""              # e.g. "Code", "Safari" — or "<filtered>"
    window_title_hash: str = ""     # 64-char hex SHA-256 — empty if filtered
    bundle_id: str = ""             # macOS only; "" elsewhere
    is_sensitive: bool = False      # True iff filtered
    platform: str = ""              # "darwin" / "linux" / "win32"
```

Single new event type. No new infrastructure beyond the existing pub/sub.

### 4.3 Cross-platform foreground detection

`foreground.py` exports a single function:

```python
@dataclass(frozen=True, slots=True)
class ForegroundSnapshot:
    app_name: str
    window_title: str    # raw — hashed by caller before publish
    bundle_id: str       # "" except on macOS
    platform: str        # "darwin" / "linux" / "win32" / "wayland"


def detect_foreground() -> ForegroundSnapshot | None:
    """Return current foreground snapshot, or None if unavailable."""
```

Per-platform implementation:
- **macOS** — single osascript invocation pulling all three values atomically. Reuses the same approach as existing `_foreground.py:8`.
- **Linux X11** — `xdotool getactivewindow getwindowname` for title; `xdotool getactivewindow getwindowclassname` for app name. If `xdotool` isn't on PATH, try `wmctrl -l` as fallback.
- **Linux Wayland** — return `None` and log once at INFO that Wayland-compositor-specific protocols aren't supported in v1; document Sway/`swaymsg` follow-up path.
- **Windows** — `pywin32`'s `win32gui.GetForegroundWindow()` + `GetWindowText()` + `psutil.Process(pid).name()` for app.

Each platform returns `None` on failure rather than raising — the daemon logs at DEBUG and tries again next tick.

### 4.4 Daemon

`daemon.py::ForegroundSensorDaemon`:

- Single asyncio task; tick interval **10 s** (configurable via `<profile_home>/ambient/config.yaml::tick_seconds`, default 10).
- Lifecycle: starts when `plugin.py::register()` reads `state.enabled=True`; stops when state flips to disabled.
- **Pause-aware**: each tick reads `<profile_home>/ambient/state.json`. If `paused_until` is set and in the future, skip + emit `AmbientSensorPauseEvent` once per pause window.
- **Dedup**: only publish when `(app_name, window_title_hash, bundle_id)` differs from the last published tuple. Eliminates ~99% of bus traffic.
- **Min interval guard**: never publish more than once per 2 s even if foreground changes faster (prevents tiling-WM auto-focus storms).
- **Sensitive filter**: snapshot → `sensitive_apps.is_sensitive(snapshot)` → if True, replace per §4.2 contract.
- **Heartbeat**: write `<profile_home>/ambient/heartbeat` (timestamp file) each tick — doctor uses this to detect stuck daemons.
- **Standalone mode**: `oc ambient daemon` runs the same daemon outside gateway, for users not running the full gateway.

### 4.5 Sensitive-app filter (`sensitive_apps.py`)

Sensible defaults shipped in code; user override at `<profile_home>/ambient/sensitive_apps.txt` (one regex per line, comments with `#`):

```python
_DEFAULT_PATTERNS: tuple[str, ...] = (
    # Password managers
    r"(?i)1Password",
    r"(?i)Bitwarden",
    r"(?i)KeePass",
    r"(?i)Dashlane",
    r"(?i)LastPass",
    # Banking — generic patterns work cross-region
    r"(?i)\bbank\b",
    r"(?i)Chase",
    r"(?i)HDFC",
    r"(?i)ICICI",
    r"(?i)Robinhood",
    r"(?i)Coinbase",
    r"(?i)Zerodha",
    r"(?i)Groww",
    # Healthcare
    r"(?i)MyChart",
    r"(?i)Teladoc",
    # Private browsing
    r"(?i)Private Browsing",
    r"(?i)Incognito",
    r"(?i)Tor Browser",
    # Secure messaging
    r"(?i)Signal",
    r"(?i)ProtonMail",
)
```

Match logic: a snapshot is sensitive if `app_name` OR `window_title` matches ANY pattern. Match runs against raw values (which the sensor sees) but those raw values never leave the sensor — only the boolean result and the filtered output do.

### 4.6 Pause/resume state file

`<profile_home>/ambient/state.json`:

```json
{
  "enabled": true,
  "paused_until": null,
  "sensors": ["foreground"]
}
```

CLI subcommand group `oc ambient`:

| Command | Effect |
|---|---|
| `oc ambient on` | Set `enabled=true`. Start daemon if gateway running. |
| `oc ambient off` | Set `enabled=false`. Daemon stops within one tick. |
| `oc ambient pause [--duration 1h]` | Set `paused_until = time.time() + 3600`. Daemon emits pause event + skips ticks until expired. |
| `oc ambient resume` | Clear `paused_until`. |
| `oc ambient status` | Show enabled, paused, last heartbeat, AGGREGATE counts only (never specific apps). |
| `oc ambient daemon` | Run daemon standalone (outside gateway). |

### 4.7 F1 capability claim

```python
CapabilityClaim(
    capability_id="ambient.foreground.observe",
    tier_required=ConsentTier.IMPLICIT,
    human_description="Observe foreground app + hashed window title; sensitive apps are filtered before publish; data stays local.",
    data_scope="local",
)
```

Justification for IMPLICIT: app names + hashed titles are low-risk (titles can't be inverted from the hash by anyone without the original); sensitive apps are filtered; user explicitly opted in via `oc ambient on`. This matches the IMPLICIT tier of similar `introspection.*` capabilities.

### 4.8 Doctor checks

Add to `opencomputer/doctor.py`:

1. `_check_ambient_state` — read `state.json`, report enabled/disabled, last heartbeat age. WARNING if enabled but heartbeat stale (>60 s).
2. `_check_ambient_foreground_capable` — platform-specific:
   - macOS: dry-run osascript probe; warn if Accessibility permission missing.
   - Linux: check `xdotool` or `wmctrl` on PATH; warn if Wayland detected (`$WAYLAND_DISPLAY` set).
   - Windows: import `win32gui`; warn if not available.

Both at WARNING level (never ERROR) — the sensor is opt-in; missing permissions don't break anything else.

### 4.9 Tests (TDD per task)

Tests live at `tests/test_ambient_*.py`:
- `test_ambient_foreground_event.py` — SDK type contract.
- `test_ambient_foreground_detector.py` — platform forks, mocked OS calls.
- `test_ambient_sensitive_filter.py` — regex matching, default + user-override.
- `test_ambient_daemon_dedup.py` — dedup logic, min-interval guard.
- `test_ambient_pause_state.py` — state file read/write, pause-until expiry.
- `test_ambient_cli.py` — Typer CLI smoke tests for on/off/pause/resume/status.
- `test_ambient_capability_claim.py` — namespace + IMPLICIT tier contract.
- `test_ambient_no_cloud_egress.py` — AST scan asserting daemon code never imports `httpx`/`requests`/etc. Local-only contract.
- `test_ambient_doctor_checks.py` — both new doctor checks, mocked platform.

### 4.10 What the sensor will NEVER do (hard contract, baked into tests)

- ❌ Send any data to a network destination.
- ❌ Capture screen pixels, OCR'd text, or audio.
- ❌ Publish raw window titles (only SHA-256 hashes).
- ❌ Auto-take a user-visible action.
- ❌ Run when paused or disabled.
- ❌ Run when sensitive-app filter matches (it filters the data instead).
- ❌ Train any model on collected data.

The `test_ambient_no_cloud_egress.py` test enforces #1 by AST-scanning the plugin's source for HTTP-client imports.

## 5. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Wayland is the default on Ubuntu 22.04+ — Linux users can't use the sensor | v1 ships X11-only; doctor warns clearly; Sway/`swaymsg` Wayland support tracked as follow-up |
| macOS Accessibility prompt is jarring on first run | Doctor preflight + setup-wizard step explain it before user hits the OS prompt organically |
| Banking-app titles leak through if the regex misses one | User can append to `<profile_home>/ambient/sensitive_apps.txt`; multiple defense layers (app name + title pattern); raw titles never leave the sensor anyway |
| Daemon crashes silently | Heartbeat file + doctor check; daemon logs to `<profile_home>/ambient/daemon.log` |
| Bus traffic explosion if dedup breaks | Bounded deque (existing F2 protection); 2-second min-interval guard; dedup unit-tested |
| User pastes `oc ambient status` output → leaks activity | Status command shows aggregate counts only, never specific app names |
| Audit log grows unboundedly with frequent publishes | Existing F1 rotation; verify capacity in audit log volume test |
| Cross-platform code rot | All 3 platforms tested; CI matrix from PR #181 covers them |
| User runs OC without gateway → daemon never starts | `oc ambient daemon` standalone mode |
| Title hash is not actually private (small set of possible titles) | Hashes used only for dedup, never exfiltrated; documentation makes this clear; user can disable hashing entirely if desired |

## 6. Out of scope (deferred)

- Wayland support — v2 follow-up after v1 dogfood
- File-activity sensor (Phase 2)
- Screen capture sensor (Phase 3, may never ship)
- Audio sensor (Phase 4, may never ship)
- Proactive reactor (Phase 5, requires Phase 1 + Phase 2 motif data first)
- Per-time-range F1 consent grants (separate F1 work)
- Per-turn persona reclassification (V2.D plan, not blocking on this)

## 7. Self-review

**Spec coverage**: every framework-lens contract from §3 has a concrete implementation in §4 + §5.

**Internal consistency**: `ForegroundAppEvent` shape matches the F2 metadata-only convention. Sensitive filter applies before publish. Pause state is single-source-of-truth (CLI writes, daemon reads).

**Scope check**: 10 implementation tasks. Realistic at ~700-900 LOC + ~400 LOC tests = ~1,200 LOC total. One-day PR shape, not multi-week.

**Ambiguity check**: "what happens when foreground app changes WHILE the daemon is paused?" — daemon doesn't even read `detect_foreground()` while paused. When unpaused, next tick captures whatever's foreground at THAT moment. No catch-up replay (correct — replay would defeat the pause's privacy intent).

**Framework-lens validation**: a random user installing OC via pip and never running `oc ambient on` sees ZERO behavior change. The sensor exists only when explicitly enabled. Pass.

---

*Spec ready for the writing-plans skill.*
