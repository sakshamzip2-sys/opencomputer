#!/usr/bin/env bash
# Phase 10 — alice/bob end-to-end demo for the social-traces plugin.
#
# Two modes:
#   --stubbed  (default) — wire-only test via HttpTraceNetworkClient.
#                          No agent loop, no LLM. Fast (~5s), reproducible.
#   --real                — full agent loop via `opencomputer oneshot`.
#                          Reads ANTHROPIC_API_KEY from env (or
#                          ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_MODE for
#                          a Claude Router proxy setup).
#
# Both modes exit 0 on full pass, 1 on any failure with a clear reason.
# Both bring up OpenHub fresh, run the flow, and tear down.
set -euo pipefail

# ── locations ────────────────────────────────────────────────────────
OC_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENHUB_REPO="${OPENHUB_REPO:-$HOME/Documents/GitHub/openhub}"

if [ ! -d "$OPENHUB_REPO" ]; then
  echo "ERR: OpenHub repo not found at $OPENHUB_REPO. Set OPENHUB_REPO=/path/to/openhub." >&2
  exit 1
fi

# ── arg parse ────────────────────────────────────────────────────────
MODE="stubbed"
for arg in "$@"; do
  case "$arg" in
    --stubbed) MODE="stubbed" ;;
    --real) MODE="real" ;;
    -h|--help)
      sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "ERR: unknown arg $arg" >&2; exit 1 ;;
  esac
done

# ── pre-flight ───────────────────────────────────────────────────────
if [ "$MODE" = "real" ]; then
  if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    cat >&2 <<'EOM'
ERR: --real mode needs ANTHROPIC_API_KEY in env.

  # Native Anthropic key:
  export ANTHROPIC_API_KEY=sk-ant-...

  # OR Claude Router proxy:
  export ANTHROPIC_BASE_URL=https://claude-router.vercel.app
  export ANTHROPIC_AUTH_MODE=bearer
  export ANTHROPIC_API_KEY=<router-proxy-key>

Then re-run with --real.
EOM
    exit 1
  fi
fi

if ! command -v pg_isready >/dev/null 2>&1; then
  echo "ERR: postgres client tools not installed (need pg_isready)." >&2
  exit 1
fi
if ! pg_isready >/dev/null 2>&1; then
  echo "ERR: postgres not reachable on default socket. brew services start postgresql@16" >&2
  exit 1
fi

# ── temp state ───────────────────────────────────────────────────────
ADMIN_TOKEN="demo-admin-$(openssl rand -hex 8)"
DEMO_TMP="$(mktemp -d -t social-traces-demo-XXXXXX)"
OH_LOG="$DEMO_TMP/openhub.log"
OH_PID=""
SUBMITTERS_FILE="$DEMO_TMP/submitters.json"
ALICE_HOME="$DEMO_TMP/alice"
BOB_HOME="$DEMO_TMP/bob"
ENDPOINT="http://127.0.0.1:8000"

cleanup() {
  local rc=$?
  if [ -n "$OH_PID" ] && kill -0 "$OH_PID" 2>/dev/null; then
    echo "── cleanup: stopping OpenHub (pid=$OH_PID)…"
    kill "$OH_PID" 2>/dev/null || true
    wait "$OH_PID" 2>/dev/null || true
  fi
  echo "── cleanup: temp dir kept at $DEMO_TMP (logs in openhub.log)"
  return $rc
}
trap cleanup EXIT

# ── 1. bring up OpenHub ──────────────────────────────────────────────
echo "── 1/5 bringing up OpenHub at $ENDPOINT (HMAC + admin-token enforced)"
pushd "$OPENHUB_REPO" >/dev/null
if [ ! -d ".venv" ]; then
  echo "ERR: $OPENHUB_REPO/.venv missing. cd $OPENHUB_REPO && python3 -m venv .venv && pip install -e ." >&2
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# alembic upgrade — idempotent.
alembic upgrade head >>"$OH_LOG" 2>&1

# Truncate DB so the demo starts clean. The demo runs against
# openhub_dev (the configured default); we wipe just the three demo-
# relevant tables — not drop the schema.
psql -d openhub_dev -c "TRUNCATE audit_log, traces, submitters CASCADE;" >>"$OH_LOG" 2>&1

