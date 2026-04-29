# Run OpenComputer as a Linux systemd user service

This guide is the generic version of the [Raspberry Pi
guide](./raspberry-pi.md) — same install pattern, but framed for
Ubuntu/Debian/Fedora/Arch on any architecture.

## Prereqs

- Linux with systemd (Ubuntu 20+, Debian 11+, Fedora 35+, Arch — basically
  any modern distro).
- Python 3.12+ (`python3 --version`).
- One of: pip + virtualenv, or Docker.
- API keys for whatever providers + channels you intend to use.

## pip install

```bash
python3 -m venv ~/.venv-oc
source ~/.venv-oc/bin/activate
pip install opencomputer

# Profile + creds
mkdir -p ~/.opencomputer/default
$EDITOR ~/.opencomputer/default/.env   # add ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, etc.

# Install the service
opencomputer service install --extra-args 'gateway'
loginctl enable-linger $USER
systemctl --user enable --now opencomputer
```

## Verify

```bash
opencomputer service status         # → active
journalctl --user -u opencomputer -f
```

## Uninstall

```bash
opencomputer service uninstall
```

## Service file location

`~/.config/systemd/user/opencomputer.service` — feel free to edit
(e.g., to change `--profile default` to `--profile work`); reload with
`systemctl --user daemon-reload && systemctl --user restart opencomputer`.

## Why a USER unit, not a system unit?

The agent runs with your home directory's profile + your API keys. A
system unit would force you to either copy creds into `/etc/` or run as
root, both worse. User units `enable-linger` to survive logout, which
matches what you want for an always-on agent.
