"""Kanban dashboard plugin — backend API routes.

Mounted at /api/plugins/kanban/ by the dashboard plugin system.

This layer is intentionally thin: every handler is a small wrapper around
``opencomputer.kanban.kanban_db`` or a direct SQL query. Writes use the same code
paths the CLI and gateway ``/kanban`` command use, so the three surfaces
cannot drift.

Live updates arrive via the ``/events`` WebSocket, which tails the
append-only ``task_events`` table on a short poll interval (WAL mode lets
reads run alongside the dispatcher's IMMEDIATE write transactions).

Security note
-------------
The dashboard's HTTP auth middleware (``web_server.auth_middleware``)
explicitly skips ``/api/plugins/`` — plugin routes are unauthenticated by
design because the dashboard binds to localhost by default. For the
WebSocket we still require the session token as a ``?token=`` query
parameter (browsers cannot set the ``Authorization`` header on an upgrade
request), matching the established pattern used by the in-browser PTY
bridge in ``opencomputer.kanban/web_server.py``. If you run the dashboard with
``--host 0.0.0.0``, every plugin route — kanban included — becomes
reachable from the network. Don't do that on a shared host.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import sqlite3
import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi import status as http_status
from pydantic import BaseModel, Field

from opencomputer.kanban import db as kanban_db

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth helper — WebSocket only (HTTP routes live behind the dashboard's
# existing plugin-bypass; this is documented above).
# ---------------------------------------------------------------------------

def _check_ws_token(provided: str | None) -> bool:
    """Constant-time compare against the dashboard session token.

    Imported lazily so the plugin still loads in test contexts where the
    dashboard web_server module isn't importable (e.g. the bare-FastAPI
    test harness).
    """
    if not provided:
        return False
    try:
        from opencomputer.kanban import web_server as _ws
    except Exception:
        # No dashboard context (tests). Accept so the tail loop is still
        # testable; in production the dashboard module always imports
        # cleanly because it's the caller.
        return True
    expected = getattr(_ws, "_SESSION_TOKEN", None)
    if not expected:
        return True
    return hmac.compare_digest(str(provided), str(expected))


def _conn():
    """Open a kanban_db connection, creating the schema on first use.

    Every handler that mutates the DB goes through this so the plugin
    self-heals on a fresh install (no user-visible "no such table"
    error if somebody hits POST /tasks before GET /board).
    ``init_db`` is idempotent.
    """
    try:
        kanban_db.init_db()
    except Exception as exc:
        log.warning("kanban init_db failed: %s", exc)
    return kanban_db.connect()


# ---------------------------------------------------------------------------
# Wave 6.E.11 — Remote-board read proxy (Hermes 'multi-host coordination'
# minimum). Other hosts can read this host's boards over HTTP for
# monitoring purposes. WRITE operations are intentionally NOT exposed —
# distributed claim coordination + clock-skew handling are multi-week
# projects this PR does not attempt.
# ---------------------------------------------------------------------------


def _proxy_conn(slug: str | None):
    """Open a read-only connection to the named board (or default).

    Uses a short-lived sqlite3 connection so we don't pollute the
    long-lived per-thread connection cache. Slug validation surfaces
    a 400 to the caller.
    """
    import sqlite3

    if slug is not None:
        try:
            kanban_db.validate_slug(slug)
        except kanban_db.InvalidBoardSlugError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        path = kanban_db.board_db_path(slug)
    else:
        path = kanban_db.kanban_db_path()
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"board {slug or '(default)'!r} has no kanban.db at {path}",
        )
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/proxy/health")
def proxy_health():
    """Cheap reachability probe for remote-read clients.

    Returns the schema-version envelope + the list of known boards
    so a remote client can show which slugs are accessible without
    polling each individually.
    """
    return {
        "schema_version": 1,
        "default_board_path": str(kanban_db.kanban_db_path()),
        "boards": kanban_db.list_boards(),
        "active_board": kanban_db.active_board(),
    }


@router.get("/proxy/board")
def proxy_board(
    slug: str | None = Query(None, description="Board slug; default = active board"),
    tenant: str | None = Query(None),
    include_archived: bool = Query(False),
):
    """Read-only board snapshot for a remote viewer.

    Wraps the response in a ``{schema_version, rows}`` envelope so
    future schema additions are backward-compatible (audit lens A7).
    """
    conn = _proxy_conn(slug)
    try:
        tasks = kanban_db.list_tasks(
            conn, tenant=tenant, include_archived=include_archived,
        )
        rows = []
        for t in tasks:
            rows.append({
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "assignee": t.assignee,
                "priority": t.priority,
                "tenant": t.tenant,
                "created_at": t.created_at,
                "completed_at": t.completed_at,
                "result": t.result,
            })
    finally:
        conn.close()
    return {
        "schema_version": 1,
        "slug": slug or kanban_db.active_board(),
        "tasks": rows,
    }


@router.get("/proxy/task/{task_id}")
def proxy_task(
    task_id: str,
    slug: str | None = Query(None),
):
    """Read-only single-task view for a remote viewer."""
    conn = _proxy_conn(slug)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(
                status_code=404,
                detail=f"task {task_id!r} not found in board {slug or '(default)'!r}",
            )
        comments = [
            {
                "id": c["id"], "author": c["author"], "body": c["body"],
                "created_at": c["created_at"],
            }
            for c in conn.execute(
                "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "schema_version": 1,
        "task": {
            "id": task.id,
            "title": task.title,
            "body": task.body,
            "status": task.status,
            "assignee": task.assignee,
            "priority": task.priority,
            "tenant": task.tenant,
            "created_at": task.created_at,
            "completed_at": task.completed_at,
            "result": task.result,
        },
        "comments": comments,
    }


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

# Columns shown by the dashboard, in left-to-right order. "archived" is
# available via a filter toggle rather than a visible column.
BOARD_COLUMNS: list[str] = [
    "triage", "todo", "ready", "running", "blocked", "done",
]


def _task_dict(task: kanban_db.Task) -> dict[str, Any]:
    d = asdict(task)
    # Add derived age metrics so the UI can colour stale cards without
    # computing deltas client-side.
    d["age"] = kanban_db.task_age(task)
    # Keep body short on list endpoints; full body comes from /tasks/:id.
    return d


def _event_dict(event: kanban_db.Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "kind": event.kind,
        "payload": event.payload,
        "created_at": event.created_at,
        "run_id": event.run_id,
    }


def _comment_dict(c: kanban_db.Comment) -> dict[str, Any]:
    return {
        "id": c.id,
        "task_id": c.task_id,
        "author": c.author,
        "body": c.body,
        "created_at": c.created_at,
    }


def _run_dict(r: kanban_db.Run) -> dict[str, Any]:
    """Serialise a Run for the drawer's Run history section."""
    return {
        "id": r.id,
        "task_id": r.task_id,
        "profile": r.profile,
        "step_key": r.step_key,
        "status": r.status,
        "claim_lock": r.claim_lock,
        "claim_expires": r.claim_expires,
        "worker_pid": r.worker_pid,
        "max_runtime_seconds": r.max_runtime_seconds,
        "last_heartbeat_at": r.last_heartbeat_at,
        "started_at": r.started_at,
        "ended_at": r.ended_at,
        "outcome": r.outcome,
        "summary": r.summary,
        "metadata": r.metadata,
        "error": r.error,
    }


