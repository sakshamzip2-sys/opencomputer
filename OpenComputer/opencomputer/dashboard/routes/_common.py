"""Shared helpers for v1 routes.

- ``clamp_limit`` — pagination clamp; default 50, max 200.
- ``get_session_db`` — context manager yielding a ``SessionDB`` for the
  active profile. Lazy-imports so route modules with no DB needs (logs,
  events) don't pay the import cost.
- ``audit_log`` — append a structured audit-log line for sensitive
  operations (env reveal, profile delete, etc.).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, status as http_status

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_audit_log = logging.getLogger("opencomputer.dashboard.audit")


def clamp_limit(
    limit: int | None,
    *,
    default: int = DEFAULT_LIMIT,
    maximum: int = MAX_LIMIT,
) -> int:
    """Pagination clamp — defends every list endpoint against unbounded queries."""
    if limit is None:
        return default
    return max(1, min(int(limit), maximum))


@contextmanager
def get_session_db() -> Iterator["SessionDB"]:  # type: ignore[name-defined]
    """Yield a ``SessionDB`` for the active profile.

    Lazy-imports to avoid pulling agent.state into module-import time
    when only some routes need it (logs/events do not).
    """
    from opencomputer.agent.config import default_config
    from opencomputer.agent.state import SessionDB

    cfg = default_config()
    db_path = cfg.home / "sessions.db"
    if not db_path.exists():
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="sessions.db not initialized — start the gateway first",
        )
    yield SessionDB(db_path)


def audit_log(action: str, **fields: object) -> None:
    """Emit a structured audit-log line.

    Used for sensitive operations: env reveal, profile delete, OAuth
    revocation. The log line is JSON so downstream tools can parse it.
    Never include secret values — only key names + actor/source metadata.
    """
    payload = {"ts": time.time(), "action": action, **fields}
    _audit_log.warning("audit %s", json.dumps(payload, default=str))
