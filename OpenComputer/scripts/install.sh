#!/usr/bin/env bash
# ============================================================================
# OpenComputer one-line installer
# ============================================================================
# Installs the `opencomputer` CLI on macOS, Linux, and Termux. Mirrors the
# shape of hermes-agent's scripts/install.sh — keeps the curl-bash flow
# small and predictable.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/sakshamzip2-sys/opencomputer/main/scripts/install.sh | bash
#
# Or with options:
#   curl -fsSL ... | bash -s -- --dev          # install from local clone in editable mode
#   curl -fsSL ... | bash -s -- --dry-run      # print what would happen, do nothing
#   curl -fsSL ... | bash -s -- --no-user      # system-wide install (use sudo)
#   curl -fsSL ... | bash -s -- --use-pipx     # force pipx (auto-installs if missing)
#
# What it does:
#   1. Finds a Python >= 3.13 (probes python3.14, python3.13, then python3)
#   2. Picks an install strategy (pipx > pip --user > venv at ~/.opencomputer/venv)
#   3. Runs the install
#   4. Verifies the binary is reachable
#   5. Prints next steps (opencomputer setup)
# ============================================================================

set -euo pipefail

# ── Colours ────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    BLUE='\033[0;34m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' BOLD='' NC=''
fi

# Reviewer fix #8: separate format string from arg payload so any
# `%` byte in the message can't confuse printf.
info()  { printf "%b%s%b  %s\n" "$BLUE"   "ℹ" "$NC" "$*"; }
ok()    { printf "%b%s%b  %s\n" "$GREEN"  "✓" "$NC" "$*"; }
warn()  { printf "%b%s%b  %s\n" "$YELLOW" "!" "$NC" "$*"; }
err()   { printf "%b%s%b  %s\n" "$RED"    "✗" "$NC" "$*" >&2; }
note()  { printf "    %s\n" "$*"; }

# Reviewer fix #5: inline help string. The previous sed-extraction was
# fragile to any header reorder.
print_help() {
    cat <<'HELP'
OpenComputer one-line installer

Usage:
  curl -fsSL https://raw.githubusercontent.com/sakshamzip2-sys/opencomputer/main/scripts/install.sh | bash

Or with options:
  bash install.sh --dev          # install from local clone in editable mode
  bash install.sh --dry-run      # print what would happen, do nothing
  bash install.sh --no-user      # system-wide install (use sudo)
  bash install.sh --use-pipx     # force pipx (auto-installs if missing)
  bash install.sh --help         # show this message

What it does:
  1. Finds a Python >= 3.13 (probes python3.14, python3.13, then python3)
  2. Picks an install strategy (pipx > pip --user > venv at ~/.opencomputer/venv)
  3. Runs the install
  4. Verifies the binary is reachable
  5. Prints next steps (opencomputer setup)
HELP
}

# ── Args ────────────────────────────────────────────────────────────────────
DEV_INSTALL=0
DRY_RUN=0
NO_USER=0
FORCE_PIPX=0
PACKAGE_NAME="opencomputer"
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=13

for arg in "$@"; do
    case "$arg" in
        --dev)        DEV_INSTALL=1 ;;
        --dry-run)    DRY_RUN=1 ;;
        --no-user)    NO_USER=1 ;;
        --use-pipx)   FORCE_PIPX=1 ;;
        --help|-h)
            print_help
            exit 0
            ;;
        *)
            err "Unknown flag: $arg"
            exit 2
            ;;
    esac
done

# Reviewer fix #3: use argv arrays not eval. The previous `eval "$@"`
# pattern was a code-execution hazard if any future maintainer let a
# user-controlled value flow into a `run` argument.
run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf "    [dry-run]"
        printf " %q" "$@"
        printf "\n"
    else
        "$@"
    fi
}

# ── Step 1: find a viable Python >= 3.13 ───────────────────────────────────
printf "\n%b%s%b\n\n" "$BOLD" "OpenComputer installer" "$NC"

