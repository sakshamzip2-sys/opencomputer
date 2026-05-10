"""Tests for the dashboard ``POST /api/plugins/kanban/tasks/{id}/specify``
endpoint (Hermes Doc-2 parity, 2026-05-08).

The dashboard wraps :func:`opencomputer.kanban.specify.specify_task` and
exposes it as a per-task verb. Tests cover error mapping (404 vs 409
vs 502) and the happy-path response shape, with the LLM call stubbed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencomputer.dashboard import build_app
from opencomputer.kanban import db as kb
from opencomputer.kanban import specify as sp


@pytest.fixture()
def tmp_oc_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def client(tmp_oc_home: Path) -> TestClient:
    app = build_app(enable_pty=False)
    return TestClient(app)


@pytest.fixture()
def kanban_db_path(tmp_oc_home: Path) -> Path:
    """Point the kanban code at a per-test DB so concurrent runs don't collide."""
    db_path = tmp_oc_home / "kanban-test.sqlite"
    os.environ["OC_KANBAN_DB"] = str(db_path)
    yield db_path
    os.environ.pop("OC_KANBAN_DB", None)


def _make_triage_task(title: str = "test idea") -> str:
    conn = kb.connect()
    try:
        return kb.create_task(
            conn, title=title, body="rough", assignee=None, triage=True,
        )
    finally:
        conn.close()


def test_specify_endpoint_happy_path(
    client: TestClient, kanban_db_path: Path, monkeypatch,
) -> None:
    tid = _make_triage_task()

    expanded = (
        "## Goal\nDone.\n\n## Approach\n- a\n- b\n\n"
        "## Definition of Done\n- ok\n\n## Out of scope\n- nothing"
    )

    async def _fake(prompt: str) -> str:
        return expanded

    monkeypatch.setattr(sp, "_call_specifier_model", _fake)

    r = client.post(f"/api/plugins/kanban/tasks/{tid}/specify", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == tid
    assert body["old_status"] == "triage"
    assert body["new_status"] == "todo"
    assert body["truncated"] is False
    assert "## Goal" in body["expanded_body"]


def test_specify_endpoint_returns_404_on_missing_task(
    client: TestClient, kanban_db_path: Path,
) -> None:
    r = client.post(
        "/api/plugins/kanban/tasks/t_does_not_exist/specify", json={},
    )
    assert r.status_code == 404


def test_specify_endpoint_returns_409_on_non_triage(
    client: TestClient, kanban_db_path: Path, monkeypatch,
) -> None:
    tid = _make_triage_task()

    async def _fake(prompt: str) -> str:
        return "## Goal\nx\n## Approach\n- y\n## Definition of Done\n- z\n## Out of scope\n- w"

    monkeypatch.setattr(sp, "_call_specifier_model", _fake)

    # Successful first specify → status moves to todo.
    r1 = client.post(f"/api/plugins/kanban/tasks/{tid}/specify", json={})
    assert r1.status_code == 200

    # Second call should 409 (not in triage anymore).
    r2 = client.post(f"/api/plugins/kanban/tasks/{tid}/specify", json={})
    assert r2.status_code == 409
    assert "not triage" in r2.json()["detail"]


def test_specify_endpoint_returns_502_on_provider_failure(
    client: TestClient, kanban_db_path: Path, monkeypatch,
) -> None:
    tid = _make_triage_task()

    async def _broken(prompt: str) -> str:
        raise RuntimeError("simulated transport error")

    monkeypatch.setattr(sp, "_call_specifier_model", _broken)

    r = client.post(f"/api/plugins/kanban/tasks/{tid}/specify", json={})
    assert r.status_code == 502
    assert "aux model call failed" in r.json()["detail"]


def test_specify_endpoint_rejects_invalid_promote_to(
    client: TestClient, kanban_db_path: Path,
) -> None:
    tid = _make_triage_task()
    r = client.post(
        f"/api/plugins/kanban/tasks/{tid}/specify",
        json={"promote_to": "running"},  # not allowed
    )
    assert r.status_code == 400
