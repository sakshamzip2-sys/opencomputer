"""Tests for Wave 6.E.13 — multi-host kanban write coordination."""

from __future__ import annotations

import argparse
import io
import json
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from opencomputer.dashboard import build_app
from opencomputer.kanban import db
from opencomputer.kanban import remote_dispatch as rd
from opencomputer.kanban import remote_hosts as rh


@pytest.fixture()
def kanban_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OC_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("OC_KANBAN_DB", raising=False)
    monkeypatch.delenv("OC_KANBAN_BOARD", raising=False)
    db.init_db()
    return tmp_path


# ---- HMAC primitives ----


def test_sign_then_verify_roundtrip():
    sig = rh.sign_request(secret="abc", method="POST", path="/x", body=b"hello")
    rh.verify_request(sig, secret="abc", method="POST", path="/x", body=b"hello")


def test_verify_rejects_wrong_secret():
    sig = rh.sign_request(secret="abc", method="POST", path="/x", body=b"")
    with pytest.raises(rh.HmacAuthError, match="signature does not match"):
        rh.verify_request(sig, secret="bad", method="POST", path="/x", body=b"")


def test_verify_rejects_tampered_body():
    sig = rh.sign_request(secret="abc", method="POST", path="/x", body=b"orig")
    with pytest.raises(rh.HmacAuthError, match="signature does not match"):
        rh.verify_request(sig, secret="abc", method="POST", path="/x", body=b"tampered")


def test_verify_rejects_outside_replay_window():
    """Timestamp older than 300s should be rejected."""
    old_ts = int(time.time()) - 1000
    sig = rh.sign_request(
        secret="abc", method="POST", path="/x", body=b"",
        timestamp=old_ts,
    )
    with pytest.raises(rh.HmacAuthError, match="replay window"):
        rh.verify_request(sig, secret="abc", method="POST", path="/x", body=b"")


def test_verify_rejects_missing_header():
    with pytest.raises(rh.HmacAuthError, match="missing"):
        rh.verify_request(None, secret="abc", method="POST", path="/x", body=b"")


def test_verify_rejects_malformed_header():
    with pytest.raises(rh.HmacAuthError):
        rh.verify_request("garbage", secret="abc", method="POST", path="/x", body=b"")


def test_verify_rejects_unsupported_version():
    with pytest.raises(rh.HmacAuthError, match="version"):
        rh.verify_request("v9:0:abc", secret="abc", method="POST", path="/x", body=b"")


# ---- remote-host registry ----


def test_add_remote_host_generates_secret(kanban_home: Path):
    with db.connect() as conn:
        host = rh.add_remote_host(conn, slug="peer1", url="http://x:1")
    assert len(host.hmac_secret) >= 32  # token_urlsafe(32) → ~43 chars


def test_add_then_list_then_remove(kanban_home: Path):
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="alpha", url="http://a:1")
        rh.add_remote_host(conn, slug="bravo", url="http://b:1")
        hosts = rh.list_remote_hosts(conn)
    assert {h.slug for h in hosts} == {"alpha", "bravo"}
    with db.connect() as conn:
        assert rh.remove_remote_host(conn, "alpha") is True
        assert rh.remove_remote_host(conn, "alpha") is False  # idempotent


def test_find_remote_host(kanban_home: Path):
    with db.connect() as conn:
        rh.add_remote_host(
            conn, slug="findme", url="http://f:1", hmac_secret="known",
        )
        host = rh.find_remote_host(conn, "findme")
    assert host is not None
    assert host.hmac_secret == "known"
    assert rh.find_remote_host(conn, "missing") is None


# ---- remote_dispatch helpers ----


def test_parse_remote_assignee_recognizes_slug_profile():
    assert rd.parse_remote_assignee("peer1/profile-a") == ("peer1", "profile-a")


def test_parse_remote_assignee_returns_none_for_local():
    assert rd.parse_remote_assignee("just-profile") is None
    assert rd.parse_remote_assignee(None) is None
    assert rd.parse_remote_assignee("") is None


def test_parse_remote_assignee_rejects_bad_slug():
    # Special chars not allowed in slug part
    assert rd.parse_remote_assignee("bad slug/profile") is None


# ---- inbound endpoints (HTTP boundary) ----


