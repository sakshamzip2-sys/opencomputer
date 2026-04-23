"""Follow-up #25 — one-shot Docker-toggle hint on CLI startup.

Every ``opencomputer`` entry point that already loads a :class:`Config`
should call :func:`maybe_print_docker_toggle_hint` once. The function
is idempotent across invocations via a sentinel file, and silently
no-ops if anything goes wrong — it must never break the main CLI.

When fires:
  1. ``cfg.memory.provider == ""``      (user is on baseline memory)
  2. ``docker --version`` returns 0     (docker CLI present + runnable)
  3. ``docker compose version`` returns 0 (compose v2 plugin present)
  4. sentinel file absent               (haven't hinted before)

All four must hold. Anything else → silent no-op.

Why a sentinel? The legitimate case is: user set up on a Docker-less
laptop (wizard persisted ``provider=""``), then later installed Docker.
Once they've seen the hint they either run the suggested command (and
``provider`` flips to ``memory-honcho``, so the first check fails) or
they've made an informed choice to stay on baseline — either way, don't
nag them again.

Sentinel path: ``<OPENCOMPUTER_HOME>/.docker_toggle_hinted`` (profile-
aware, because :func:`_home` honours ``OPENCOMPUTER_HOME``). A hint
seen under one profile won't suppress under another — that's intentional,
because each profile has its own memory config.
"""

from __future__ import annotations

import subprocess

from opencomputer.agent.config import Config, _home

_HINT_LINE = (
    "\U0001f4a1 Docker is now available on this machine. "
    "Run `opencomputer memory setup` to enable Honcho (advanced memory)."
)
_SENTINEL_NAME = ".docker_toggle_hinted"
_DETECT_TIMEOUT_S = 1.0


def _detect_docker_simple() -> bool:
    """Return True iff both ``docker --version`` and ``docker compose version``
    exit 0 within :data:`_DETECT_TIMEOUT_S` each.

    Kept tiny and self-contained so this module has zero dependency on
    the ``memory-honcho`` extension (which is an optional overlay). If
    the extension isn't installed, the hint layer still works.

    Any exception (OSError, TimeoutExpired, FileNotFoundError when
    ``docker`` is absent) returns False — this is a best-effort probe.
    """
    try:
        r1 = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            timeout=_DETECT_TIMEOUT_S,
        )
        if r1.returncode != 0:
            return False
        r2 = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=_DETECT_TIMEOUT_S,
        )
        return r2.returncode == 0
    except Exception:  # noqa: BLE001 — UX nicety must never break CLI
        return False


def maybe_print_docker_toggle_hint(cfg: Config) -> None:
    """Print the Docker-toggle hint exactly once, when the four conditions hold.

    Order of checks matters for both correctness (no unnecessary subprocess
    calls) and testability (the provider-set path must not shell out):

      1. provider already set → bail, no sentinel update.
      2. sentinel exists       → bail, no subprocess.
      3. docker detected?      → yes: print + write sentinel. no: bail,
                                 no sentinel (Docker might get installed
                                 later — give the hint another chance).

    Any exception in this function is swallowed; the CLI must continue.
    """
    try:
        if cfg.memory.provider != "":
            return  # already on an overlay — nothing to hint

        sentinel = _home() / _SENTINEL_NAME
        if sentinel.exists():
            return  # already hinted once, don't nag

        if not _detect_docker_simple():
            return  # Docker not (yet) usable; re-check on next invocation

        # All four conditions hold — hint and burn the sentinel.
        print(_HINT_LINE)
        try:
            sentinel.touch()
        except OSError:
            # Can't write sentinel (read-only home, weird perms). Hint
            # was still shown; re-hinting next time is annoying but not
            # broken. Swallow silently.
            pass
    except Exception:  # noqa: BLE001 — UX nicety must never break CLI
        return


__all__ = ["maybe_print_docker_toggle_hint"]
