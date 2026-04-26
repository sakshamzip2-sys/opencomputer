# OpenComputer gateway — macOS LaunchAgent

Auto-start `opencomputer gateway` at login + restart on crash, so your
Telegram / Discord / Slack / etc. channels keep working without you
remembering to run the daemon.

## Install

```bash
bash scripts/launchd/install.sh
```

That's it. The installer:

1. Resolves the absolute path to `opencomputer` on your `PATH`
2. Substitutes it (plus `$HOME`) into the plist template
3. Writes `~/Library/LaunchAgents/com.opencomputer.gateway.plist`
4. Runs `launchctl unload` (no-op if not loaded) then `launchctl load`
5. Verifies the job is listed

Pass `--dry-run` first if you want to see what gets written.

## Verify

```bash
launchctl list | grep opencomputer
# Expected: PID  STATUS  com.opencomputer.gateway
tail -f ~/.opencomputer/logs/gateway.launchd.out.log
```

## Uninstall

```bash
bash scripts/launchd/uninstall.sh
```

Idempotent. Logs are preserved (delete manually if you want them gone).

## Behaviour notes

- **`KeepAlive=true` + `ThrottleInterval=60`** → restart on any exit, but no faster than once per 60s. A permanent failure (bad token, etc.) won't busy-loop.
- **Sparse PATH** — LaunchAgents inherit `/usr/bin:/bin:/usr/sbin:/sbin`. The plist template adds `/usr/local/bin:/opt/homebrew/bin:/opt/anaconda3/bin` so the gateway can find anything it shells out to. If you install opencomputer in a venv, edit the plist's `EnvironmentVariables.PATH` to include your venv `bin/`.
- **Env vars** — your shell dotfiles are NOT sourced by LaunchAgents. Anything the gateway needs (`ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, etc.) must live in `~/.opencomputer/.env` which `opencomputer` loads via the `security/env_loader` module.
- **Single-instance** — PR #149's scoped lock prevents two `opencomputer gateway` processes from polling the same Telegram bot. Starting the daemon manually with this LaunchAgent loaded will fail-fast with the holding PID.

## Linux / VPS

LaunchAgents are macOS-only. For headless deployments, use `docker compose up -d` against the bundled `docker-compose.yml` — that's the one-liner for a 24/7 gateway. Systemd units land in a future PR.
