"""Server: HTTP control surface — auth + CSRF + routes + dispatcher + lifecycle.

Wave W2b — the security perimeter.

Public surface:

  - BrowserAuth, ensure_browser_control_auth, generate_browser_control_token,
    is_authorized
  - CSRFMiddleware, BrowserAuthMiddleware, BodyLimitMiddleware
  - is_loopback_url, should_reject_browser_mutation
  - BrowserRouteContext (+ handler error types)
  - is_persistent_browser_profile_mutation, normalize_browser_request_path
  - DispatchResult, dispatch_browser_control_request
  - create_app
  - BrowserServerHandle, start_browser_control_server, stop_browser_control_server

Routes are registered onto ``create_app(ctx)``'s FastAPI app via
``server.routes.register_all`` (called automatically inside create_app).
"""

from __future__ import annotations

from .app import create_app
from .auth import (
    BrowserAuth,
    ensure_browser_control_auth,
    generate_browser_control_token,
    is_authorized,
    parse_basic_password,
    parse_bearer_token,
    resolve_browser_control_auth,
    should_auto_generate_browser_auth,
)
from .csrf import (
    CSRFMiddleware,
    is_loopback_url,
    should_reject_browser_mutation,
)
from .dispatcher import DispatchResult, dispatch_browser_control_request
from .handlers import (
    BrowserHandlerError,
    BrowserRouteContext,
    DriverUnsupportedError,
    ProfileMutationDeniedError,
    ensure_profile_can_mutate,
    resolve_profile_name,
)
from .lifecycle import (
    LOOPBACK_HOST,
    BrowserServerHandle,
    start_browser_control_server,
    stop_browser_control_server,
)
from .middleware import (
    DEFAULT_BODY_LIMIT_BYTES,
    BodyLimitMiddleware,
    BrowserAuthMiddleware,
)
from .policy import (
    is_persistent_browser_profile_mutation,
    normalize_browser_request_path,
)

__all__ = [
    "BodyLimitMiddleware",
    "BrowserAuth",
    "BrowserAuthMiddleware",
    "BrowserHandlerError",
    "BrowserRouteContext",
    "BrowserServerHandle",
    "DriverUnsupportedError",
    "CSRFMiddleware",
    "DEFAULT_BODY_LIMIT_BYTES",
    "DispatchResult",
    "LOOPBACK_HOST",
    "ProfileMutationDeniedError",
    "create_app",
    "dispatch_browser_control_request",
    "ensure_browser_control_auth",
    "ensure_profile_can_mutate",
    "generate_browser_control_token",
    "is_authorized",
    "is_loopback_url",
    "is_persistent_browser_profile_mutation",
    "normalize_browser_request_path",
    "parse_basic_password",
    "parse_bearer_token",
    "resolve_browser_control_auth",
    "resolve_profile_name",
    "should_auto_generate_browser_auth",
    "should_reject_browser_mutation",
    "start_browser_control_server",
    "stop_browser_control_server",
]