@pytest.fixture()
def client(kanban_home: Path) -> TestClient:
    app = build_app(enable_pty=False)
    c = TestClient(app)
    c._token = app.state.session_token
    return c


def test_proxy_spawn_rejects_missing_signature(client: TestClient, kanban_home: Path):
    with db.connect() as conn:
        rh.add_remote_host(
            conn, slug="peer1", url="http://x:1", hmac_secret="topsecret",
        )
    body = json.dumps({
        "schema_version": 2,
        "task": {"title": "T", "assignee": "remote-prof"},
    })
    r = client.post(
        "/api/plugins/kanban/proxy/spawn?slug=peer1",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 401


def test_proxy_spawn_rejects_unknown_slug(client: TestClient, kanban_home: Path):
    body = json.dumps({"task": {"title": "T", "assignee": "p"}})
    sig = rh.sign_request(
        secret="anything", method="POST",
        path="/api/plugins/kanban/proxy/spawn", body=body,
    )
    r = client.post(
        "/api/plugins/kanban/proxy/spawn?slug=ghost-peer",
        content=body,
        headers={"Content-Type": "application/json", "X-OC-Signature": sig},
    )
    assert r.status_code == 401


def test_proxy_spawn_creates_local_task(client: TestClient, kanban_home: Path):
    secret = "shared-secret-aaaaaaaaaaaa"
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="peerA", url="http://a:1", hmac_secret=secret)
    body = json.dumps({
        "schema_version": 2,
        "task": {
            "title": "delegated",
            "body": "do thing",
            "assignee": "local-prof",
            "priority": 5,
            "tenant": "t1",
            "workspace_kind": "scratch",
        },
        "callback_url": "http://peerA:1/api/plugins/kanban/proxy/callback",
    })
    sig = rh.sign_request(
        secret=secret, method="POST",
        path="/api/plugins/kanban/proxy/spawn", body=body,
    )
    r = client.post(
        "/api/plugins/kanban/proxy/spawn?slug=peerA",
        content=body,
        headers={"Content-Type": "application/json", "X-OC-Signature": sig},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "remote_task_id" in data
    assert data["lease_until"] > int(time.time())
    # Local task got created with the right metadata
    with db.connect() as conn:
        task = db.get_task(conn, data["remote_task_id"])
    assert task is not None
    assert task.title == "delegated"
    assert task.assignee == "local-prof"
    assert task.priority == 5


def test_proxy_heartbeat_extends_lease(client: TestClient, kanban_home: Path):
    secret = "heartbeat-secret-aaaa"
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="peerB", url="http://b:1", hmac_secret=secret)
        tid = db.create_task(conn, title="for-hb", body=None, assignee="x")
    body = json.dumps({"remote_task_id": tid})
    sig = rh.sign_request(
        secret=secret, method="POST",
        path="/api/plugins/kanban/proxy/heartbeat", body=body,
    )
    r = client.post(
        "/api/plugins/kanban/proxy/heartbeat?slug=peerB",
        content=body,
        headers={"Content-Type": "application/json", "X-OC-Signature": sig},
    )
    assert r.status_code == 200
    assert r.json()["lease_until"] > int(time.time())


def test_proxy_callback_reconciles_done(client: TestClient, kanban_home: Path):
    secret = "cb-secret-abcdef"
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="peerC", url="http://c:1", hmac_secret=secret)
        # Create a local task + claim row
        tid = db.create_task(conn, title="delegated", body=None, assignee="x")
        with db.write_txn(conn):
            conn.execute(
                "INSERT INTO kanban_remote_claims "
                "(local_task_id, remote_slug, remote_task_id, leased_at, "
                " lease_until, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                (tid, "peerC", "peer-side-id", int(time.time()),
                 int(time.time()) + 300),
            )
    body = json.dumps({
        "schema_version": 2,
        "remote_task_id": "peer-side-id",
        "outcome": "done",
        "summary": "all good",
    })
    sig = rh.sign_request(
        secret=secret, method="POST",
        path="/api/plugins/kanban/proxy/callback", body=body,
    )
    r = client.post(
        "/api/plugins/kanban/proxy/callback?slug=peerC",
        content=body,
        headers={"Content-Type": "application/json", "X-OC-Signature": sig},
    )
    assert r.status_code == 200
    with db.connect() as conn:
        task = db.get_task(conn, tid)
        claim_row = conn.execute(
            "SELECT * FROM kanban_remote_claims WHERE local_task_id = ?",
            (tid,),
        ).fetchone()
    assert task.status == "done"
    assert claim_row["status"] == "done"


