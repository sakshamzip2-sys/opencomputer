# Always-On Daemon Runbook

OpenComputer can run as a persistent OS-level service that the OS keeps alive
across crashes, terminal sessions, and reboots. This is what makes it possible
to power on a laptop after weeks of being off and immediately receive a
"back online" Telegram message — the agent fully reconstitutes from disk-only
state in seconds.

## The mental model

| State of laptop | Daemon status |
|---|---|
| Powered on, awake | running |
| Sleep | frozen — instantly resumes when lid opens |
| Shutdown | not running, but auto-starts at next boot |

The "always running" illusion comes from short gaps and instant reconnects.

## Install per platform

### One command (all platforms)

```bash
oc setup --install-daemon          # first-run wizard + service install
# OR
oc service install                  # if already onboarded
# OR
oc gateway --install-daemon         # one-shot install + exit (no foreground gateway)
```

To install for a non-default profile:

```bash
oc gateway --install-daemon --daemon-profile work
oc setup --install-daemon --daemon-profile work
```

### Linux (systemd-user)

Writes `~/.config/systemd/user/opencomputer.service`. Runs:

```
oc --headless --profile <p> gateway
```

with `Restart=always` and `RestartSec=5`.

**Headless servers (no GUI session):** enable lingering so the service runs
across SSH disconnects and at boot before any login:

```bash
sudo loginctl enable-linger $USER
```

`oc service install` prints this hint when it detects linger is not enabled.

### macOS (launchd)

Writes `~/Library/LaunchAgents/com.opencomputer.gateway.plist`. Bootstrapped
into the user's GUI domain via `launchctl bootstrap gui/<uid>` (the modern API,
not the deprecated `launchctl load`). `KeepAlive=true` + `RunAtLoad=true` keep
the service alive across crashes and login.

### Windows (Task Scheduler)

Registers a user-scope task `OpenComputerGateway` triggered on logon, with
`RestartOnFailure` configured. No admin elevation needed. The XML is rendered
to `%USERPROFILE%\.opencomputer\opencomputer-task.xml` and registered via
`schtasks /create /xml`.

## Verify it's running

```bash
oc service status
```

A healthy install shows: `enabled=True, running=True, pid=<PID>`.

Native commands per platform if you want to dig deeper:

```bash
# Linux
systemctl --user status opencomputer.service
journalctl --user -u opencomputer.service -f

# macOS
launchctl print gui/$(id -u)/com.opencomputer.gateway

# Windows
schtasks /query /tn OpenComputerGateway /v /fo list
```

## Diagnostic: `oc service doctor`

Runs five checks:

- `executable_resolvable` — `oc`/`opencomputer` found on PATH or in fallbacks
- `config_file_present` — service config file exists at the expected path
- `service_enabled` — OS reports it as enabled
- `service_running` — currently running
- `recent_crashes` — last 5 log lines free of `Traceback`/`panic`/`FATAL`

`oc doctor` (the broader health check) also includes a `service` row that wraps
the same factory.

## Tail logs

```bash
oc service logs                     # last 100 lines
oc service logs -n 500              # last 500
oc service logs --follow            # stream new lines (like `tail -f`)
```

Per-platform sources:
- Linux: `journalctl --user -u opencomputer.service -f`
- macOS: tail `~/.opencomputer/<profile>/logs/gateway.{stdout,stderr}.log`
- Windows: tail `%USERPROFILE%\.opencomputer\<profile>\logs\gateway.{stdout,stderr}.log`

## Credentials persistence — what survives shutdown

Per-profile credentials live in `~/.opencomputer/<profile>/`:

```
~/.opencomputer/<profile>/
├── config.yaml                # profile config
├── credentials/               # OS keyring fallback file storage
└── logs/                      # gateway stdout/stderr (macOS, Windows)
```

These files are what survive shutdown and let the agent reconnect to Telegram /
Discord without re-pairing. The two pieces of state needed for the
"power-on-after-months → instant Telegram message" trick:

1. The service-manager config file (launchd plist, systemd unit, or Task XML)
   — survives because it's a file on disk
2. The per-profile credentials — same reason

Combine those with the LLM provider's API key (env var or `~/.opencomputer/<p>/.env`),
and the agent fully reconstitutes the moment the OS boots.

## Boot sequence (the "magic Telegram message")

```
1. OS finishes booting / user logs in (~10–30s)
2. OS reads service config (launchd plist / systemd unit / scheduled task)
3. OS executes:  oc --headless --profile <p> gateway
4. oc loads ~/.opencomputer/<profile>/config.yaml + credentials
5. Gateway loads channel adapters → each adapter reconnects
   (bot identity is server-side persistent on Telegram/Discord/Matrix)
6. Adapters announce "online"; heartbeat scheduler ticks
7. (Optional) First heartbeat sends a "back online" message — the magic ping
```

## Sleep vs shutdown

- **Sleep** — daemon is *frozen*, not running. CPU is off. When you open the
  lid, the OS un-freezes everything and the daemon resumes the exact
  instruction it was on. No reconnect needed.
- **Shutdown** — daemon is *not running at all*. It only comes back at next
  boot. The launchd/systemd/Task config is what makes "next boot" automatic.

## Uninstall

```bash
oc service uninstall
```

Removes the service config file and the OS-side registration. Does **not**
remove profile data in `~/.opencomputer/`.

## Troubleshooting

| Symptom | Try |
|---|---|
| `oc service status` reports `not enabled` after install | re-run `oc service install` (idempotent) |
| Linux: service stops when SSH disconnects | `sudo loginctl enable-linger $USER` |
| macOS: `launchctl bootstrap` returned 5 | older plist still loaded — `launchctl bootout gui/$(id -u)/com.opencomputer.gateway`, then re-install |
| Windows Defender SmartScreen flags `schtasks /create` | install via `taskschd.msc` GUI: import the rendered XML at `%USERPROFILE%\.opencomputer\opencomputer-task.xml` |
| First boot after install — no Telegram message | check `oc service logs -n 100` for adapter connection errors |
| `oc service doctor` reports `executable_resolvable: WARN: could not find oc` | `pipx ensurepath` (then re-login), or set `OC_EXECUTABLE=/path/to/oc` env var |

## Alternative deployment: Docker

If you prefer container-native deployment over OS-service supervision, use
`docker run --restart=always opencomputer/gateway:latest` — but be aware that
local laptop use is better served by the daemon flow above (no Docker Desktop
tax on macOS/Windows).
