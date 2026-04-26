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
#   curl -fsSL ... | bash -s -- --use-pipx     # force pipx even if not installed
#
# What it does:
#   1. Verifies python3 >= 3.13 is on PATH
#   2. Picks an install strategy (pipx > pip --user > venv at ~/.opencomputer/venv)
#   3. Runs the install
#   4. Prints next steps (opencomputer setup)
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

info()  { printf "${BLUE}ℹ${NC}  %s\n" "$*"; }
ok()    { printf "${GREEN}✓${NC}  %s\n" "$*"; }
warn()  { printf "${YELLOW}!${NC}  %s\n" "$*"; }
err()   { printf "${RED}✗${NC}  %s\n" "$*" >&2; }
note()  { printf "    %s\n" "$*"; }

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
            sed -n '/^# Usage:/,/^# What it does:/p' "$0" | sed 's/^# //; s/^#//'
            exit 0
            ;;
        *)
            err "Unknown flag: $arg"
            exit 2
            ;;
    esac
done

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf "    [dry-run] %s\n" "$*"
    else
        eval "$@"
    fi
}

# ── Step 1: detect Python ──────────────────────────────────────────────────
printf "\n${BOLD}OpenComputer installer${NC}\n\n"

if ! command -v python3 >/dev/null 2>&1; then
    err "python3 not found on PATH."
    note "Install Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR} first:"
    note "  macOS:  brew install python@3.13"
    note "  Linux:  apt install python3.13   # Debian/Ubuntu (may need a PPA)"
    note "  Termux: pkg install python"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=${PY_VERSION%.*}
PY_MINOR=${PY_VERSION#*.}

if (( PY_MAJOR < PYTHON_MIN_MAJOR )) || \
   (( PY_MAJOR == PYTHON_MIN_MAJOR && PY_MINOR < PYTHON_MIN_MINOR )); then
    err "python3 is ${PY_VERSION} — OpenComputer needs >= ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}."
    note "Install Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR} alongside the existing one and re-run this script with python3.13 on PATH."
    exit 1
fi

ok "python3 ${PY_VERSION}"

# ── Step 2: pick install strategy ──────────────────────────────────────────
STRATEGY=""
if [[ $FORCE_PIPX -eq 1 ]] || command -v pipx >/dev/null 2>&1; then
    STRATEGY="pipx"
else
    if python3 -m pip install --help >/dev/null 2>&1; then
        STRATEGY="pip-user"
        if [[ $NO_USER -eq 1 ]]; then
            STRATEGY="pip-system"
        fi
    else
        err "Neither pipx nor pip is available."
        note "Install pip:  python3 -m ensurepip --upgrade"
        note "Install pipx: python3 -m pip install --user pipx"
        exit 1
    fi
fi

info "install strategy: ${STRATEGY}"

# ── Step 3: PEP 668 detection (externally managed environment) ─────────────
PEP668_DETECTED=0
if [[ "$STRATEGY" == "pip-user" ]]; then
    if python3 -c 'import sys, sysconfig; sys.exit(0 if sysconfig.get_path("stdlib") and __import__("os").path.exists(sysconfig.get_path("stdlib") + "/EXTERNALLY-MANAGED") else 1)' 2>/dev/null; then
        PEP668_DETECTED=1
        warn "PEP 668 'externally managed' Python detected."
        note "pip --user will refuse to install into the system site-packages."
        note "Falling back to a managed venv at ~/.opencomputer/venv."
        STRATEGY="venv"
    fi
fi

# ── Step 4: install ────────────────────────────────────────────────────────
INSTALL_TARGET="${PACKAGE_NAME}"
if [[ $DEV_INSTALL -eq 1 ]]; then
    if [[ ! -f pyproject.toml ]]; then
        err "--dev expects pyproject.toml in the current directory."
        note "cd into the OpenComputer repo root before running with --dev."
        exit 1
    fi
    INSTALL_TARGET="-e ."
    info "installing OpenComputer in editable mode from $(pwd)"
else
    info "installing ${PACKAGE_NAME} from PyPI"
fi

case "$STRATEGY" in
    pipx)
        run "pipx install ${INSTALL_TARGET}"
        ;;
    pip-user)
        run "python3 -m pip install --user --upgrade ${INSTALL_TARGET}"
        ;;
    pip-system)
        run "python3 -m pip install --upgrade ${INSTALL_TARGET}"
        ;;
    venv)
        VENV_PATH="${HOME}/.opencomputer/venv"
        run "python3 -m venv \"${VENV_PATH}\""
        run "\"${VENV_PATH}/bin/pip\" install --upgrade ${INSTALL_TARGET}"
        info "venv created at ${VENV_PATH}"
        info "add to PATH:  export PATH=\"${VENV_PATH}/bin:\$PATH\""
        info "or symlink:    ln -sf ${VENV_PATH}/bin/opencomputer /usr/local/bin/opencomputer"
        ;;
    *)
        err "Unknown strategy: ${STRATEGY}"
        exit 1
        ;;
esac

# ── Step 5: next steps ─────────────────────────────────────────────────────
printf "\n${GREEN}${BOLD}OpenComputer installed.${NC}\n\n"

if [[ $DRY_RUN -eq 1 ]]; then
    info "(dry-run — no changes made)"
fi

note "Next steps:"
note "  1. Make sure ~/.local/bin (or your venv bin) is on PATH"
note "  2. Run:  ${BOLD}opencomputer setup${NC}     — interactive first-run wizard"
note "  3. Then: ${BOLD}opencomputer${NC}           — start chatting"
printf "\n"
