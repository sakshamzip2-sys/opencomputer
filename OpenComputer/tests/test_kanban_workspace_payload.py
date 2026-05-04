"""Tests for cross-host workspace payload sync (Wave 6.E.15)."""

from __future__ import annotations

import argparse
import base64
import io
import json
import shutil
import tarfile
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
from opencomputer.kanban.workspace_payload import (
    DEFAULT_MAX_BYTES,
    WorkspacePayloadError,
    pack_workspace,
    replace_workspace_atomic,
    unpack_workspace,
)


@pytest.fixture()
def kanban_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OC_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("OC_KANBAN_DB", raising=False)
    monkeypatch.delenv("OC_KANBAN_BOARD", raising=False)
    db.init_db()
    return tmp_path


# ---- pack_workspace ----


def test_pack_roundtrip(tmp_path: Path):
    src = tmp_path / "ws"
    src.mkdir()
    (src / "a.txt").write_text("alpha")
    (src / "sub").mkdir()
    (src / "sub" / "b.txt").write_text("bravo")

    data = pack_workspace(src)
    assert isinstance(data, bytes)
    assert len(data) > 0

    dest = tmp_path / "extract"
    extracted = unpack_workspace(data, dest=dest)
    assert (extracted / "a.txt").read_text() == "alpha"
    assert (extracted / "sub" / "b.txt").read_text() == "bravo"


def test_pack_rejects_missing_path(tmp_path: Path):
    with pytest.raises(WorkspacePayloadError, match="does not exist"):
        pack_workspace(tmp_path / "nope")


