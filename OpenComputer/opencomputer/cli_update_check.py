"""Non-blocking PyPI update check (hermes parity).

Mirrors hermes-agent's ``hermes_cli/banner.py::prefetch_update_check`` /
``check_for_updates`` pair, but adapted for OC's pip-installed
distribution model — hermes checks ``git fetch origin/main`` because
hermes is installed via git clone; OC ships on PyPI, so we hit
``https://pypi.org/pypi/opencomputer/json`` and compare the published
``info.version`` against the running ``__version__``.

Design:

* Cached at ``~/.opencomputer/.update_check.json`` for
  :data:`_CACHE_TTL_SECONDS` (24h). Cache misses + offline are silent.
* Background daemon thread — never blocks startup. The result is
  picked up at the END of the chat session via
  :func:`get_update_hint` so the prompt isn't disturbed.
* Opt-out via ``OPENCOMPUTER_NO_UPDATE_CHECK=1`` env var. Useful in
  air-gapped environments and CI.
* Every error path returns ``None`` and is swallowed — the update
  check must never crash startup or even leak a warning into the
  banner. Logged to debug only.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

from opencomputer import __version__

_log = logging.getLogger("opencomputer.cli_update_check")

#: How long a cached check is considered fresh. 24h matches typical
#: release cadence for a date-versioned project — checking more often
#: is noise; checking less often hides genuinely stale installs for
#: too long.
_CACHE_TTL_SECONDS = 24 * 3600

#: The PyPI JSON endpoint we probe. Stable URL, 200 OK on every
#: published package; 404 on non-existent / unpublished.
_PYPI_URL = "https://pypi.org/pypi/opencomputer/json"

#: HTTP timeout — short. We're a background thread; if PyPI is slow
#: we'd rather skip than hold a thread open for minutes.
_HTTP_TIMEOUT_SECONDS = 5

#: Set by the background thread when the check completes (success OR
#: failure). Public consumers read via :func:`get_update_hint`.
_check_done = threading.Event()
_latest_version: str | None = None


def _cache_path() -> Path:
    from opencomputer.agent.config import _home

    return _home() / ".update_check.json"


def _opt_out() -> bool:
    return bool(os.environ.get("OPENCOMPUTER_NO_UPDATE_CHECK"))


def _load_cache() -> tuple[str | None, float]:
    """Return (latest_version, timestamp) from disk or (None, 0) on miss."""
    try:
        raw = _cache_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data.get("latest"), float(data.get("ts") or 0.0)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None, 0.0


def _save_cache(latest: str | None) -> None:
    """Atomically persist the latest-version probe.

    Reviewer fix: daemon threads are killed mid-syscall on process
    exit, so a naive ``write_text`` could leave a truncated /
    half-empty JSON file on disk. Next session would still recover
    (``_load_cache`` swallows ``JSONDecodeError`` and re-fetches),
    but the corruption-then-refetch breaks the 24h cache contract.
    Write to ``.tmp`` then ``os.replace()`` — atomic on POSIX, and
    Windows-safe since Python 3.3.
    """
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"latest": latest, "ts": time.time()}),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        _log.debug("update-check cache write failed: %s", exc)


#: Maximum bytes we'll read from the PyPI JSON response before
#: bailing. Real PyPI ``/pypi/<pkg>/json`` payloads land in the
#: 50-200 KB range; 1 MB is ~5x that. A hostile / buggy mirror
#: that streams MB into memory would otherwise cause silent
#: memory pressure on the daemon thread before json.loads errors.
_MAX_RESPONSE_BYTES = 1_048_576


def _fetch_pypi_latest() -> str | None:
    """Hit PyPI's JSON endpoint and return the latest published version.

    Returns ``None`` on any failure (offline, timeout, 404, malformed
    response, oversize body). Never raises.
    """
    try:
        req = urllib.request.Request(
            _PYPI_URL,
            headers={"User-Agent": f"opencomputer/{__version__} (+update-check)"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            # Reviewer fix: cap the read so a hostile mirror or MITM
            # on a misconfigured corp proxy can't stream MBs into the
            # daemon thread before json.loads complains. Read +1 byte
            # over the cap so we can detect truncation and bail loudly
            # rather than silently parsing a partial document.
            body = resp.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            _log.debug(
                "PyPI response exceeded %d bytes — bailing", _MAX_RESPONSE_BYTES
            )
            return None
        data = json.loads(body.decode("utf-8"))
        version = (data.get("info") or {}).get("version")
        return str(version) if version else None
    except Exception as exc:  # noqa: BLE001 — must never crash startup
        _log.debug("PyPI update check failed: %s", exc)
        return None


def prefetch_update_check() -> None:
    """Start the background update check.

    Idempotent — safe to call multiple times (the inner thread no-ops
    if the event is already set). Honours the
    ``OPENCOMPUTER_NO_UPDATE_CHECK`` opt-out.
    """
    if _opt_out() or _check_done.is_set():
        _check_done.set()
        return

    cached_latest, cached_ts = _load_cache()
    if cached_latest is not None and (time.time() - cached_ts) < _CACHE_TTL_SECONDS:
        global _latest_version
        _latest_version = cached_latest
        _check_done.set()
        return

    def _run() -> None:
        global _latest_version
        latest = _fetch_pypi_latest()
        if latest is not None:
            _latest_version = latest
            _save_cache(latest)
        _check_done.set()

    threading.Thread(target=_run, daemon=True, name="oc-update-check").start()


def get_update_hint(timeout: float = 0.2) -> str | None:
    """Return a one-line upgrade hint when a newer version is available.

    Blocks up to ``timeout`` seconds for the background check to
    finish — defaults to 200ms so callers (e.g. the chat exit banner)
    don't stutter waiting on PyPI. Returns ``None`` when:

    - check still running after timeout (degrade silently — they'll see
      it next session)
    - we couldn't reach PyPI
    - the running version is up-to-date (or even ahead, e.g. a dev
      build with a date-version newer than the published one)
    """
    _check_done.wait(timeout=timeout)
    latest = _latest_version
    if not latest:
        return None
    if _is_outdated(__version__, latest):
        return (
            f"A newer opencomputer ({latest}) is available "
            f"— upgrade with: pip install -U opencomputer"
        )
    return None


def _is_outdated(running: str, latest: str) -> bool:
    """Conservative version comparison — date-versioned strings sort
    lexicographically when zero-padded, but PyPI publishes
    ``YYYY.M.D`` (no zero-pad), so we need to parse.

    Compares (year, month, day) tuples. Anything we can't parse is
    treated as "not outdated" so a malformed running-version (e.g.
    ``0.0.0+unknown`` from a broken install) doesn't cause a spurious
    upgrade nag.
    """
    def _parse(v: str) -> tuple[int, int, int] | None:
        if not v:
            return None
        head = v.split("+", 1)[0]
        parts = head.split(".")
        if len(parts) < 3:
            return None
        try:
            tup = (int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            return None
        # ``0.0.0`` is OC's placeholder for "importlib.metadata could not
        # resolve a version" — i.e. a broken install. Skip the nag in
        # that case; the user has bigger problems than version drift.
        if tup == (0, 0, 0):
            return None
        return tup

    r = _parse(running)
    l_ = _parse(latest)
    if r is None or l_ is None:
        return False
    return l_ > r