def _links_for(conn: sqlite3.Connection, task_id: str) -> dict[str, list[str]]:
    """Return {'parents': [...], 'children': [...]} for a task."""
    parents = [
        r["parent_id"]
        for r in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
            (task_id,),
        )
    ]
    children = [
        r["child_id"]
        for r in conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
            (task_id,),
        )
    ]
    return {"parents": parents, "children": children}


# ---------------------------------------------------------------------------
# GET /board
# ---------------------------------------------------------------------------

@router.get("/board")
def get_board(
    tenant: str | None = Query(None, description="Filter to a single tenant"),
    include_archived: bool = Query(False),
):
    """Return the full board grouped by status column.

    ``_conn()`` auto-initializes ``kanban.db`` on first call so a fresh
    install doesn't surface a "failed to load" error on the plugin tab.
    """
    conn = _conn()
    try:
        tasks = kanban_db.list_tasks(
            conn, tenant=tenant, include_archived=include_archived
        )
        # Pre-fetch link counts per task (cheap: one query).
        link_counts: dict[str, dict[str, int]] = {}
        for row in conn.execute(
            "SELECT parent_id, child_id FROM task_links"
        ).fetchall():
            link_counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})[
                "children"
            ] += 1
            link_counts.setdefault(row["child_id"], {"parents": 0, "children": 0})[
                "parents"
            ] += 1

        # Comment + event counts (both cheap aggregates).
        comment_counts: dict[str, int] = {
            r["task_id"]: r["n"]
            for r in conn.execute(
                "SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id"
            )
        }

        # Progress rollup: for each parent, how many children are done / total.
        # One pass over task_links joined with child status — cheaper than
        # N per-task queries and the plugin uses it to render "N/M".
        progress: dict[str, dict[str, int]] = {}
        for row in conn.execute(
            "SELECT l.parent_id AS pid, t.status AS cstatus "
            "FROM task_links l JOIN tasks t ON t.id = l.child_id"
        ).fetchall():
            p = progress.setdefault(row["pid"], {"done": 0, "total": 0})
            p["total"] += 1
            if row["cstatus"] == "done":
                p["done"] += 1

        latest_event_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
        ).fetchone()["m"]

        columns: dict[str, list[dict]] = {c: [] for c in BOARD_COLUMNS}
        if include_archived:
            columns["archived"] = []

        for t in tasks:
            d = _task_dict(t)
            d["link_counts"] = link_counts.get(t.id, {"parents": 0, "children": 0})
            d["comment_count"] = comment_counts.get(t.id, 0)
            d["progress"] = progress.get(t.id)  # None when the task has no children
            col = t.status if t.status in columns else "todo"
            columns[col].append(d)

        # Stable per-column ordering already applied by list_tasks
        # (priority DESC, created_at ASC), keep as-is.

        # List of known tenants for the UI filter dropdown.
        tenants = [
            r["tenant"]
            for r in conn.execute(
                "SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant"
            )
        ]
        # List of distinct assignees for the lane-by-profile sub-grouping.
        assignees = [
            r["assignee"]
            for r in conn.execute(
                "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL "
                "AND status != 'archived' ORDER BY assignee"
            )
        ]

        return {
            "columns": [
                {"name": name, "tasks": columns[name]} for name in columns
            ],
            "tenants": tenants,
            "assignees": assignees,
            "latest_event_id": int(latest_event_id),
            "now": int(time.time()),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /tasks/:id
# ---------------------------------------------------------------------------

@router.get("/tasks/{task_id}")
def get_task(task_id: str):
    conn = _conn()
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        return {
            "task": _task_dict(task),
            "comments": [_comment_dict(c) for c in kanban_db.list_comments(conn, task_id)],
            "events": [_event_dict(e) for e in kanban_db.list_events(conn, task_id)],
            "links": _links_for(conn, task_id),
            "runs": [_run_dict(r) for r in kanban_db.list_runs(conn, task_id)],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# POST /tasks
# ---------------------------------------------------------------------------

class CreateTaskBody(BaseModel):
    title: str
    body: str | None = None
    assignee: str | None = None
    tenant: str | None = None
    priority: int = 0
    workspace_kind: str = "scratch"
    workspace_path: str | None = None
    parents: list[str] = Field(default_factory=list)
    triage: bool = False
    idempotency_key: str | None = None
    max_runtime_seconds: int | None = None
    skills: list[str] | None = None


@router.post("/tasks")
def create_task(payload: CreateTaskBody):
    conn = _conn()
    try:
        task_id = kanban_db.create_task(
            conn,
            title=payload.title,
            body=payload.body,
            assignee=payload.assignee,
            created_by="dashboard",
            workspace_kind=payload.workspace_kind,
            workspace_path=payload.workspace_path,
            tenant=payload.tenant,
            priority=payload.priority,
            parents=payload.parents,
            triage=payload.triage,
            idempotency_key=payload.idempotency_key,
            max_runtime_seconds=payload.max_runtime_seconds,
            skills=payload.skills,
        )
        task = kanban_db.get_task(conn, task_id)
        body: dict[str, Any] = {"task": _task_dict(task) if task else None}
        # Surface a dispatcher-presence warning so the UI can show a
        # banner when a `ready` task would otherwise sit idle because no
        # gateway is running (or dispatch_in_gateway=false). Only emit
        # for ready+assigned tasks; triage/todo are expected to wait,
        # and unassigned tasks can't be dispatched regardless.
        if task and task.status == "ready" and task.assignee:
            try:
                from opencomputer.kanban.kanban import _check_dispatcher_presence
                running, message = _check_dispatcher_presence()
                if not running and message:
                    body["warning"] = message
            except Exception:
                # Probe failure must never block the create itself.
                pass
        return body
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PATCH /tasks/:id  (status / assignee / priority / title / body)
# ---------------------------------------------------------------------------

class UpdateTaskBody(BaseModel):
    status: str | None = None
    assignee: str | None = None
    priority: int | None = None
    title: str | None = None
    body: str | None = None
    result: str | None = None
    block_reason: str | None = None
    # Structured handoff fields — forwarded to complete_task when status
    # transitions to 'done'. Dashboard parity with ``oc kanban
    # complete --summary ... --metadata ...``.
    summary: str | None = None
    metadata: dict | None = None


@router.patch("/tasks/{task_id}")
def update_task(task_id: str, payload: UpdateTaskBody):
    conn = _conn()
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")

        # --- assignee ----------------------------------------------------
        if payload.assignee is not None:
            try:
                ok = kanban_db.assign_task(
                    conn, task_id, payload.assignee or None,
                )
            except RuntimeError as e:
                raise HTTPException(status_code=409, detail=str(e))
            if not ok:
                raise HTTPException(status_code=404, detail="task not found")

        # --- status -------------------------------------------------------
        if payload.status is not None:
            s = payload.status
            ok = True
            if s == "done":
                ok = kanban_db.complete_task(
                    conn, task_id,
                    result=payload.result,
                    summary=payload.summary,
                    metadata=payload.metadata,
                )
            elif s == "blocked":
                ok = kanban_db.block_task(conn, task_id, reason=payload.block_reason)
            elif s == "ready":
                # Re-open a blocked task, or just an explicit status set.
                current = kanban_db.get_task(conn, task_id)
                if current and current.status == "blocked":
                    ok = kanban_db.unblock_task(conn, task_id)
                else:
                    # Direct status write for drag-drop (todo -> ready etc).
                    ok = _set_status_direct(conn, task_id, "ready")
            elif s == "archived":
                ok = kanban_db.archive_task(conn, task_id)
            elif s in ("todo", "running", "triage"):
                ok = _set_status_direct(conn, task_id, s)
            else:
                raise HTTPException(status_code=400, detail=f"unknown status: {s}")
            if not ok:
                raise HTTPException(
                    status_code=409,
                    detail=f"status transition to {s!r} not valid from current state",
                )

        # --- priority -----------------------------------------------------
        if payload.priority is not None:
            with kanban_db.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET priority = ? WHERE id = ?",
                    (int(payload.priority), task_id),
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?, 'reprioritized', ?, ?)",
                    (task_id, json.dumps({"priority": int(payload.priority)}),
                     int(time.time())),
                )

        # --- title / body -------------------------------------------------
        if payload.title is not None or payload.body is not None:
            with kanban_db.write_txn(conn):
                sets, vals = [], []
                if payload.title is not None:
                    if not payload.title.strip():
                        raise HTTPException(status_code=400, detail="title cannot be empty")
                    sets.append("title = ?")
                    vals.append(payload.title.strip())
                if payload.body is not None:
                    sets.append("body = ?")
                    vals.append(payload.body)
                vals.append(task_id)
                conn.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals,
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?, 'edited', NULL, ?)",
                    (task_id, int(time.time())),
                )

        updated = kanban_db.get_task(conn, task_id)
        return {"task": _task_dict(updated) if updated else None}
    finally:
        conn.close()