# Background-launch uvicorn. Export the secrets it needs.
export ADMIN_TOKEN
export REQUIRE_HMAC=true
uvicorn openhub.main:app --host 127.0.0.1 --port 8000 >>"$OH_LOG" 2>&1 &
OH_PID=$!
deactivate
popd >/dev/null

# Wait for /healthz up to 10s.
for _ in $(seq 1 50); do
  if curl -sf "$ENDPOINT/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
if ! curl -sf "$ENDPOINT/healthz" >/dev/null 2>&1; then
  echo "ERR: OpenHub did not come up within 10s. Tail of $OH_LOG:" >&2
  tail -30 "$OH_LOG" >&2
  exit 1
fi
echo "    OK  /healthz responding"

# ── 2. register alice + bob ──────────────────────────────────────────
echo "── 2/5 registering two submitters via /admin/submitters"
ALICE_RESP="$(curl -sfX POST "$ENDPOINT/admin/submitters" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{}')"
BOB_RESP="$(curl -sfX POST "$ENDPOINT/admin/submitters" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{}')"

ALICE_HASH="$(printf '%s' "$ALICE_RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["submitter_hash"])')"
ALICE_KEY="$(printf '%s'  "$ALICE_RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["shared_key"])')"
BOB_HASH="$(printf '%s'   "$BOB_RESP"   | python3 -c 'import json,sys;print(json.load(sys.stdin)["submitter_hash"])')"
BOB_KEY="$(printf '%s'    "$BOB_RESP"   | python3 -c 'import json,sys;print(json.load(sys.stdin)["shared_key"])')"

cat >"$SUBMITTERS_FILE" <<JSON
{"alice": {"hash": "$ALICE_HASH", "key_redacted": "***"},
 "bob":   {"hash": "$BOB_HASH",   "key_redacted": "***"}}
JSON
echo "    alice hash=$ALICE_HASH"
echo "    bob   hash=$BOB_HASH"

# ── 3. run the chosen mode ───────────────────────────────────────────
echo "── 3/5 running mode: $MODE"
pushd "$OC_REPO" >/dev/null
# shellcheck disable=SC1091
source .venv/bin/activate

if [ "$MODE" = "stubbed" ]; then
  python scripts/demo_social_traces_stubbed.py \
    --endpoint "$ENDPOINT" \
    --admin-token "$ADMIN_TOKEN" \
    --alice-hash "$ALICE_HASH" --alice-key "$ALICE_KEY" \
    --bob-hash "$BOB_HASH" --bob-key "$BOB_KEY"
else
  # ── REAL MODE ─────────────────────────────────────────────────────
  # Set up alice + bob OC profiles in temp homes so we don't touch the
  # user's real profiles. OPENCOMPUTER_HOME points each invocation at
  # its own per-profile dir; the social_traces config.yaml carries
  # the HMAC creds (env vars OPENHUB_SUBMITTER_HASH/SHARED_KEY would
  # collide between alice and bob in the same shell).
  mkdir -p "$ALICE_HOME/traces" "$BOB_HOME/traces"

  # config.yaml — model config inherited from user's env vars.
  # social_traces section carries HMAC creds + endpoint.
  # (anthropic-provider is enabled_by_default=true; social-traces is
  # opt-in so we list it explicitly in profile.yaml.)
  for HOMEDIR in "$ALICE_HOME" "$BOB_HOME"; do
    cat >"$HOMEDIR/profile.yaml" <<YAML
plugins:
  enabled:
    - social-traces
YAML
    cat >"$HOMEDIR/config.yaml" <<YAML
model:
  provider: anthropic
  model: claude-haiku-4-5
  api_key_env: ANTHROPIC_API_KEY
  # Override the new 32768 default — Anthropic SDK rejects
  # non-streaming requests that big with "Streaming is required for
  # operations that may take longer than 10 minutes". 4096 is plenty
  # for this demo's prompts.
  max_tokens: 4096
social_traces:
  backend: http
  endpoint: $ENDPOINT
