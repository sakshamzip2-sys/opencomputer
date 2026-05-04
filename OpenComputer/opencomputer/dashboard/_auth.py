"""Dashboard auth helpers (Wave 6.D-α).

Centralizes the session-token check so both the management mutation
endpoints and the models mutation endpoints share one implementation.
The pattern matches what ``/api/pty`` already does in
``opencomputer.dashboard.server`` — Bearer token from ``Authorization``
header OR ``?token=`` query param.

Why both? Browsers cannot set ``Authorization`` on a WebSocket upgrade,
so the WebSocket route accepts query-param tokens. We mirror this here
for symmetry: the same token works on REST and WS, and mutation buttons
in the static SPA can attach the token via either mechanism.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

__all__ = ["require_session_token"]


def _extract_token(request: Request) -> str:
    """Pull the candidate token from header → query → cookie."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):]
    qs = request.query_params.get("token", "")
    if qs:
        return qs
    return request.cookies.get("oc_dashboard_token", "")


async def require_session_token(request: Request) -> None:
    """FastAPI dependency: 401 if the request lacks a valid session token.

    Uses ``secrets.compare_digest`` for constant-time comparison —
    prevents timing-attack token recovery against a small alphabet.
    """
    expected = getattr(request.app.state, "session_token", None)
    if not expected:
        # No token configured (test harness etc.) — allow.
        return
    candidate = _extract_token(request)
    if not candidate or not secrets.compare_digest(candidate, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid session token",
            headers={"WWW-Authenticate": "Bearer"},
        )
