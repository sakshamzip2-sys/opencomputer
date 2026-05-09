"""Tests for Wave 6.E.17 — _default_spawn delegation + heartbeat tick.

PR #460 shipped the multi-host primitives (HMAC, leases, spawn endpoint,
register CLI) but two genuine wiring gaps remained: ``_default_spawn``
didn't check for remote assignees and the dispatcher loop never
heartbeated pending remote claims. This test file exercises both.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from opencomputer.gateway import kanban_dispatcher as gw_disp
from opencomputer.kanban import db
from opencomputer.kanban import remote_dispatch as rd
from opencomputer.kanban import remote_hosts as rh


@pytest.fixture()
def kanban_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OC_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("OC_KANBAN_DB", raising=False)
    monkeypatch.delenv("OC_KANBAN_BOARD", raising=False)
    monkeypatch.setenv(
        "OC_KANBAN_LOCAL_CALLBACK_URL",
        "http://us:9119/api/plugins/kanban/proxy/callback",
    )
    db.init_db()
    return tmp_path


# ---------------------------------------------------------------------------
# _default_spawn — remote delegation path
# ---------------------------------------------------------------------------


def test_default_spawn_delegates_for_known_remote_slug(kanban_home: Path):
    """A task with assignee='peer/profile' for a registered peer
    should delegate via httpx.post and create a kanban_remote_claims
    row, returning None for the PID."""
    with db.connect() as conn:
        rh.add_remote_host(
            conn, slug="peerX", url="http://x:1", hmac_secret="del-secret",
        )
        task_id = db.create_task(
            conn, title="for-remote", body=None, assignee="peerX/profile-a",
        )
        task = db.get_task(conn, task_id)

    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {
        "remote_task_id": "remote-id-X",
        "lease_until": int(time.time()) + 600,
    }
    with patch("httpx.post", return_value=fake):
        pid = db._default_spawn(task, "/tmp/ws")

    assert pid is None  # remote work — no local PID
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM kanban_remote_claims WHERE local_task_id = ?",
            (task_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["remote_slug"] == "peerX"
    assert rows[0]["remote_task_id"] == "remote-id-X"


def test_default_spawn_raises_for_unknown_peer_slug(kanban_home: Path):
    """An assignee referencing a peer NOT in kanban_remote_hosts must
    raise ValueError so the spawn-failure counter records it (and
    eventually auto-blocks the task after `failure_limit` retries)."""
    with db.connect() as conn:
        task_id = db.create_task(
            conn, title="for-unknown", body=None, assignee="ghost/profile-z",
        )
        task = db.get_task(conn, task_id)

    with pytest.raises(ValueError, match="unknown peer slug"):
        db._default_spawn(task, "/tmp/ws")


def test_default_spawn_raises_when_callback_url_missing(
    kanban_home: Path, monkeypatch,
):
    """Without OC_KANBAN_LOCAL_CALLBACK_URL set, delegation must
    refuse — otherwise the peer would have no way to report back."""
    monkeypatch.delenv("OC_KANBAN_LOCAL_CALLBACK_URL", raising=False)
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="peerY", url="http://y:1")
        task_id = db.create_task(
            conn, title="x", body=None, assignee="peerY/p",
        )
        task = db.get_task(conn, task_id)

    with pytest.raises(ValueError, match="OC_KANBAN_LOCAL_CALLBACK_URL"):
        db._default_spawn(task, "/tmp/ws")


def test_default_spawn_no_slash_assignee_falls_through_to_local(
    kanban_home: Path, monkeypatch,
):
    """assignee='just-profile' (no slash) is a local assignee — must
    fall through to the existing subprocess.Popen path. Patch Popen so
    the test doesn't actually spawn `oc`."""
    with db.connect() as conn:
        task_id = db.create_task(
            conn, title="x", body=None, assignee="local-profile",
        )
        task = db.get_task(conn, task_id)

    fake_proc = MagicMock()
    fake_proc.pid = 9999
    with patch("subprocess.Popen", return_value=fake_proc) as popen:
        pid = db._default_spawn(task, str(kanban_home))

    assert pid == 9999
    # Popen was called with `<oc-resolver> -p local-profile ... chat -q ...`
    # The resolver returns either ["oc"]-shaped (PATH lookup), a sibling
    # of sys.executable, or [sys.executable, "-m", "opencomputer"] —
    # check the trailing argv shape rather than cmd[0] which now varies.
    cmd = popen.call_args.args[0]
    assert "local-profile" in cmd
    assert "chat" in cmd
    assert "-p" in cmd