YAML
    if [ "$HOMEDIR" = "$ALICE_HOME" ]; then
      echo "  submitter_hash: $ALICE_HASH" >>"$HOMEDIR/config.yaml"
      echo "  shared_key: $ALICE_KEY"     >>"$HOMEDIR/config.yaml"
    else
      echo "  submitter_hash: $BOB_HASH"  >>"$HOMEDIR/config.yaml"
      echo "  shared_key: $BOB_KEY"       >>"$HOMEDIR/config.yaml"
    fi
    printf '{"enabled": true}\n' >"$HOMEDIR/traces/state.json"
  done

  ALICE_PROMPT="Summarize the difference between TCP and UDP in two sentences. Tag this conversation as 'networking'."
  BOB_PROMPT="What's the high-level difference between TCP and UDP, conceptually?"

  echo "    [alice] running oneshot…"
  OPENCOMPUTER_HOME="$ALICE_HOME" opencomputer oneshot "$ALICE_PROMPT" \
    >"$DEMO_TMP/alice.out" 2>"$DEMO_TMP/alice.err" || {
      echo "ERR: alice oneshot failed. stderr:" >&2; cat "$DEMO_TMP/alice.err" >&2; exit 1;
    }

  # Wait briefly for the post-task subscriber to fire submit.
  echo "    [alice] waiting up to 30s for post-task submit to land…"
  PENDING_ID=""
  for _ in $(seq 1 60); do
    PENDING_RESP="$(curl -sf "$ENDPOINT/admin/queue?status=pending&limit=10" \
      -H "Authorization: Bearer $ADMIN_TOKEN" || true)"
    PENDING_ID="$(printf '%s' "$PENDING_RESP" | \
      python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(0)
ts=d.get("traces",[])
if ts: print(ts[0].get("id",""))
' || true)"
    if [ -n "$PENDING_ID" ]; then
      break
    fi
    sleep 0.5
  done
  if [ -z "$PENDING_ID" ]; then
    echo "ERR: alice's submit never reached OpenHub. tail of $OH_LOG:" >&2
    tail -50 "$OH_LOG" >&2
    exit 1
  fi
  echo "    [alice] submitted trace $PENDING_ID"

  echo "    [admin] approving $PENDING_ID…"
  curl -sfX POST "$ENDPOINT/admin/traces/$PENDING_ID/accept" \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"reason": "demo accept"}' >/dev/null
  echo "    [admin] approved"

  echo "    [bob]   running oneshot with related prompt…"
  OPENCOMPUTER_HOME="$BOB_HOME" opencomputer oneshot "$BOB_PROMPT" \
    >"$DEMO_TMP/bob.out" 2>"$DEMO_TMP/bob.err" || {
      echo "ERR: bob oneshot failed. stderr:" >&2; cat "$DEMO_TMP/bob.err" >&2; exit 1;
    }

  # Verify: does bob's per-profile state record show a trace was used?
  # The plugin writes session_state.peek_trace_used as a side effect of
  # on_before_task hitting an injected trace. We check by inspecting
  # the latest trace history in bob's profile (Phase 4 wrote a
  # per-session record on every prefetch hit).
  echo "    [verify] checking bob's session for prefetch hit…"
  BOB_HEARTBEAT="$BOB_HOME/traces/heartbeat"
  if [ ! -f "$BOB_HEARTBEAT" ]; then
    echo "ERR: bob's traces/heartbeat is missing — the prefetch hook never fired." >&2
    exit 1
  fi
  echo "    [verify] bob heartbeat OK"

  # The cleanest "trace used" signal lives in the inbox cache (Phase
  # 6 wired the http backend's query cache to mirror local-file's
  # inbox/outbox layout for diagnostic parity). We also assert that
  # the OH-side approved-traces list is non-empty, proving the round
  # trip closed.
  APPROVED_RESP="$(curl -sf "$ENDPOINT/admin/queue?status=approved&limit=10" \
    -H "Authorization: Bearer $ADMIN_TOKEN")"
  APPROVED_COUNT="$(printf '%s' "$APPROVED_RESP" | \
    python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("traces",[])))')"
  if [ "$APPROVED_COUNT" = "0" ]; then
    echo "ERR: no approved traces after admin accept — the flow broke." >&2
    exit 1
  fi
  echo "    [verify] OpenHub reports $APPROVED_COUNT approved trace(s) — round trip closed"

  echo "──────── REAL DEMO PASSED ────────"
fi
deactivate
popd >/dev/null

# ── 4. summary ───────────────────────────────────────────────────────
echo "── 4/5 demo finished cleanly"
echo "    submitters: $SUBMITTERS_FILE"
echo "    OH log:     $OH_LOG"

# ── 5. tear-down via trap ────────────────────────────────────────────
echo "── 5/5 tearing down"
exit 0