def _set_status_direct(
    conn: sqlite3.Connection, task_id: str, new_status: str,
) -> bool:
    """Direct status write for drag-drop moves that aren't covered by the
    structured complete/block/unblock/archive verbs (e.g. todo<->ready,
    running<->ready). Appends a ``status`` event row for the live feed.

    When this transitions OFF ``running`` to anything other than the
    terminal verbs above (which own their own run closing), we close the
    active run with outcome='reclaimed' so attempt history isn't
    orphaned. ``running -> ready`` via drag-drop is the common case
    (user yanking a stuck worker back to the queue).
    """
    with kanban_db.write_txn(conn):
        # Snapshot current state so we know whether to close a run.
        prev = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if prev is None:
            return False
        was_running = prev["status"] == "running"

        cur = conn.execute(
            "UPDATE tasks SET status = ?, "
            "  claim_lock = CASE WHEN ? = 'running' THEN claim_lock ELSE NULL END, "
            "  claim_expires = CASE WHEN ? = 'running' THEN claim_expires ELSE NULL END, "
            "  worker_pid = CASE WHEN ? = 'running' THEN worker_pid ELSE NULL END "
            "WHERE id = ?",
            (new_status, new_status, new_status, new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        run_id = None
        if was_running and new_status != "running" and prev["current_run_id"]:
            run_id = kanban_db._end_run(
                conn, task_id,
                outcome="reclaimed", status="reclaimed",
                summary=f"status changed to {new_status} (dashboard/direct)",
            )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'status', ?, ?)",
            (task_id, run_id, json.dumps({"status": new_status}), int(time.time())),
        )
    # If we re-opened something, children may have gone stale.
    if new_status in ("done", "ready"):
        kanban_db.recompute_ready(conn)
    return True


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