def test_default_spawn_propagates_remote_dispatch_error(kanban_home: Path):
    """Network errors during delegation should propagate as
    RemoteDispatchError, not silently fall through to the local
    subprocess path (which would corrupt state)."""
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="peerZ", url="http://z:1")
        task_id = db.create_task(
            conn, title="x", body=None, assignee="peerZ/p",
        )
        task = db.get_task(conn, task_id)

    with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
        with pytest.raises(rd.RemoteDispatchError, match="failed"):
            db._default_spawn(task, "/tmp/ws")

    # No claim row should have been written on failure.
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM kanban_remote_claims WHERE local_task_id = ?",
            (task_id,),
        ).fetchall()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Heartbeat tick — gateway loop
# ---------------------------------------------------------------------------


def _seed_pending_claim(
    conn, *, local_task_id: str, slug: str, lease_until: int,
):
    """Insert a kanban_remote_claims row in 'pending' status."""
    now = int(time.time())
    with db.write_txn(conn):
        conn.execute(
            "INSERT INTO kanban_remote_claims "
            "(local_task_id, remote_slug, remote_task_id, leased_at, "
            " lease_until, status, last_heartbeat) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (local_task_id, slug, f"r-{local_task_id}", now, lease_until, now),
        )


def test_heartbeat_tick_refreshes_near_expiry_claim(kanban_home: Path):
    """A claim with lease_until within HEARTBEAT_LEAD_SECONDS gets a
    POST /proxy/heartbeat. The new lease_until from the response is
    written back to the row."""
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="peerH", url="http://h:1")
        tid = db.create_task(conn, title="x", body=None, assignee="peerH/p")
        # Lease expires in 30 seconds — well inside the 60-second lead.
        _seed_pending_claim(
            conn, local_task_id=tid, slug="peerH",
            lease_until=int(time.time()) + 30,
        )

    new_until = int(time.time()) + 600
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"lease_until": new_until}
    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post", return_value=fake) as post:
        loop._tick_heartbeats()

    assert post.call_count == 1
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM kanban_remote_claims WHERE local_task_id = ?",
            (tid,),
        ).fetchone()
    assert row["lease_until"] == new_until


def test_heartbeat_tick_skips_claims_with_comfortable_lease(kanban_home: Path):
    """A claim with lease_until far in the future gets no heartbeat."""
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="peerH2", url="http://h:2")
        tid = db.create_task(conn, title="x", body=None, assignee="peerH2/p")
        # Lease expires in 10 minutes — far outside the 60s lead.
        _seed_pending_claim(
            conn, local_task_id=tid, slug="peerH2",
            lease_until=int(time.time()) + 600,
        )

    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post") as post:
        loop._tick_heartbeats()

    assert post.call_count == 0


def test_heartbeat_tick_handles_no_pending_claims(kanban_home: Path):
    """No-op when the claims table is empty. Must not raise."""
    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post") as post:
        loop._tick_heartbeats()
    assert post.call_count == 0


def test_heartbeat_tick_suppresses_repeat_errors_per_slug(kanban_home: Path):
    """Per audit lens A4: when a peer is down with N near-expiry
    claims, we should heartbeat the FIRST one, fail, then skip the
    rest of THAT slug's claims for this tick (avoid log spam +
    needless network calls). Other slugs are unaffected."""
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="dead", url="http://dead:1")
        rh.add_remote_host(conn, slug="alive", url="http://alive:1")
        for i in range(3):
            tid = db.create_task(
                conn, title=f"d{i}", body=None, assignee="dead/p",
            )
            _seed_pending_claim(
                conn, local_task_id=tid, slug="dead",
                lease_until=int(time.time()) + 30,
            )
        tid_a = db.create_task(conn, title="a", body=None, assignee="alive/p")
        _seed_pending_claim(
            conn, local_task_id=tid_a, slug="alive",
            lease_until=int(time.time()) + 30,
        )

    def _fake_post(url, *_a, **_kw):
        if "dead" in url:
            raise httpx.ConnectError("refused")
        ok = MagicMock()
        ok.status_code = 200
        ok.json.return_value = {"lease_until": int(time.time()) + 600}
        return ok

    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post", side_effect=_fake_post) as post:
        loop._tick_heartbeats()

    # Exactly 2 calls: one to dead (fails, slug suppressed for the
    # remaining 2 dead claims), one to alive (succeeds).
    assert post.call_count == 2


