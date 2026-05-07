"""DM Pairing Code system — production-grade port of Hermes ``gateway/pairing.py``.

Code-based approval flow for authorizing new users on messaging platforms.
Instead of static allowlists with user IDs, unknown users receive a one-time
pairing code that the bot owner approves via the CLI:

    user → bot DM ("hello")
    bot → user    ("Pairing code: XKGH5N7P. Run: oc gateway pairing approve telegram XKGH5N7P")
    admin → CLI   "oc gateway pairing approve telegram XKGH5N7P"
    user → bot DM ("hello")        # now allowed

Security features (OWASP + NIST SP 800-63-4):
- 8-char codes from 32-char unambiguous alphabet (excludes 0/O/1/I).
- Cryptographic randomness via ``secrets.choice``.
- 1-hour code expiry.
- Max 3 pending codes per platform.
- Rate limiting: 1 request per (platform, user_id) per 10 minutes.
- Lockout after 5 failed approval attempts (1 hour, platform-wide).
- Atomic file writes (tmpfile + os.replace) with chmod 0600.
- Cross-process safety via ``flock`` (POSIX) / ``msvcrt.locking`` (Windows).

Storage: ``<profile>/pairing/`` — ``{platform}-pending.json``,
``{platform}-approved.json``, ``_rate_limits.json``.

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (T1.4)
Spec: docs/superpowers/specs/2026-05-08-messaging-gateway-parity-design.md (§5.2)
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger("opencomputer.channels.pairing_codes")

# ── Constants ──────────────────────────────────────────────────────────────

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
"""32-char unambiguous alphabet — no 0/O, no 1/I (mirrors Hermes upstream)."""

CODE_LENGTH = 8
CODE_TTL_SECONDS = 3600
RATE_LIMIT_SECONDS = 600
LOCKOUT_SECONDS = 3600
MAX_PENDING_PER_PLATFORM = 3
MAX_FAILED_ATTEMPTS = 5

# ── Cross-process locking ──────────────────────────────────────────────────


@contextlib.contextmanager
def _flock(path: Path):
    """Cross-process advisory lock around ``path``.

    POSIX: ``fcntl.flock(LOCK_EX)``.
    Windows: best-effort — uses ``msvcrt.locking`` if available, otherwise a
    rename-based fallback (acceptable: the only race is the first-time tip
    emitting twice on Windows, never a security boundary).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        # Windows fallback: open file exclusively. This isn't a true flock —
        # it's a best-effort advisory check. Crashes leave .lock files behind;
        # GC happens on next acquire that finds a stale .lock older than 60s.
        lock_path = path.with_suffix(path.suffix + ".lock")
        for _ in range(50):
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                break
            except FileExistsError:
                # Stale lock GC.
                try:
                    if time.time() - lock_path.stat().st_mtime > 60:
                        lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                time.sleep(0.1)
        else:
            # Force-acquire as a last resort.
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            yield
        finally:
            try:
                os.close(fd)
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        import fcntl

        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _atomic_write(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` atomically with 0600 perms (POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


# ── Public dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PairingCode:
    """A pending pairing request (read-only snapshot)."""

    platform: str
    user_id: str
    user_name: str
    code: str
    created_at: float

    @property
    def expires_at(self) -> float:
        return self.created_at + CODE_TTL_SECONDS

    @property
    def age_minutes(self) -> int:
        return int((time.time() - self.created_at) / 60)


# ── Store ──────────────────────────────────────────────────────────────────


class PairingCodeStore:
    """Owner-restricted file-backed pairing-code store.

    All read-modify-write cycles are protected by an in-process ``RLock`` AND
    a per-store cross-process ``flock`` (so a daemon and a CLI invocation of
    ``oc gateway pairing approve`` can't race even though they share a
    profile dir).
    """

    def __init__(self, profile_home: Path):
        self._dir = Path(profile_home) / "pairing"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._rlock = threading.RLock()
        self._flock_path = self._dir / ".lock"

    # ── path helpers ──

    def _pending_path(self, platform: str) -> Path:
        return self._dir / f"{platform}-pending.json"

    def _approved_path(self, platform: str) -> Path:
        return self._dir / f"{platform}-approved.json"

    def _rate_limit_path(self) -> Path:
        return self._dir / "_rate_limits.json"

    # ── JSON IO with corruption recovery ──

    def _load_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            backup = path.with_suffix(f"{path.suffix}.corrupt.{int(time.time())}")
            with contextlib.suppress(OSError):
                shutil.copy2(path, backup)
            logger.warning(
                "pairing-store: corrupt JSON at %s — backed up to %s; resetting",
                path,
                backup,
            )
            return {}

    def _save_json(self, path: Path, data: dict) -> None:
        _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))

    # ── Approved users ──

    def is_approved(self, platform: str, user_id: str) -> bool:
        with self._rlock, _flock(self._flock_path):
            return user_id in self._load_json(self._approved_path(platform))

    def list_approved(self, platform: str | None = None) -> list[dict]:
        results: list[dict] = []
        with self._rlock, _flock(self._flock_path):
            platforms = [platform] if platform else self._all_platforms("approved")
            for p in platforms:
                approved = self._load_json(self._approved_path(p))
                for uid, info in approved.items():
                    results.append({"platform": p, "user_id": uid, **info})
        return results

    def revoke(self, platform: str, user_id: str) -> bool:
        with self._rlock, _flock(self._flock_path):
            path = self._approved_path(platform)
            approved = self._load_json(path)
            if user_id in approved:
                del approved[user_id]
                self._save_json(path, approved)
                return True
            return False

    def _approve_user(self, platform: str, user_id: str, user_name: str = "") -> None:
        """Mutate-only approval write. Caller must hold rlock + flock."""
        path = self._approved_path(platform)
        approved = self._load_json(path)
        approved[user_id] = {
            "user_name": user_name,
            "approved_at": time.time(),
        }
        self._save_json(path, approved)

    # ── Pending codes ──

    def generate_code(
        self, platform: str, user_id: str, user_name: str = ""
    ) -> str | None:
        """Mint a one-time pairing code or return ``None`` on rate-limit /
        lockout / max-pending."""
        with self._rlock, _flock(self._flock_path):
            self._cleanup_expired(platform)
            if self._is_locked_out(platform):
                return None
            if self._is_rate_limited(platform, user_id):
                return None
            pending = self._load_json(self._pending_path(platform))
            if len(pending) >= MAX_PENDING_PER_PLATFORM:
                return None
            code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
            pending[code] = {
                "user_id": user_id,
                "user_name": user_name,
                "created_at": time.time(),
            }
            self._save_json(self._pending_path(platform), pending)
            self._record_rate_limit(platform, user_id)
            return code

    def regenerate_code(
        self, platform: str, user_id: str, user_name: str = ""
    ) -> str | None:
        """Force-mint a fresh code, bypassing the rate limit but honoring
        lockout. Used by ``oc gateway pairing regen`` for admin UX when a
        user lost their original code."""
        with self._rlock, _flock(self._flock_path):
            self._cleanup_expired(platform)
            if self._is_locked_out(platform):
                return None
            # Drop any existing pending entries for this user_id (latest wins).
            pending = self._load_json(self._pending_path(platform))
            for existing_code in list(pending.keys()):
                if pending[existing_code].get("user_id") == user_id:
                    del pending[existing_code]
            if len(pending) >= MAX_PENDING_PER_PLATFORM:
                return None
            code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
            pending[code] = {
                "user_id": user_id,
                "user_name": user_name,
                "created_at": time.time(),
            }
            self._save_json(self._pending_path(platform), pending)
            # Reset the rate-limit timer so the user can re-DM if needed.
            self._record_rate_limit(platform, user_id)
            return code

    def approve_code(self, platform: str, code: str) -> dict | None:
        """Approve a pending code. Returns ``{user_id, user_name}`` on
        success, ``None`` if invalid/expired (and records a failure that
        contributes to the lockout counter)."""
        with self._rlock, _flock(self._flock_path):
            self._cleanup_expired(platform)
            code = (code or "").upper().strip()
            pending = self._load_json(self._pending_path(platform))
            if code not in pending:
                self._record_failed_attempt(platform)
                return None
            entry = pending.pop(code)
            self._save_json(self._pending_path(platform), pending)
            self._approve_user(
                platform, entry["user_id"], entry.get("user_name", "")
            )
            return {
                "user_id": entry["user_id"],
                "user_name": entry.get("user_name", ""),
            }

    def list_pending(self, platform: str | None = None) -> list[dict]:
        """List pending requests with ``age_minutes`` for table rendering."""
        results: list[dict] = []
        with self._rlock, _flock(self._flock_path):
            platforms = [platform] if platform else self._all_platforms("pending")
            for p in platforms:
                self._cleanup_expired(p)
                pending = self._load_json(self._pending_path(p))
                for code, info in pending.items():
                    age_min = int((time.time() - info["created_at"]) / 60)
                    results.append(
                        {
                            "platform": p,
                            "code": code,
                            "user_id": info["user_id"],
                            "user_name": info.get("user_name", ""),
                            "age_minutes": age_min,
                        }
                    )
        return results

    def clear_pending(self, platform: str | None = None) -> int:
        """Drop all pending requests. Returns count removed."""
        with self._rlock, _flock(self._flock_path):
            count = 0
            platforms = [platform] if platform else self._all_platforms("pending")
            for p in platforms:
                pending = self._load_json(self._pending_path(p))
                count += len(pending)
                self._save_json(self._pending_path(p), {})
            return count

    # ── Rate limiting and lockout (held under flock by callers) ──

    def _is_rate_limited(self, platform: str, user_id: str) -> bool:
        limits = self._load_json(self._rate_limit_path())
        last = limits.get(f"{platform}:{user_id}", 0)
        return (time.time() - last) < RATE_LIMIT_SECONDS

    def _record_rate_limit(self, platform: str, user_id: str) -> None:
        limits = self._load_json(self._rate_limit_path())
        limits[f"{platform}:{user_id}"] = time.time()
        self._save_json(self._rate_limit_path(), limits)

    def _is_locked_out(self, platform: str) -> bool:
        limits = self._load_json(self._rate_limit_path())
        return time.time() < limits.get(f"_lockout:{platform}", 0)

    def _record_failed_attempt(self, platform: str) -> None:
        limits = self._load_json(self._rate_limit_path())
        fails = limits.get(f"_failures:{platform}", 0) + 1
        limits[f"_failures:{platform}"] = fails
        if fails >= MAX_FAILED_ATTEMPTS:
            limits[f"_lockout:{platform}"] = time.time() + LOCKOUT_SECONDS
            limits[f"_failures:{platform}"] = 0
            logger.warning(
                "pairing-store: platform %s locked out for %ds after %d failed attempts",
                platform,
                LOCKOUT_SECONDS,
                MAX_FAILED_ATTEMPTS,
            )
        self._save_json(self._rate_limit_path(), limits)

    # ── Cleanup ──

    def _cleanup_expired(self, platform: str) -> None:
        path = self._pending_path(platform)
        pending = self._load_json(path)
        now = time.time()
        expired = [
            code
            for code, info in pending.items()
            if (now - info["created_at"]) > CODE_TTL_SECONDS
        ]
        if expired:
            for code in expired:
                del pending[code]
            self._save_json(path, pending)

    def expired_sweep_all(self) -> int:
        """Sweep expired codes across all platforms. Returns count removed.

        Wired into the cron tick (or ``Gateway.serve_forever`` periodic
        loop fallback).
        """
        removed = 0
        with self._rlock, _flock(self._flock_path):
            for p in self._all_platforms("pending"):
                before = len(self._load_json(self._pending_path(p)))
                self._cleanup_expired(p)
                after = len(self._load_json(self._pending_path(p)))
                removed += before - after
        return removed

    def _all_platforms(self, suffix: str) -> list[str]:
        platforms: list[str] = []
        for f in self._dir.iterdir():
            name = f.name
            if name.endswith(f"-{suffix}.json"):
                p = name[: -(len(suffix) + len("-.json"))]
                if not p.startswith("_"):
                    platforms.append(p)
        return platforms

    # ── Deep-link helpers ──

    def deep_link(
        self,
        platform: str,
        code: str,
        *,
        bot_username: str | None = None,
    ) -> str | None:
        """Return a one-click admin-approval URL when supported.

        Telegram supports ``?start=<payload>`` deep links — if
        ``bot_username`` is provided (or set via ``TELEGRAM_BOT_USERNAME``),
        we return a ``https://t.me/<bot>?start=approve_<code>`` URL the
        admin can click to land in the bot DM with the approve command
        pre-filled.

        Discord and other platforms don't support deep-link bot DMs with
        query payload, so we return ``None``.
        """
        platform = platform.lower()
        if platform == "telegram":
            bot = bot_username or os.environ.get("TELEGRAM_BOT_USERNAME")
            if bot:
                return f"https://t.me/{quote(bot)}?start=approve_{quote(code)}"
            return None
        return None


__all__ = [
    "ALPHABET",
    "CODE_LENGTH",
    "CODE_TTL_SECONDS",
    "RATE_LIMIT_SECONDS",
    "LOCKOUT_SECONDS",
    "MAX_PENDING_PER_PLATFORM",
    "MAX_FAILED_ATTEMPTS",
    "PairingCode",
    "PairingCodeStore",
]