# Reviewer fix #4: probe specific minor versions before falling back
# to bare `python3` so a system with both 3.9 and 3.13 installed
# (common on macOS via brew) finds 3.13 instead of refusing.
PY=""
for candidate in python3.14 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        ver=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
        major=${ver%.*}
        minor=${ver#*.}
        if (( major > PYTHON_MIN_MAJOR )) || \
           (( major == PYTHON_MIN_MAJOR && minor >= PYTHON_MIN_MINOR )); then
            PY="$candidate"
            PY_VERSION="$ver"
            break
        fi
    fi
done

if [[ -z "$PY" ]]; then
    err "No Python >= ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR} found on PATH."
    note "Install Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR} first:"
    note "  macOS:  brew install python@3.13"
    note "  Linux:  apt install python3.13   # Debian/Ubuntu (may need a PPA)"
    note "  Termux: pkg install python"
    exit 1
fi

ok "${PY} (Python ${PY_VERSION})"

# ── Step 2: pick install strategy ──────────────────────────────────────────
STRATEGY=""
if command -v pipx >/dev/null 2>&1; then
    STRATEGY="pipx"
elif [[ $FORCE_PIPX -eq 1 ]]; then
    # Reviewer fix #1: --use-pipx without pipx now installs pipx
    # instead of failing with `command not found` further down.
    info "pipx not on PATH — bootstrapping via pip --user"
    if [[ $DRY_RUN -eq 1 ]]; then
        printf "    [dry-run] %s -m pip install --user pipx\n" "$PY"
    else
        "$PY" -m pip install --user pipx >/dev/null 2>&1 || {
            err "Failed to bootstrap pipx via 'pip install --user pipx'."
            note "Install pipx manually then re-run:"
            note "  $PY -m pip install --user pipx"
            exit 1
        }
    fi
    STRATEGY="pipx-bootstrapped"
else
    if "$PY" -m pip --help >/dev/null 2>&1; then
        STRATEGY="pip-user"
        if [[ $NO_USER -eq 1 ]]; then
            STRATEGY="pip-system"
        fi
    else
        err "Neither pipx nor pip is available."
        note "Install pip:  $PY -m ensurepip --upgrade"
        note "Install pipx: $PY -m pip install --user pipx"
        exit 1
    fi
fi

info "install strategy: ${STRATEGY}"

# ── Step 3: PEP 668 detection (externally managed environment) ─────────────
# Reviewer fix #2: probe pip's actual behaviour via a dry-run install
# rather than guessing from the EXTERNALLY-MANAGED marker file.
# Distros that patch pip to honour --break-system-packages or that
# carve out user-site exceptions are correctly handled this way.
if [[ "$STRATEGY" == "pip-user" ]]; then
    if ! "$PY" -m pip install --user --dry-run "$PACKAGE_NAME" >/dev/null 2>&1; then
        warn "pip --user refused (likely PEP 668 'externally managed')."
        note "Falling back to a managed venv at ~/.opencomputer/venv."
        STRATEGY="venv"
    fi
fi

# ── Step 4: install ────────────────────────────────────────────────────────
INSTALL_ARGS=( "$PACKAGE_NAME" )
if [[ $DEV_INSTALL -eq 1 ]]; then
    if [[ ! -f pyproject.toml ]]; then
        err "--dev expects pyproject.toml in the current directory."
        note "cd into the OpenComputer repo root before running with --dev."
        exit 1
    fi
    INSTALL_ARGS=( "-e" "." )
    info "installing OpenComputer in editable mode from $(pwd)"
else
    info "installing ${PACKAGE_NAME} from PyPI"
fi

case "$STRATEGY" in
    pipx|pipx-bootstrapped)
        run pipx install "${INSTALL_ARGS[@]}"
        ;;
    pip-user)
        run "$PY" -m pip install --user --upgrade "${INSTALL_ARGS[@]}"
        ;;
    pip-system)
        run "$PY" -m pip install --upgrade "${INSTALL_ARGS[@]}"
        ;;
    venv)
        VENV_PATH="${HOME}/.opencomputer/venv"
        run "$PY" -m venv "$VENV_PATH"
        run "${VENV_PATH}/bin/pip" install --upgrade "${INSTALL_ARGS[@]}"
        info "venv created at ${VENV_PATH}"
        info "add to PATH:  export PATH=\"${VENV_PATH}/bin:\$PATH\""
        info "or symlink:    ln -sf ${VENV_PATH}/bin/opencomputer /usr/local/bin/opencomputer"
        ;;
    *)
        err "Unknown strategy: ${STRATEGY}"
        exit 1
        ;;
esac

# ── Step 5: verify the binary is reachable ─────────────────────────────────
# Reviewer fixes #6 + #7: pip / venv exit 0 doesn't mean the binary
# is on PATH. For pip-user, ~/.local/bin needs to be exported. For
# venv, the binary lives inside the venv. Probe both.
if [[ $DRY_RUN -ne 1 ]]; then
    if [[ "$STRATEGY" == "venv" ]]; then
        VENV_BIN="${VENV_PATH:-$HOME/.opencomputer/venv}/bin/opencomputer"
        if [[ ! -x "$VENV_BIN" ]]; then
            warn "expected ${VENV_BIN} after install — not found."
            note "Re-run with --dry-run to inspect the install commands."
        fi
    elif ! command -v opencomputer >/dev/null 2>&1; then
        warn "opencomputer is not on PATH yet."
        case "$STRATEGY" in
            pip-user)
                note "Add ~/.local/bin to PATH (then re-open your shell):"
                note "  export PATH=\"\$HOME/.local/bin:\$PATH\""
                ;;
            pipx|pipx-bootstrapped)
                note "Run:  pipx ensurepath  (then re-open your shell)"
                ;;
        esac
    fi
fi

# ── Step 6: next steps ─────────────────────────────────────────────────────
printf "\n%b%s%b\n\n" "${GREEN}${BOLD}" "OpenComputer installed." "$NC"

if [[ $DRY_RUN -eq 1 ]]; then
    info "(dry-run — no changes made)"
fi

note "Next steps:"
note "  1. Make sure ~/.local/bin (or your venv bin) is on PATH"
note "  2. Run:  ${BOLD}opencomputer setup${NC}     — interactive first-run wizard"
note "  3. Then: ${BOLD}opencomputer${NC}           — start chatting"
printf "\n"