def test_heartbeat_tick_skips_claim_if_host_was_removed(kanban_home: Path):
    """If a host got removed from the registry mid-flight (e.g. user
    ran `oc kanban remote remove`), the heartbeat must skip — never
    raise — and the next tick will see the lease eventually expire
    server-side."""
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="ghost", url="http://g:1")
        tid = db.create_task(conn, title="x", body=None, assignee="ghost/p")
        _seed_pending_claim(
            conn, local_task_id=tid, slug="ghost",
            lease_until=int(time.time()) + 30,
        )
        rh.remove_remote_host(conn, "ghost")

    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post") as post:
        loop._tick_heartbeats()
    assert post.call_count == 0


def test_heartbeat_tick_fails_open_on_unexpected_error(kanban_home: Path):
    """The heartbeat tick must NEVER wedge the dispatcher. If
    list_pending_remote_claims itself raises (e.g. transient SQLite
    error), the tick logs + returns — no raise."""
    loop = gw_disp.KanbanDispatcherLoop()
    with patch.object(rd, "list_pending_remote_claims",
                      side_effect=RuntimeError("boom")):
        # Must NOT raise.
        loop._tick_heartbeats()


def test_heartbeat_tick_only_processes_pending_status(kanban_home: Path):
    """A claim that's already in status='done' or 'failed' is filtered
    out by list_pending_remote_claims — confirm we don't accidentally
    heartbeat them."""
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="peerD", url="http://d:1")
        tid = db.create_task(conn, title="x", body=None, assignee="peerD/p")
        _seed_pending_claim(
            conn, local_task_id=tid, slug="peerD",
            lease_until=int(time.time()) + 30,
        )
        # Flip status to done — the heartbeat must skip it.
        with db.write_txn(conn):
            conn.execute(
                "UPDATE kanban_remote_claims SET status='done' "
                "WHERE local_task_id=?",
                (tid,),
            )

    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post") as post:
        loop._tick_heartbeats()
    assert post.call_count == 0


def test_heartbeat_tick_writes_back_new_lease(kanban_home: Path):
    """End-to-end: near-expiry claim → POST → response.lease_until
    written back to the row + last_heartbeat refreshed to ~now."""
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="peerWB", url="http://w:1")
        tid = db.create_task(conn, title="x", body=None, assignee="peerWB/p")
        _seed_pending_claim(
            conn, local_task_id=tid, slug="peerWB",
            lease_until=int(time.time()) + 30,
        )

    fake = MagicMock()
    fake.status_code = 200
    new_until = int(time.time()) + 999
    fake.json.return_value = {"lease_until": new_until}

    before = int(time.time())
    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post", return_value=fake):
        loop._tick_heartbeats()
    after = int(time.time())

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM kanban_remote_claims WHERE local_task_id = ?",
            (tid,),
        ).fetchone()
    assert row["lease_until"] == new_until
    assert before <= int(row["last_heartbeat"]) <= after


# ---------------------------------------------------------------------------
# run_daemon path also heartbeats (parity with gateway loop)
# ---------------------------------------------------------------------------


def test_run_daemon_helper_heartbeats_remote_claims(kanban_home: Path):
    """``_heartbeat_pending_remote_claims`` (used by the standalone
    `oc kanban daemon` path) must do the same near-expiry refresh."""
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="peerSD", url="http://sd:1")
        tid = db.create_task(conn, title="x", body=None, assignee="peerSD/p")
        _seed_pending_claim(
            conn, local_task_id=tid, slug="peerSD",
            lease_until=int(time.time()) + 30,
        )

    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"lease_until": int(time.time()) + 600}
    with patch("httpx.post", return_value=fake) as post:
        db._heartbeat_pending_remote_claims()
    assert post.call_count == 1
