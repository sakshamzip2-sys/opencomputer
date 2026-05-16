"""Track which environment variables were sourced from a profile's
``.env`` so a profile swap can cleanly unload them and load the new
profile's keys without clobbering shell-exported values.

Closes §9.3 documented in
``docs/plans/profile-handoff-investigation.md``: before this fix,
``.env`` was loaded once at process start and never reloaded after a
profile swap, so provider calls post-swap kept using the original
profile's API keys.

## Why this is non-trivial

Naively setting + clearing env keys would clobber shell-exported
values. Consider:

* User runs ``ANTHROPIC_API_KEY=shell-key oc chat``
* Profile A's ``.env`` sets ``ANTHROPIC_API_KEY=profile-a-key``
* After ``load_dotenv(override=True)``: ``os.environ`` holds
  ``profile-a-key``, shell value is gone
* User swaps to profile B
* Naive unload removes ``ANTHROPIC_API_KEY`` entirely — user-visible
  surprise; they expected ``shell-key`` to come back

The tracker captures the **pre-dotenv** snapshot for each key the
``.env`` file touched. On unload, those keys revert to their
pre-dotenv value (or are removed if they didn't exist pre-dotenv).
The new profile's ``.env`` is then loaded on top via ``load_dotenv``.

## Public API

* :func:`load_profile_dotenv` — load ``<profile_home>/.env`` and
  remember what changed; idempotent / safe to call repeatedly.
* :func:`unload_active_dotenv` — reverse the most recent load,
  restoring pre-dotenv state.
* :func:`swap_profile_dotenv` — convenience wrapper that calls unload
  + load atomically, used by the profile-rebind handler.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

_log = logging.getLogger(__name__)

# Module-level state: tracks the env keys + their pre-dotenv values
# for the currently-active profile. None = no profile dotenv has been
# loaded since process start.
_lock = threading.Lock()
_active_snapshot: dict[str, str | None] | None = None
_active_path: Path | None = None


def load_profile_dotenv(profile_home: Path, *, override: bool = True) -> int:
    """Load ``<profile_home>/.env`` and remember what we changed.

    Args:
        profile_home: Profile root that holds the ``.env`` file.
        override: Passed to ``load_dotenv``. Default ``True`` matches
            the boot-time behavior in ``cli.py:_on_reload``.

    Returns:
        Number of env keys touched by the load (added or changed).
        Returns 0 if the .env file doesn't exist or python-dotenv is
        not installed.

    Raises:
        TypeError: if ``profile_home`` is not a Path.
    """
    if not isinstance(profile_home, Path):
        raise TypeError(
            f"profile_home must be a Path, got {type(profile_home).__name__}"
        )

    env_path = profile_home / ".env"
    if not env_path.exists():
        _log.debug("dotenv: no .env at %s — nothing to load", env_path)
        return 0

    try:
        from dotenv import dotenv_values, load_dotenv
    except ImportError:
        _log.warning(
            "dotenv: python-dotenv not installed; profile .env files cannot "
            "be loaded or unloaded. Run ``pip install python-dotenv``.",
        )
        return 0

    try:
        new_vals: dict[str, str | None] = dict(dotenv_values(str(env_path)))
    except Exception:  # noqa: BLE001 — malformed .env file
        _log.warning(
            "dotenv: failed to parse %s — leaving env untouched", env_path,
            exc_info=True,
        )
        return 0

    with _lock:
        # Capture pre-load values BEFORE load_dotenv mutates os.environ.
        # ``None`` sentinel = key was not set before load (so unload
        # should ``del`` it rather than restoring a value).
        snapshot: dict[str, str | None] = {
            k: os.environ.get(k, None) for k in new_vals if k
        }
        try:
            load_dotenv(str(env_path), override=override)
        except Exception:  # noqa: BLE001 — load_dotenv usually swallows internally
            _log.warning(
                "dotenv: load_dotenv(%s) raised — env state may be partial",
                env_path,
                exc_info=True,
            )
        global _active_snapshot, _active_path
        _active_snapshot = snapshot
        _active_path = env_path

        touched = sum(1 for v in new_vals.values() if v is not None)
        _log.info(
            "dotenv: loaded %d key(s) from %s (override=%s)",
            touched, env_path, override,
        )
        return touched


def unload_active_dotenv() -> int:
    """Reverse the most recent :func:`load_profile_dotenv` call.

    For each key the prior load touched: if its pre-load value was
    ``None`` (key did not exist), ``del`` it; otherwise restore the
    pre-load value.

    Returns:
        Number of env keys reverted. Returns 0 if no dotenv is
        currently tracked as loaded.
    """
    global _active_snapshot, _active_path
    with _lock:
        if _active_snapshot is None:
            return 0
        snapshot = _active_snapshot
        path = _active_path

        reverted = 0
        for key, prior in snapshot.items():
            if prior is None:
                # Key did not exist pre-load; remove if still present.
                if key in os.environ:
                    del os.environ[key]
                    reverted += 1
            else:
                # Restore the pre-load value (could be a shell-set value
                # that was overridden by override=True).
                if os.environ.get(key) != prior:
                    os.environ[key] = prior
                    reverted += 1

        _active_snapshot = None
        _active_path = None
        _log.info(
            "dotenv: unloaded %d key(s) tracked from %s", reverted, path,
        )
        return reverted


def swap_profile_dotenv(new_home: Path, old_home: Path | None = None) -> int:
    """Atomic unload-then-load for a profile swap.

    Calls :func:`unload_active_dotenv` to remove the prior profile's
    keys, then :func:`load_profile_dotenv` for the new profile.

    Args:
        new_home: Profile home directory for the NEW profile (has
            ``<new_home>/.env``).
        old_home: Unused; kept for symmetry with the rebind-handler
            signature ``(new, old) -> None``. The unload step uses
            the internally-tracked snapshot, not ``old_home``.

    Returns:
        Number of keys touched by the LOAD half (not the unload).
    """
    del old_home  # snapshot-based unload; signature parity only
    unload_active_dotenv()
    return load_profile_dotenv(new_home)


def active_dotenv_path() -> Path | None:
    """Return the path of the currently-loaded ``.env``, or ``None``."""
    return _active_path


def active_dotenv_keys() -> tuple[str, ...]:
    """Return the keys currently tracked as sourced from the active ``.env``."""
    with _lock:
        return tuple(_active_snapshot or ())


def _reset_for_tests() -> None:
    """Test-only: clear module state without touching ``os.environ``.

    NOT for production use. Tests that mutate the snapshot directly
    can leak state across tests; use this in a fixture teardown.
    """
    global _active_snapshot, _active_path
    with _lock:
        _active_snapshot = None
        _active_path = None