class CommentBody(BaseModel):
    body: str
    author: str | None = "dashboard"


@router.post("/tasks/{task_id}/comments")
def add_comment(task_id: str, payload: CommentBody):
    if not payload.body.strip():
        raise HTTPException(status_code=400, detail="body is required")
    conn = _conn()
    try:
        if kanban_db.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        kanban_db.add_comment(
            conn, task_id, author=payload.author or "dashboard", body=payload.body,
        )
        return {"ok": True}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

class LinkBody(BaseModel):
    parent_id: str
    child_id: str


@router.post("/links")
def add_link(payload: LinkBody):
    conn = _conn()
    try:
        kanban_db.link_tasks(conn, payload.parent_id, payload.child_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@router.delete("/links")
def delete_link(parent_id: str = Query(...), child_id: str = Query(...)):
    conn = _conn()
    try:
        ok = kanban_db.unlink_tasks(conn, parent_id, child_id)
        return {"ok": bool(ok)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bulk actions (multi-select on the board)
# ---------------------------------------------------------------------------

class BulkTaskBody(BaseModel):
    ids: list[str]
    status: str | None = None
    assignee: str | None = None  # "" or None = unassign
    priority: int | None = None
    archive: bool = False


@router.post("/tasks/bulk")
def bulk_update(payload: BulkTaskBody):
    """Apply the same patch to every id in ``payload.ids``.

    This is an *independent* iteration — per-task failures don't abort
    siblings. Returns per-id outcome so the UI can surface partials.
    """
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="ids is required")
    results: list[dict] = []
    conn = _conn()
    try:
        for tid in ids:
            entry: dict[str, Any] = {"id": tid, "ok": True}
            try:
                task = kanban_db.get_task(conn, tid)
                if task is None:
                    entry.update(ok=False, error="not found")
                    results.append(entry)
                    continue
                if payload.archive:
                    if not kanban_db.archive_task(conn, tid):
                        entry.update(ok=False, error="archive refused")
                if payload.status is not None and not payload.archive:
                    s = payload.status
                    if s == "done":
                        ok = kanban_db.complete_task(conn, tid)
                    elif s == "blocked":
                        ok = kanban_db.block_task(conn, tid)
                    elif s == "ready":
                        cur = kanban_db.get_task(conn, tid)
                        if cur and cur.status == "blocked":
                            ok = kanban_db.unblock_task(conn, tid)
                        else:
                            ok = _set_status_direct(conn, tid, "ready")
                    elif s in ("todo", "running", "triage"):
                        ok = _set_status_direct(conn, tid, s)
                    else:
                        entry.update(ok=False, error=f"unknown status {s!r}")
                        results.append(entry)
                        continue
                    if not ok:
                        entry.update(ok=False, error=f"transition to {s!r} refused")
                if payload.assignee is not None:
                    try:
                        if not kanban_db.assign_task(
                            conn, tid, payload.assignee or None,
                        ):
                            entry.update(ok=False, error="assign refused")
                    except RuntimeError as e:
                        entry.update(ok=False, error=str(e))
                if payload.priority is not None:
                    with kanban_db.write_txn(conn):
                        conn.execute(
                            "UPDATE tasks SET priority = ? WHERE id = ?",
                            (int(payload.priority), tid),
                        )
                        conn.execute(
                            "INSERT INTO task_events (task_id, kind, payload, created_at) "
                            "VALUES (?, 'reprioritized', ?, ?)",
                            (tid, json.dumps({"priority": int(payload.priority)}),
                             int(time.time())),
                        )
            except Exception as e:  # defensive — one bad id shouldn't kill the batch
                entry.update(ok=False, error=str(e))
            results.append(entry)
        return {"results": results}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Plugin config (read dashboard.kanban.* defaults from config.yaml)
# ---------------------------------------------------------------------------

@router.get("/config")
def get_config():
    """Return kanban dashboard preferences from ~/.oc/config.yaml.

    Reads the ``dashboard.kanban`` section if present; defaults otherwise.
    Used by the UI to pre-select tenant filters, toggle markdown rendering,
    or set column-width preferences without a round-trip per page load.
    """
    try:
        from opencomputer.kanban.config import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    dash_cfg = (cfg.get("dashboard") or {})
    # dashboard.kanban may itself be a dict; fall back to {}.
    k_cfg = dash_cfg.get("kanban") or {}
    return {
        "default_tenant": k_cfg.get("default_tenant") or "",
        "lane_by_profile": bool(k_cfg.get("lane_by_profile", True)),
        "include_archived_by_default": bool(k_cfg.get("include_archived_by_default", False)),
        "render_markdown": bool(k_cfg.get("render_markdown", True)),
    }


# ---------------------------------------------------------------------------
# Stats (per-profile / per-status counts + oldest-ready age)
# ---------------------------------------------------------------------------

@router.get("/stats")
def get_stats():
    """Per-status + per-assignee counts + oldest-ready age.

    Designed for the dashboard HUD and for router profiles that need to
    answer "is this specialist overloaded?" without scanning the whole
    board themselves.
    """
    conn = _conn()
    try:
        return kanban_db.board_stats(conn)
    finally:
        conn.close()


@router.get("/assignees")
def get_assignees():
    """Known profiles + per-profile task counts.

    Returns the union of ``~/.oc/profiles/*`` on disk and every
    distinct assignee currently used on the board. The dashboard uses
    this to populate its assignee dropdown so a freshly-created profile
    appears in the picker before it's been given any task.
    """
    conn = _conn()
    try:
        return {"assignees": kanban_db.known_assignees(conn)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Worker log (read-only; file written by _default_spawn)
# ---------------------------------------------------------------------------

@router.get("/tasks/{task_id}/log")
def get_task_log(task_id: str, tail: int | None = Query(None, ge=1, le=2_000_000)):
    """Return the worker's stdout/stderr log.

    ``tail`` caps the response size (bytes) so the dashboard drawer
    doesn't paginate megabytes into the browser. Returns 404 if the task
    has never spawned. The on-disk log is rotated at 2 MiB per
    ``_rotate_worker_log`` — a single ``.log.1`` is kept, no further
    generations, so disk usage per task is bounded at ~4 MiB.
    """
    conn = _conn()
    try:
        task = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    content = kanban_db.read_worker_log(task_id, tail_bytes=tail)
    log_path = kanban_db.worker_log_path(task_id)
    size = log_path.stat().st_size if log_path.exists() else 0
    return {
        "task_id": task_id,
        "path": str(log_path),
        "exists": content is not None,
        "size_bytes": size,
        "content": content or "",
        # Truncated when the on-disk file was larger than the tail cap.
        "truncated": bool(tail and size > tail),
    }


# ---------------------------------------------------------------------------
# Dispatch nudge (optional quick-path so the UI doesn't wait 60 s)
# ---------------------------------------------------------------------------

@router.post("/dispatch")
def dispatch(dry_run: bool = Query(False), max_n: int = Query(8, alias="max")):
    conn = _conn()
    try:
        result = kanban_db.dispatch_once(
            conn, dry_run=dry_run, max_spawn=max_n,
        )
        # DispatchResult is a dataclass.
        try:
            return asdict(result)
        except TypeError:
            return {"result": str(result)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# WebSocket: /events?since=<event_id>
# ---------------------------------------------------------------------------

# Poll interval for the event tail loop. SQLite WAL + 300 ms polling is
# the simplest and most robust approach; it adds a fraction of a percent
# of CPU and has no shared state to synchronize across workers.
_EVENT_POLL_SECONDS = 0.3


@router.websocket("/events")
async def stream_events(ws: WebSocket):
    # Enforce the dashboard session token as a query param — browsers can't
    # set Authorization on a WS upgrade. This matches how the PTY bridge
    # authenticates in opencomputer.kanban/web_server.py.
    token = ws.query_params.get("token")
    if not _check_ws_token(token):
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    try:
        since_raw = ws.query_params.get("since", "0")
        try:
            cursor = int(since_raw)
        except ValueError:
            cursor = 0

        def _fetch_new(cursor_val: int) -> tuple[int, list[dict]]:
            conn = kanban_db.connect()
            try:
                rows = conn.execute(
                    "SELECT id, task_id, run_id, kind, payload, created_at "
                    "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT 200",
                    (cursor_val,),
                ).fetchall()
                out: list[dict] = []
                new_cursor = cursor_val
                for r in rows:
                    try:
                        payload = json.loads(r["payload"]) if r["payload"] else None
                    except Exception:
                        payload = None
                    out.append({
                        "id": r["id"],
                        "task_id": r["task_id"],
                        "run_id": r["run_id"],
                        "kind": r["kind"],
                        "payload": payload,
                        "created_at": r["created_at"],
                    })
                    new_cursor = r["id"]
                return new_cursor, out
            finally:
                conn.close()

        while True:
            cursor, events = await asyncio.to_thread(_fetch_new, cursor)
            if events:
                await ws.send_json({"events": events, "cursor": cursor})
            await asyncio.sleep(_EVENT_POLL_SECONDS)
    except WebSocketDisconnect:
        return
    except Exception as exc:  # defensive: never crash the dashboard worker
        log.warning("Kanban event stream error: %s", exc)
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Wave 6.E.13 — Multi-host write coordination (inbound peer endpoints)
# ---------------------------------------------------------------------------


def _peer_slug_from_query(slug: str | None) -> str:
    """The peer identifies itself via ``?slug=<our-name-for-them>``.
    Both sides agreed on this slug at the ``oc kanban remote add`` time."""
    if not slug:
        raise HTTPException(
            status_code=400, detail="missing ?slug=<peer-slug>",
        )
    return slug


async def _read_body(request: Request) -> bytes:
    return await request.body()


@router.post("/proxy/spawn")
async def proxy_spawn(
    request: Request,
    slug: str = Query(...),
    x_oc_signature: str | None = Header(None, alias="X-OC-Signature"),
):
    """Inbound: a peer asks us to spawn a worker for a delegated task.

    HMAC-verified. We claim the task locally + spawn (via the existing
    dispatcher path). Returns ``{remote_task_id, lease_until}`` so the
    sender can record the lease.
    """
    from opencomputer.kanban.remote_dispatch import (
        DEFAULT_REMOTE_TTL_SECONDS,
        verify_inbound_request,
    )
    from opencomputer.kanban.remote_hosts import HmacAuthError

    body = await _read_body(request)
    conn = _conn()
    try:
        try:
            verify_inbound_request(
                conn,
                slug=slug,
                header_value=x_oc_signature,
                method="POST",
                path="/api/plugins/kanban/proxy/spawn",
                body=body,
            )
        except HmacAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail=f"bad JSON: {exc}",
            ) from exc

        task = payload.get("task") or {}
        title = task.get("title")
        assignee = task.get("assignee")
        if not title or not assignee:
            raise HTTPException(
                status_code=422, detail="task.title and task.assignee required",
            )
        ws_kind = task.get("workspace_kind") or "scratch"
        ws_path = task.get("workspace_path")
        ws_payload_b64 = payload.get("workspace_payload_b64")

        # Wave 6.E.15 — when the sender included a workspace payload
        # AND we have workspace_sync_enabled for this peer, unpack into
        # a local sandbox. Otherwise, fall back to the existing
        # PR #460 behaviour: dir:<path> must exist locally or 422.
        if ws_payload_b64:
            from opencomputer.kanban import remote_hosts as _rh
            host = _rh.find_remote_host(conn, slug)
            if host is None or not host.workspace_sync_enabled:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"peer {slug!r} sent workspace payload but this host "
                        "has workspace_sync_enabled=0 — refuse"
                    ),
                )
            try:
                import base64

                from opencomputer.kanban.workspace_payload import (
                    WorkspacePayloadError,
                    unpack_workspace,
                )
                tarball = base64.b64decode(ws_payload_b64, validate=True)
                # Extract under <kanban-root>/remote-workspaces/<task-id>/
                from pathlib import Path
                remote_ws_root = (
                    kanban_db.kanban_home() / "remote-workspaces"
                )
                remote_ws_root.mkdir(parents=True, exist_ok=True)
                # Use the original payload's task id as a sandbox folder.
                sandbox_parent = remote_ws_root / (
                    str(task.get("id") or f"task-{int(time.time())}")
                )
                sandbox_parent.mkdir(parents=True, exist_ok=True)
                extracted = unpack_workspace(tarball, dest=sandbox_parent)
                ws_kind = "dir"
                ws_path = str(extracted)
            except (ValueError, WorkspacePayloadError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"workspace payload extract failed: {exc}",
                ) from exc
        elif ws_kind == "dir" and ws_path:
            from pathlib import Path
            if not Path(ws_path).exists():
                raise HTTPException(
                    status_code=422,
                    detail=f"dir:{ws_path} does not exist on this host",
                )

        # Create the local mirror task. We use the assignee from the
        # payload (peer-side profile name) and a fresh local id.
        local_task_id = kanban_db.create_task(
            conn,
            title=title,
            body=task.get("body"),
            assignee=assignee,
            priority=int(task.get("priority") or 0),
            tenant=task.get("tenant"),
            workspace_kind=ws_kind,
            workspace_path=ws_path,
        )
        lease_until = int(time.time()) + DEFAULT_REMOTE_TTL_SECONDS
        return {
            "remote_task_id": local_task_id,
            "lease_until": lease_until,
        }
    finally:
        conn.close()


