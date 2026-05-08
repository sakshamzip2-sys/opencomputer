#!/usr/bin/env bash
# Hermes parity (2026-05-08): one-command Open-WebUI bootstrap.
#
# What it does:
#   1. Generates a random API_SERVER_TOKEN (24 hex bytes = 48 chars).
#   2. Sets API_SERVER_ENABLED=true.
#   3. Configures port (default 8642).
#   4. Prints the docker-run command for Open-WebUI.
#
# Idempotency: if API_SERVER_TOKEN is already set, prompts before overwrite.
# Exit codes: 0 success, 1 user aborted, 2 prerequisite missing.
#
# Usage:
#   bash scripts/setup_open_webui.sh           # interactive
#   bash scripts/setup_open_webui.sh --force   # non-interactive overwrite
#   bash scripts/setup_open_webui.sh --port 9000
#
set -euo pipefail

PORT=8642
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --port) PORT="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Prereq: oc CLI must be installed.
if ! command -v oc >/dev/null 2>&1; then
    echo "error: 'oc' CLI not found in PATH" >&2
    echo "  install via: pip install opencomputer  (or pipx install opencomputer)" >&2
    exit 2
fi

# Prereq: openssl for token generation.
if ! command -v openssl >/dev/null 2>&1; then
    echo "error: 'openssl' not found in PATH" >&2
    exit 2
fi

# Idempotency check.
EXISTING=""
if EXISTING="$(oc config get API_SERVER_TOKEN 2>/dev/null || true)" && [[ -n "${EXISTING// /}" && "${EXISTING}" != "null" ]]; then
    if [[ "$FORCE" -eq 0 ]]; then
        echo "warning: API_SERVER_TOKEN is already set."
        echo -n "Overwrite with a new random token? [y/N] "
        read -r yn
        case "$yn" in
            [Yy]*) ;;
            *) echo "aborted — keeping existing token."; exit 1 ;;
        esac
    fi
fi

TOKEN="$(openssl rand -hex 24)"
oc config set API_SERVER_ENABLED true
oc config set API_SERVER_TOKEN "$TOKEN"
oc config set API_SERVER_PORT "$PORT"

cat <<EOF

✓ Open-WebUI bootstrap complete.

Token (save this — Open-WebUI needs it as OPENAI_API_KEY):
  $TOKEN

Next steps:
  1. Start the gateway (foreground or as a service):
       oc gateway
       # or:    oc gateway install && oc gateway start

  2. Start Open-WebUI in Docker:

       docker run -d -p 3000:8080 \\
         -e OPENAI_API_BASE_URL=http://host.docker.internal:$PORT/v1 \\
         -e OPENAI_API_KEY=$TOKEN \\
         -e ENABLE_OLLAMA_API=false \\
         --add-host=host.docker.internal:host-gateway \\
         -v open-webui:/app/backend/data \\
         --name open-webui --restart always \\
         ghcr.io/open-webui/open-webui:main

  3. Open http://localhost:3000

For multi-profile setups, run this script per profile with --port; each
profile advertises its name as a separate "model" in Open-WebUI.

EOF