def test_pack_rejects_non_directory(tmp_path: Path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(WorkspacePayloadError, match="not a directory"):
        pack_workspace(f)


def test_pack_size_cap_enforced(tmp_path: Path):
    """Cap is on POST-compression size; use random bytes (incompressible)."""
    import os as _os
    src = tmp_path / "big"
    src.mkdir()
    for i in range(20):
        (src / f"f{i}.bin").write_bytes(_os.urandom(2048))
    with pytest.raises(WorkspacePayloadError, match="cap"):
        pack_workspace(src, max_bytes=1024)


def test_pack_strips_uid_gid(tmp_path: Path):
    src = tmp_path / "ws"
    src.mkdir()
    (src / "x.txt").write_text("x")
    data = pack_workspace(src)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            assert m.uid == 0
            assert m.gid == 0
            assert m.uname == ""
            assert m.gname == ""


# ---- unpack_workspace safety ----


def test_unpack_rejects_oversized_payload(tmp_path: Path):
    src = tmp_path / "ws"
    src.mkdir()
    (src / "x").write_text("y")
    data = pack_workspace(src)
    with pytest.raises(WorkspacePayloadError, match="cap"):
        unpack_workspace(data, dest=tmp_path / "out", max_bytes=1)


def test_unpack_sanitizes_absolute_paths(tmp_path: Path):
    """Python 3.12's `data` filter sanitizes absolute paths instead of
    raising. Verify the extracted file lands UNDER dest, not at /etc."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="/etc/escape")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    dest = tmp_path / "out"
    # Should not raise — filter sanitizes (strips leading /).
    unpack_workspace(buf.getvalue(), dest=dest)
    # Critical security check: did anything land at /etc/escape?
    assert not Path("/etc/escape").exists()
    # And: the file landed somewhere under dest (sanitized)
    assert any(p.is_file() for p in dest.rglob("*"))


# ---- replace_workspace_atomic ----


def test_replace_atomic_swaps_directories(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "old.txt").write_text("old")

    replacement = tmp_path / "new"
    replacement.mkdir()
    (replacement / "new.txt").write_text("new")

    replace_workspace_atomic(target, replacement)
    assert (target / "new.txt").read_text() == "new"
    assert not (target / "old.txt").exists()
    assert not replacement.exists()  # got renamed in
    backup = target.with_name("target.old")
    assert not backup.exists()  # cleaned up


def test_replace_atomic_with_no_existing_target(tmp_path: Path):
    target = tmp_path / "fresh"
    replacement = tmp_path / "stuff"
    replacement.mkdir()
    (replacement / "x.txt").write_text("x")
    replace_workspace_atomic(target, replacement)
    assert (target / "x.txt").read_text() == "x"


# ---- delegate path packs payload when sync enabled ----


def test_delegate_includes_workspace_payload_when_sync_on(kanban_home: Path):
    workspace = kanban_home / "ws"
    workspace.mkdir()
    (workspace / "task.md").write_text("work product")

    with db.connect() as conn:
        host = rh.add_remote_host(
            conn, slug="syncpeer", url="http://x:1",
            hmac_secret="syncsecret-xxxxxxxx",
            workspace_sync_enabled=True,
        )
        tid = db.create_task(
            conn, title="dir task", body=None, assignee="syncpeer/p",
            workspace_kind="dir", workspace_path=str(workspace),
        )
        # update workspace_path explicitly
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(workspace), tid),
        )
        conn.commit()
        task = db.get_task(conn, tid)

    captured = {}

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "remote_task_id": "remote-id",
        "lease_until": int(__import__("time").time()) + 600,
    }

    def _capture_post(url, content=None, headers=None, **kwargs):
        captured["body"] = content
        return fake_response

    with patch("httpx.post", side_effect=_capture_post):
        with db.connect() as conn:
            rd.delegate_task_to_remote(
                conn, task=task, host=host, profile="p",
                local_callback_url="http://us/cb",
            )

    body = json.loads(captured["body"])
    assert "workspace_payload_b64" in body
    assert len(body["workspace_payload_b64"]) > 0


def test_delegate_skips_payload_when_sync_off(kanban_home: Path):
    workspace = kanban_home / "ws2"
    workspace.mkdir()
    (workspace / "x").write_text("x")

    with db.connect() as conn:
        host = rh.add_remote_host(
            conn, slug="nosync", url="http://x:1",
            hmac_secret="aaaaa-bbbbb-ccccc",
            workspace_sync_enabled=False,
        )
        tid = db.create_task(
            conn, title="dir task", body=None, assignee="nosync/p",
            workspace_kind="dir", workspace_path=str(workspace),
        )
        task = db.get_task(conn, tid)

    captured = {}
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"remote_task_id": "x", "lease_until": 9_999_999_999}

    def _cap(url, content=None, headers=None, **kw):
        captured["body"] = content
        return fake

    with patch("httpx.post", side_effect=_cap):
        with db.connect() as conn:
            rd.delegate_task_to_remote(
                conn, task=task, host=host, profile="p",
                local_callback_url="http://us/cb",
            )
    body = json.loads(captured["body"])
    assert "workspace_payload_b64" not in body


# ---- /proxy/spawn endpoint with payload ----


@pytest.fixture()
def client(kanban_home: Path) -> TestClient:
    app = build_app(enable_pty=False)
    c = TestClient(app)
    c._token = app.state.session_token
    return c


def test_proxy_spawn_extracts_payload_when_sync_on(
    client: TestClient, kanban_home: Path,
):
    secret = "spawn-payload-aaaa"
    with db.connect() as conn:
        rh.add_remote_host(
            conn, slug="incoming-peer", url="http://x:1",
            hmac_secret=secret, workspace_sync_enabled=True,
        )

    # Build a workspace tarball to send
    src = kanban_home / "src-ws"
    src.mkdir()
    (src / "foo.txt").write_text("hello peer")
    tarball = pack_workspace(src)
    payload_b64 = base64.b64encode(tarball).decode("ascii")

    body = json.dumps({
        "schema_version": 2,
        "task": {
            "id": "src-task-id",
            "title": "with-ws",
            "assignee": "x",
            "workspace_kind": "dir",
        },
        "workspace_payload_b64": payload_b64,
    })
    sig = rh.sign_request(
        secret=secret, method="POST",
        path="/api/plugins/kanban/proxy/spawn", body=body,
    )
    r = client.post(
        "/api/plugins/kanban/proxy/spawn?slug=incoming-peer",
        content=body,
        headers={"Content-Type": "application/json", "X-OC-Signature": sig},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    # The created local task should have a workspace_path under
    # remote-workspaces/<src-task-id>/...
    with db.connect() as conn:
        task = db.get_task(conn, data["remote_task_id"])
    assert task is not None
    assert task.workspace_kind == "dir"
    assert task.workspace_path
    extracted = Path(task.workspace_path)
    assert (extracted / "foo.txt").read_text() == "hello peer"


def test_proxy_spawn_rejects_payload_when_peer_sync_off(
    client: TestClient, kanban_home: Path,
):
    secret = "no-sync-1234567890ab"
    with db.connect() as conn:
        rh.add_remote_host(
            conn, slug="no-sync", url="http://x:1",
            hmac_secret=secret, workspace_sync_enabled=False,
        )

    src = kanban_home / "src-ws-2"
    src.mkdir()
    (src / "x").write_text("x")
    payload_b64 = base64.b64encode(pack_workspace(src)).decode("ascii")

    body = json.dumps({
        "schema_version": 2,
        "task": {"title": "x", "assignee": "x", "workspace_kind": "dir"},
        "workspace_payload_b64": payload_b64,
    })
    sig = rh.sign_request(
        secret=secret, method="POST",
        path="/api/plugins/kanban/proxy/spawn", body=body,
    )
    r = client.post(
        "/api/plugins/kanban/proxy/spawn?slug=no-sync",
        content=body,
        headers={"Content-Type": "application/json", "X-OC-Signature": sig},
    )
    assert r.status_code == 422
    assert "workspace_sync_enabled" in r.text


# ---- callback path applies payload ----


def test_callback_extracts_return_payload(client: TestClient, kanban_home: Path):
    secret = "callback-payload-aaaa"
    with db.connect() as conn:
        rh.add_remote_host(
            conn, slug="cb-peer", url="http://x:1",
            hmac_secret=secret, workspace_sync_enabled=True,
        )

    # Simulate: local task with a dir:<path>, claim row in pending
    target_ws = kanban_home / "local-target-ws"
    target_ws.mkdir()
    (target_ws / "before.txt").write_text("before")

    with db.connect() as conn:
        tid = db.create_task(
            conn, title="cb-task", body=None, assignee="cb-peer/p",
            workspace_kind="dir", workspace_path=str(target_ws),
        )
        with db.write_txn(conn):
            conn.execute(
                "INSERT INTO kanban_remote_claims "
                "(local_task_id, remote_slug, remote_task_id, leased_at, "
                " lease_until, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                (tid, "cb-peer", "remote-cb-1",
                 int(__import__("time").time()),
                 int(__import__("time").time()) + 300),
            )

    # Build a "modified" workspace tarball
    modified = kanban_home / "modified-ws"
    modified.mkdir()
    (modified / "after.txt").write_text("after")
    return_b64 = base64.b64encode(pack_workspace(modified)).decode("ascii")

    body = json.dumps({
        "schema_version": 2,
        "remote_task_id": "remote-cb-1",
        "outcome": "done",
        "summary": "did the thing",
        "workspace_payload_b64": return_b64,
    })
    sig = rh.sign_request(
        secret=secret, method="POST",
        path="/api/plugins/kanban/proxy/callback", body=body,
    )
    r = client.post(
        "/api/plugins/kanban/proxy/callback?slug=cb-peer",
        content=body,
        headers={"Content-Type": "application/json", "X-OC-Signature": sig},
    )
    assert r.status_code == 200, r.text
    # Local target dir should have the after.txt now (atomic swap)
    assert (target_ws / "after.txt").read_text() == "after"
    assert not (target_ws / "before.txt").exists()


# ---- CLI roundtrip ----


def _run_cli(*argv: str) -> tuple[int, str]:
    from opencomputer.kanban import cli as kbcli
    parser = argparse.ArgumentParser(prog="oc", add_help=False)
    sub = parser.add_subparsers(dest="cmd")
    kbcli.build_parser(sub)
    parsed = parser.parse_args(["kanban", *argv])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = kbcli.kanban_command(parsed) or 0
    return rc, buf.getvalue()


def test_cli_remote_add_with_workspace_sync_flag(kanban_home: Path):
    rc, out = _run_cli(
        "remote", "add", "p1", "http://p1:9119",
        "--secret", "preshared", "--enable-workspace-sync",
    )
    assert rc == 0
    assert "workspace_sync=on" in out
    rc, out = _run_cli("remote", "list")
    assert rc == 0
    assert "p1" in out
    assert "on" in out


def test_cli_remote_set_workspace_sync_toggles(kanban_home: Path):
    _run_cli("remote", "add", "p2", "http://p2:9119",
             "--secret", "preshared")
    rc, out = _run_cli("remote", "set-workspace-sync", "p2", "on")
    assert rc == 0
    assert "→ on" in out
    with db.connect() as conn:
        host = rh.find_remote_host(conn, "p2")
    assert host.workspace_sync_enabled is True
    rc, _ = _run_cli("remote", "set-workspace-sync", "p2", "off")
    assert rc == 0


def test_cli_set_workspace_sync_unknown_peer_fails(kanban_home: Path):
    rc, _ = _run_cli("remote", "set-workspace-sync", "ghost", "on")
    assert rc == 1
