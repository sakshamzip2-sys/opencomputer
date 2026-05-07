"""Tests for the DM pairing-code store (Task 1.4).

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (T1.4)
Spec: docs/superpowers/specs/2026-05-08-messaging-gateway-parity-design.md (§5.2)
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from opencomputer.channels.pairing_codes import (
    ALPHABET,
    CODE_LENGTH,
    CODE_TTL_SECONDS,
    LOCKOUT_SECONDS,
    MAX_FAILED_ATTEMPTS,
    MAX_PENDING_PER_PLATFORM,
    RATE_LIMIT_SECONDS,
    PairingCodeStore,
)


# ── Code minting ────────────────────────────────────────────────────────────


def test_generate_code_returns_8_char_from_alphabet(tmp_path):
    store = PairingCodeStore(tmp_path)
    code = store.generate_code("telegram", "user_a")
    assert code is not None
    assert len(code) == CODE_LENGTH
    assert all(c in ALPHABET for c in code)
    # Must NOT contain ambiguous characters.
    assert "0" not in code
    assert "O" not in code
    assert "1" not in code
    assert "I" not in code


def test_alphabet_is_unambiguous():
    assert "0" not in ALPHABET
    assert "O" not in ALPHABET
    assert "1" not in ALPHABET
    assert "I" not in ALPHABET
    assert len(ALPHABET) == 32


# ── Approve / revoke flow ───────────────────────────────────────────────────


def test_approve_flow_marks_user_approved(tmp_path):
    store = PairingCodeStore(tmp_path)
    code = store.generate_code("telegram", "user_a", "Alice")
    result = store.approve_code("telegram", code)
    assert result == {"user_id": "user_a", "user_name": "Alice"}
    assert store.is_approved("telegram", "user_a")


def test_approve_unknown_code_returns_none(tmp_path):
    store = PairingCodeStore(tmp_path)
    assert store.approve_code("telegram", "ABCDEFGH") is None


def test_revoke_after_approve_clears_approval(tmp_path):
    store = PairingCodeStore(tmp_path)
    code = store.generate_code("telegram", "user_a", "Alice")
    store.approve_code("telegram", code)
    assert store.is_approved("telegram", "user_a")
    assert store.revoke("telegram", "user_a") is True
    assert not store.is_approved("telegram", "user_a")


def test_revoke_unknown_user_returns_false(tmp_path):
    store = PairingCodeStore(tmp_path)
    assert store.revoke("telegram", "ghost") is False


# ── Rate limiting ───────────────────────────────────────────────────────────


def test_rate_limit_blocks_second_request_within_window(tmp_path):
    store = PairingCodeStore(tmp_path)
    first = store.generate_code("telegram", "user_a")
    second = store.generate_code("telegram", "user_a")
    assert first is not None
    assert second is None  # rate-limited


def test_rate_limit_separate_users_independent(tmp_path):
    store = PairingCodeStore(tmp_path)
    a = store.generate_code("telegram", "user_a")
    b = store.generate_code("telegram", "user_b")
    assert a is not None
    assert b is not None


# ── Max-pending cap ─────────────────────────────────────────────────────────


def test_max_pending_per_platform_enforced(tmp_path):
    store = PairingCodeStore(tmp_path)
    # Mint exactly MAX_PENDING distinct user codes — all succeed.
    minted = []
    for i in range(MAX_PENDING_PER_PLATFORM):
        code = store.generate_code("telegram", f"user_{i}")
        minted.append(code)
    assert all(c is not None for c in minted)
    # Next request hits the cap.
    overflow = store.generate_code("telegram", "user_overflow")
    assert overflow is None


# ── Lockout ─────────────────────────────────────────────────────────────────


def test_lockout_after_failed_approvals(tmp_path):
    store = PairingCodeStore(tmp_path)
    for _ in range(MAX_FAILED_ATTEMPTS):
        store.approve_code("telegram", "BADCODE0")
    # All subsequent generate_code calls return None — platform locked.
    assert store.generate_code("telegram", "user_z") is None


# ── Regenerate ──────────────────────────────────────────────────────────────


def test_regenerate_bypasses_rate_limit(tmp_path):
    store = PairingCodeStore(tmp_path)
    first = store.generate_code("telegram", "user_a")
    assert first is not None
    # Standard generate_code is rate-limited.
    assert store.generate_code("telegram", "user_a") is None
    # regenerate_code bypasses the rate limit.
    fresh = store.regenerate_code("telegram", "user_a")
    assert fresh is not None
    assert fresh != first


def test_regenerate_honors_lockout(tmp_path):
    store = PairingCodeStore(tmp_path)
    for _ in range(MAX_FAILED_ATTEMPTS):
        store.approve_code("telegram", "BADCODE0")
    assert store.regenerate_code("telegram", "user_a") is None


# ── Listing ─────────────────────────────────────────────────────────────────


def test_list_pending_returns_age_minutes(tmp_path):
    store = PairingCodeStore(tmp_path)
    store.generate_code("telegram", "user_a", "Alice")
    rows = store.list_pending("telegram")
    assert len(rows) == 1
    assert rows[0]["platform"] == "telegram"
    assert rows[0]["user_id"] == "user_a"
    assert rows[0]["user_name"] == "Alice"
    assert "code" in rows[0]
    assert rows[0]["age_minutes"] >= 0


def test_list_approved_cross_platform(tmp_path):
    store = PairingCodeStore(tmp_path)
    code1 = store.generate_code("telegram", "user_a", "Alice")
    store.approve_code("telegram", code1)
    code2 = store.generate_code("discord", "user_d", "Diana")
    store.approve_code("discord", code2)

    all_approved = store.list_approved()
    platforms = {row["platform"] for row in all_approved}
    assert {"telegram", "discord"} <= platforms

    only_tg = store.list_approved("telegram")
    assert len(only_tg) == 1
    assert only_tg[0]["user_id"] == "user_a"


# ── Clear pending ──────────────────────────────────────────────────────────


def test_clear_pending_returns_count(tmp_path):
    store = PairingCodeStore(tmp_path)
    store.generate_code("telegram", "u1")
    store.generate_code("telegram", "u2")
    count = store.clear_pending("telegram")
    assert count == 2
    assert store.list_pending("telegram") == []


# ── Expired sweep ──────────────────────────────────────────────────────────


def test_expired_sweep_drops_old_codes(tmp_path, monkeypatch):
    store = PairingCodeStore(tmp_path)
    # Mint a code, then rewind its created_at to >TTL ago by editing the file.
    store.generate_code("telegram", "user_a")
    pending_path = tmp_path / "pairing" / "telegram-pending.json"
    data = json.loads(pending_path.read_text(encoding="utf-8"))
    for code in data:
        data[code]["created_at"] = time.time() - CODE_TTL_SECONDS - 60
    pending_path.write_text(json.dumps(data), encoding="utf-8")

    removed = store.expired_sweep_all()
    assert removed == 1
    assert store.list_pending("telegram") == []


# ── Corruption recovery ────────────────────────────────────────────────────


def test_corrupt_pending_recovered_with_backup(tmp_path):
    store = PairingCodeStore(tmp_path)
    bad = tmp_path / "pairing" / "telegram-pending.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not valid json {{{", encoding="utf-8")
    # generate_code recovers — sees empty + can mint.
    code = store.generate_code("telegram", "user_a")
    assert code is not None
    # Backup file created.
    backups = list(bad.parent.glob("telegram-pending.json.corrupt.*"))
    assert len(backups) == 1


# ── File permissions (POSIX only) ──────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_pending_file_has_0600_perms(tmp_path):
    store = PairingCodeStore(tmp_path)
    store.generate_code("telegram", "user_a")
    p = tmp_path / "pairing" / "telegram-pending.json"
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600


# ── Atomic write (no partial reads) ────────────────────────────────────────


def test_atomic_write_no_temp_files_left_behind(tmp_path):
    store = PairingCodeStore(tmp_path)
    store.generate_code("telegram", "user_a")
    pdir = tmp_path / "pairing"
    leftovers = [p for p in pdir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


# ── Threading: two threads can't double-mint past cap ──────────────────────


def test_threading_no_overshoot_max_pending(tmp_path):
    import threading

    store = PairingCodeStore(tmp_path)
    minted: list = []

    def worker(uid: str):
        c = store.generate_code("telegram", uid)
        if c is not None:
            minted.append(c)

    threads = [threading.Thread(target=worker, args=(f"u{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # No matter how many threads raced, at most MAX_PENDING_PER_PLATFORM
    # codes were minted (RLock + flock serialize).
    assert len(minted) == MAX_PENDING_PER_PLATFORM


# ── Cross-process locking (POSIX only — flock semantics) ───────────────────


def _generate_in_subprocess(profile: str, platform: str, user_id: str, q):
    """Subprocess worker for the cross-process flock test."""
    from opencomputer.channels.pairing_codes import PairingCodeStore

    store = PairingCodeStore(Path(profile))
    q.put(store.generate_code(platform, user_id))


@pytest.mark.skipif(sys.platform == "win32", reason="flock POSIX-only path")
def test_cross_process_flock_no_overshoot(tmp_path):
    """Two distinct PROCESSES race generate_code on the same store — flock
    serializes so the cap is honored."""
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [
        ctx.Process(target=_generate_in_subprocess, args=(str(tmp_path), "telegram", f"u{i}", q))
        for i in range(8)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
    minted = []
    while not q.empty():
        r = q.get_nowait()
        if r is not None:
            minted.append(r)
    assert len(minted) == MAX_PENDING_PER_PLATFORM


# ── Deep-link generation ───────────────────────────────────────────────────


def test_deep_link_telegram_with_username(tmp_path, monkeypatch):
    store = PairingCodeStore(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "MyBot")
    url = store.deep_link("telegram", "ABCDEFGH")
    assert url == "https://t.me/MyBot?start=approve_ABCDEFGH"


def test_deep_link_telegram_no_username_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)
    store = PairingCodeStore(tmp_path)
    assert store.deep_link("telegram", "ABCDEFGH") is None


def test_deep_link_other_platform_returns_none(tmp_path):
    store = PairingCodeStore(tmp_path)
    assert store.deep_link("discord", "ABCDEFGH") is None
    assert store.deep_link("slack", "ABCDEFGH") is None
