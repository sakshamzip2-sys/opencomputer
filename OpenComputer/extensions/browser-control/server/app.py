"""FastAPI app factory.

  create_app(ctx) -> FastAPI

The middleware order on incoming requests is critical (deep dive §3
+ Python translation guide):

    CSRF → Auth → BodyLimit → routes

In FastAPI, ``add_middleware`` reverses the call order — so we add
BodyLimit first, then Auth, then CSRF (CSRF ends up outermost).
"""

from __future__ import annotations

from fastapi import FastAPI

from .csrf import CSRFMiddleware
from .handlers import BrowserRouteContext
from .middleware import DEFAULT_BODY_LIMIT_BYTES, BodyLimitMiddleware, BrowserAuthMiddleware
from .routes import register_all


def create_app(
    ctx: BrowserRouteContext,
    *,
    body_limit_bytes: int = DEFAULT_BODY_LIMIT_BYTES,
) -> FastAPI:
    app = FastAPI(
        title="OpenComputer browser-control",
        version="0.1.0",
        docs_url=None,  # internal API; no swagger surface
        redoc_url=None,
        openapi_url=None,
    )
    app.state.browser_ctx = ctx

    # add_middleware reverses on incoming → add innermost first.
    app.add_middleware(BodyLimitMiddleware, limit_bytes=body_limit_bytes)
    app.add_middleware(BrowserAuthMiddleware, auth=ctx.auth)
    app.add_middleware(CSRFMiddleware)

    register_all(app, ctx)
    return app