@router.post("/proxy/heartbeat")
async def proxy_heartbeat(
    request: Request,
    slug: str = Query(...),
    x_oc_signature: str | None = Header(None, alias="X-OC-Signature"),
):
    """Inbound: peer asks us to refresh a delegated task's lease.

    Returns the new ``lease_until`` (server-time TTL — we set it, not
    the caller).
    """
    from opencomputer.kanban.remote_dispatch import (
        DEFAULT_REMOTE_TTL_SECONDS,
        verify_inbound_request,
    )
    from opencomputer.kanban.remote_hosts import HmacAuthError

    body = await _read_body(request)
    conn = _conn()
    try:
        try:
            verify_inbound_request(
                conn,
                slug=slug,
                header_value=x_oc_signature,
                method="POST",
                path="/api/plugins/kanban/proxy/heartbeat",
                body=body,
            )
        except HmacAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail=f"bad JSON: {exc}",
            ) from exc

        remote_task_id = payload.get("remote_task_id")
        if not remote_task_id:
            raise HTTPException(
                status_code=422, detail="remote_task_id required",
            )
        if kanban_db.get_task(conn, remote_task_id) is None:
            raise HTTPException(
                status_code=404, detail=f"task {remote_task_id!r} not found",
            )
        lease_until = int(time.time()) + DEFAULT_REMOTE_TTL_SECONDS
        return {"lease_until": lease_until}
    finally:
        conn.close()


@router.post("/proxy/callback")
async def proxy_callback(
    request: Request,
    slug: str = Query(...),
    x_oc_signature: str | None = Header(None, alias="X-OC-Signature"),
):
    """Inbound: peer reports a delegated task's terminal state.

    Verifies HMAC + reconciles the local ``kanban_remote_claims`` row
    + transitions the underlying local task.
    """
    from opencomputer.kanban.remote_dispatch import (
        reconcile_callback,
        verify_inbound_request,
    )
    from opencomputer.kanban.remote_hosts import HmacAuthError

    body = await _read_body(request)
    conn = _conn()
    try:
        try:
            verify_inbound_request(
                conn,
                slug=slug,
                header_value=x_oc_signature,
                method="POST",
                path="/api/plugins/kanban/proxy/callback",
                body=body,
            )
        except HmacAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail=f"bad JSON: {exc}",
            ) from exc

        try:
            reconcile_callback(conn, remote_slug=slug, payload=payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True}
    finally:
        conn.close()
