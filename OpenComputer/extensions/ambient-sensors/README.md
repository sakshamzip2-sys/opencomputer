# Ambient Sensors (foreground-app)

Cross-platform ambient awareness for OpenComputer. **Default OFF — opt-in only.**

## What this does

When enabled, a small daemon polls the foreground application every 10
seconds and publishes a `ForegroundAppEvent` to OpenComputer's F2 typed
event bus. The persona classifier and motif extractor consume those
events to build a richer picture of how you use your machine across apps
— without the agent ever seeing the raw window titles.

## What this does NOT do

| Thing | Status |
|---|---|
| Capture screen content (pixels, OCR, anything visual) | ❌ |
| Record audio | ❌ |
| Send any data to a network destination | ❌ |
| Send raw window titles anywhere | ❌ (only SHA-256 hashes) |
| Auto-take user-visible actions | ❌ |
| Run when paused or disabled | ❌ |
| Run when sensitive-app filter matches | ❌ (filtered to `<filtered>`) |
| Train any model on collected data | ❌ |

The "no network" rule is enforced by `tests/test_ambient_no_cloud_egress.py`
— a CI guard that AST-scans this directory for HTTP-client imports.
Adding networking here is a contract break, not just a code change; it
requires updating the deny-list, this README, and the CHANGELOG.

## Privacy contract

| Field captured | Storage | Where it goes |
|---|---|---|
| App name (e.g. "Code") | In-memory only | F2 bus (consumed locally) |
| Window title | SHA-256 hashed before publish | Hash only — never the title |
| Bundle ID (macOS) | In-memory only | F2 bus |
| Sensitive-app match | Boolean only | F2 bus |

When the sensitive-app filter matches, the published event has
`app_name = "<filtered>"`, `window_title_hash = ""`, and
`is_sensitive = True`. The raw values never leave the sensor process.

Note on title hashes: SHA-256 of "Inbox - Gmail" is the same hash for
everyone, so the hash isn't a secret — it's a per-process dedup token.
Hashes are useful only for the local F2 bus subscribers.

## Sensitive-app filter

Default deny-list (regex) covers password managers, banking apps,
healthcare apps, private-browsing tabs, and secure-messaging apps. To
extend the list, create:

```
<profile_home>/ambient/sensitive_apps.txt
```

Format: one regex per line; lines starting with `#` are comments;
blank lines ignored. Example:

```
# my company's internal tools
(?i)AcmeFinancialPortal
(?i)InternalHRSystem
```

The filter matches against either the app name OR the window title.
Malformed regexes are silently skipped (not raised) so a typo in the
override file can't break the daemon.

## How to use

```bash
# Enable (opt in)
opencomputer ambient on

# Pause for an hour (e.g. during a sensitive call)
opencomputer ambient pause --duration 1h

# Resume
opencomputer ambient resume

# Disable completely (clears state)
opencomputer ambient off

# See state — aggregate counts only, never specific apps
opencomputer ambient status

# Run the daemon outside the gateway (e.g. for testing)
opencomputer ambient daemon
```

## Platform support

| Platform | Status | Mechanism |
|---|---|---|
| macOS | Supported | osascript via System Events (Accessibility permission required) |
| Linux X11 | Supported | xdotool primary; wmctrl fallback |
| Linux Wayland | Unsupported in v1 | Daemon stays silent; doctor reports unsupported |
| Windows | Supported | pywin32 (`win32gui` + `psutil`) |

`opencomputer doctor` reports per-platform readiness as part of its
checks. If your platform isn't yet supported, the daemon refuses to
emit data rather than emitting bogus values.

## Troubleshooting

**macOS "not authorized" error on first run.**
System Settings → Privacy & Security → Accessibility → enable Terminal
(or whichever app launches the gateway). Re-run `opencomputer ambient
status` to confirm.

**Linux: daemon emits nothing.**
Install `xdotool` or `wmctrl`:
```
sudo apt install xdotool
```
Or, if you're on Wayland, the foreground sensor is unsupported in v1.
The daemon stays running but quiet; `opencomputer doctor` flags this.

**Daemon not starting.**
Three things to check:
1. `opencomputer ambient on` was run (confirm with `status`).
2. The gateway daemon is up (`opencomputer gateway`) OR you're running
   `opencomputer ambient daemon` standalone.
3. `opencomputer doctor` — look for `ambient sensor` lines.

**Heartbeat shown as stale.**
The daemon is enabled but not actually running, or the daemon is stuck.
Restart the gateway, or `opencomputer ambient off` then back `on`.

## Disabling completely

`opencomputer ambient off` flips the `enabled` flag. The daemon stops
within one tick (≤10s).

If you don't trust the flag (e.g. moving to a different machine):
```
rm -rf <profile_home>/ambient/
```
The daemon defaults to disabled when `state.json` is missing or
unreadable.

## Future phases (not in v1)

This is Phase 1. The framework can grow other ambient sensors via
the same plugin pattern. Each future phase ships its own opt-in flag
+ privacy contract:

- **Phase 2**: file-activity push events (watchdog/fsevents/inotify)
- **Phase 3**: screen capture + on-device VLM (MLX-VLM / ONNX) — needs
  explicit go-ahead; high cost/risk
- **Phase 4**: audio + on-device Whisper (whisper.cpp / mlx-whisper) —
  needs explicit go-ahead; highest privacy risk
- **Phase 5**: proactive nudge reactor — needs Phase 1+2 motif data first

Each phase is a separate PR and a separate opt-in.