def test_proxy_callback_unknown_remote_id_422(client: TestClient, kanban_home: Path):
    secret = "cb-rejects-aaaaaa"
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="peerD", url="http://d:1", hmac_secret=secret)
    body = json.dumps({
        "remote_task_id": "no-such-claim",
        "outcome": "done",
    })
    sig = rh.sign_request(
        secret=secret, method="POST",
        path="/api/plugins/kanban/proxy/callback", body=body,
    )
    r = client.post(
        "/api/plugins/kanban/proxy/callback?slug=peerD",
        content=body,
        headers={"Content-Type": "application/json", "X-OC-Signature": sig},
    )
    assert r.status_code == 422


# ---- delegate_task_to_remote (outbound) ----


def test_delegate_task_to_remote_records_claim(kanban_home: Path):
    with db.connect() as conn:
        host = rh.add_remote_host(
            conn, slug="peerE", url="http://e:1", hmac_secret="del-secret",
        )
        task = db.get_task(conn, db.create_task(
            conn, title="for-delegate", body=None, assignee="peerE/p",
        ))

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "remote_task_id": "remote-id-123",
        "lease_until": int(time.time()) + 600,
    }
    with patch("httpx.post", return_value=fake_response):
        with db.connect() as conn:
            claim = rd.delegate_task_to_remote(
                conn, task=task, host=host, profile="p",
                local_callback_url="http://us:9119/cb",
            )
    assert claim.remote_task_id == "remote-id-123"
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM kanban_remote_claims WHERE local_task_id = ?",
            (task.id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


def test_delegate_raises_on_network_error(kanban_home: Path):
    with db.connect() as conn:
        host = rh.add_remote_host(conn, slug="peerF", url="http://f:1")
        task = db.get_task(conn, db.create_task(
            conn, title="x", body=None, assignee="peerF/p",
        ))
    with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
        with db.connect() as conn:
            with pytest.raises(rd.RemoteDispatchError, match="failed"):
                rd.delegate_task_to_remote(
                    conn, task=task, host=host, profile="p",
                    local_callback_url="http://us:9119/cb",
                )


def test_delegate_raises_on_401(kanban_home: Path):
    with db.connect() as conn:
        host = rh.add_remote_host(conn, slug="peerG", url="http://g:1")
        task = db.get_task(conn, db.create_task(
            conn, title="x", body=None, assignee="peerG/p",
        ))
    fake = MagicMock()
    fake.status_code = 401
    fake.text = "bad sig"
    with patch("httpx.post", return_value=fake):
        with db.connect() as conn:
            with pytest.raises(rd.RemoteDispatchError, match="401"):
                rd.delegate_task_to_remote(
                    conn, task=task, host=host, profile="p",
                    local_callback_url="http://us:9119/cb",
                )


# ---- CLI roundtrip ----


def _run_cli(verb: str, *argv: str) -> tuple[int, str, str]:
    from opencomputer.kanban import cli as kbcli
    parser = argparse.ArgumentParser(prog="oc", add_help=False)
    sub = parser.add_subparsers(dest="cmd")
    kbcli.build_parser(sub)
    parsed = parser.parse_args(["kanban", verb, *argv])
    out_buf = io.StringIO()
    with redirect_stdout(out_buf):
        rc = kbcli.kanban_command(parsed) or 0
    return rc, out_buf.getvalue(), ""


def test_cli_remote_add_list_rm(kanban_home: Path):
    rc, out, _ = _run_cli(
        "remote", "add", "alpha", "http://alpha:9119",
    )
    assert rc == 0
    assert "alpha" in out
    rc, out, _ = _run_cli("remote", "list")
    assert rc == 0
    assert "alpha" in out
    rc, _, _ = _run_cli("remote", "rm", "alpha")
    assert rc == 0


def test_cli_remote_add_with_explicit_secret(kanban_home: Path):
    rc, out, _ = _run_cli(
        "remote", "add", "beta", "http://b:1",
        "--secret", "preshared",
    )
    assert rc == 0
    # When --secret given, we shouldn't print a generated one
    assert "preshared" not in out
