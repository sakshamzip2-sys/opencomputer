# Run OpenComputer on a Raspberry Pi (always-on)

This guide takes a fresh Raspberry Pi 4 or 5 from boot to "agent online,
listening on Telegram, surviving reboots" in under 10 minutes.

## What you need

- Pi 4 (4 GB+) or Pi 5 — 32-bit Pis aren't supported (Python wheels require 64-bit).
- Raspberry Pi OS 64-bit (Lite is fine — no desktop required).
- Network access (Ethernet or Wi-Fi configured).
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- Your Telegram numeric user ID (ask [@userinfobot](https://t.me/userinfobot)).

## Install — option A: pip (smaller image, slightly slower start)

```bash
# 1. Update + Python 3.12+
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git

# 2. Create a virtualenv outside the home root so systemd can find it
python3 -m venv ~/.venv-oc
source ~/.venv-oc/bin/activate
pip install --upgrade pip
pip install opencomputer

# 3. Optional — journald handler (pretty `journalctl` output)
sudo apt install -y python3-systemd

# 4. Provider creds (pick one)
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.bashrc
# or
echo 'export OPENAI_API_KEY=sk-...' >> ~/.bashrc
source ~/.bashrc

# 5. Telegram creds
mkdir -p ~/.opencomputer/default
cat > ~/.opencomputer/default/.env <<EOF
TELEGRAM_BOT_TOKEN=12345:abcdef-your-token
TELEGRAM_ALLOWED_USERS=123456789
EOF

# 6. Install + start the systemd user service
opencomputer service install --extra-args 'gateway'
loginctl enable-linger $USER     # so the service runs even when you're logged out
systemctl --user enable --now opencomputer

# 7. Watch it work
journalctl --user -u opencomputer -f
```

## Install — option B: Docker (faster, a bit more disk)

```bash
sudo apt update && sudo apt install -y docker.io
sudo usermod -aG docker $USER
# log out + back in for the group to take effect

mkdir -p ~/oc-data
docker run -d --name oc \
    --restart unless-stopped \
    -v ~/oc-data:/home/oc/.opencomputer \
    -e ANTHROPIC_API_KEY=sk-ant-... \
    -e TELEGRAM_BOT_TOKEN=... \
    -e TELEGRAM_ALLOWED_USERS=... \
    ghcr.io/sakshamzip2-sys/opencomputer:latest gateway

docker logs -f oc
```

## Verify

Send a message to your bot on Telegram. You should see:

1. The bot replies (model output).
2. `journalctl` shows the request + response in real time.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Failed to connect to user instance` from `systemctl --user` | Run `loginctl enable-linger $USER` and retry. |
| Bot doesn't reply | Check `TELEGRAM_ALLOWED_USERS` matches your numeric ID exactly. |
| `ImportError: systemd.journal` | Missed `apt install python3-systemd` in step 3 — non-fatal, journald handler stays unattached. |
| OOM kill on Pi 4 (4 GB) | Use a smaller model or set `OPENCOMPUTER_LOOP_BUDGET=4096` to cap context. |

## What the service does

- Runs `opencomputer --headless --profile default gateway` under your user.
- `gateway` is the long-running daemon that talks to channel adapters (Telegram, Discord, Slack — whichever plugins are enabled in your profile).
- Survives reboots (`enable-linger`), restarts on crash (`Restart=always`).
- Logs to journald, viewable with `journalctl --user -u opencomputer`.

## Updating

Pip install:

```bash
~/.venv-oc/bin/pip install --upgrade opencomputer
systemctl --user restart opencomputer
```

Docker:

```bash
docker pull ghcr.io/sakshamzip2-sys/opencomputer:latest
docker rm -f oc && # re-run the docker run command from above
```
