"""Tests for the ``OC_DASHBOARD_TOKEN`` env-pinning override.

Phase 1a of the tryopencomputer.com platform build
(see ``OpenComputer/docs/plans/tryopencomputer-platform-build-2026-05-18.md``).

The contract:

- When ``OC_DASHBOARD_TOKEN`` is set at import time, the module-level
  ``_SESSION_TOKEN`` MUST equal the env value verbatim. This is the
  shape production VMs use — the platform records the token in its DB
  and the VM must surface the same value across restarts.
- When the env var is absent, ``_SESSION_TOKEN`` falls back to a fresh
  ``secrets.token_urlsafe(32)`` (43 chars urlsafe-base64). This is the
  shape standalone / local users see.
- The env is read at module import time. Changing the env var after
  import does NOT re-derive the token; reload is required if a test
  wants to observe the override flip.

The dashboard server's request-handling code uses ``app.state.session_token``
which is seeded from ``_SESSION_TOKEN`` in ``_build_app``. Confirming the
module-level value is what gets propagated is part of the contract.
"""

from __future__ import annotations

import importlib
import sys

_DASHBOARD_SERVER_MODULE = "opencomputer.dashboard.server"


def _reimport_dashboard_server():
    """Drop the module from ``sys.modules`` and re-import.

    ``_SESSION_TOKEN`` is assigned at import time from the env, so toggling
    the env then reimporting is the only way to exercise both branches in
    a single test process.
    """
    sys.modules.pop(_DASHBOARD_SERVER_MODULE, None)
    return importlib.import_module(_DASHBOARD_SERVER_MODULE)


def test_env_override_pins_token(monkeypatch):
    """With ``OC_DASHBOARD_TOKEN`` set, the module-level token uses the
    env value verbatim."""
    monkeypatch.setenv("OC_DASHBOARD_TOKEN", "test-pinned-token-xyz")
    module = _reimport_dashboard_server()
    try:
        assert module._SESSION_TOKEN == "test-pinned-token-xyz"
    finally:
        sys.modules.pop(_DASHBOARD_SERVER_MODULE, None)


def test_env_override_propagates_to_app_state(monkeypatch):
    """The env-pinned token MUST be the one a freshly built FastAPI app
    surfaces via ``app.state.session_token``.

    This is the property production VMs rely on: the platform sets the
    env, ``oc workspace backend`` boots, the FastAPI app accepts the
    Bearer that matches that env."""
    monkeypatch.setenv("OC_DASHBOARD_TOKEN", "test-propagation-token")
    module = _reimport_dashboard_server()
    try:
        app = module._build_app(enable_pty=False)
        assert app.state.session_token == "test-propagation-token"
    finally:
        sys.modules.pop(_DASHBOARD_SERVER_MODULE, None)


def test_no_env_uses_random_token(monkeypatch):
    """Without the env var, ``_SESSION_TOKEN`` is a fresh urlsafe token.

    ``secrets.token_urlsafe(32)`` returns 43 chars (32 random bytes
    base64-urlsafe-encoded with padding stripped). We assert shape, not
    the exact value (would be flaky)."""
    monkeypatch.delenv("OC_DASHBOARD_TOKEN", raising=False)
    module = _reimport_dashboard_server()
    try:
        token = module._SESSION_TOKEN
        assert isinstance(token, str)
        assert len(token) == 43
        # urlsafe-base64 alphabet
        assert all(c.isalnum() or c in "-_" for c in token)
    finally:
        sys.modules.pop(_DASHBOARD_SERVER_MODULE, None)


def test_two_imports_without_env_yield_different_tokens(monkeypatch):
    """Successive reimports without the env MUST yield different random
    tokens. If this ever returns the same value twice, something is
    caching the token outside the env override path and the contract is
    silently broken for any non-env user."""
    monkeypatch.delenv("OC_DASHBOARD_TOKEN", raising=False)
    first = _reimport_dashboard_server()._SESSION_TOKEN
    second = _reimport_dashboard_server()._SESSION_TOKEN
    try:
        assert first != second
    finally:
        sys.modules.pop(_DASHBOARD_SERVER_MODULE, None)


def test_two_imports_with_same_env_yield_same_token(monkeypatch):
    """Successive reimports with the SAME env value MUST yield the same
    token both times. This is the actual property platform VMs depend
    on across systemd restarts."""
    monkeypatch.setenv("OC_DASHBOARD_TOKEN", "stable-platform-token")
    first = _reimport_dashboard_server()._SESSION_TOKEN
    second = _reimport_dashboard_server()._SESSION_TOKEN
    try:
        assert first == "stable-platform-token"
        assert second == "stable-platform-token"
    finally:
        sys.modules.pop(_DASHBOARD_SERVER_MODULE, None)


def test_empty_env_falls_back_to_random(monkeypatch):
    """Empty-string env (``OC_DASHBOARD_TOKEN=``) is treated the same
    as unset — falls through ``or`` to the random fallback.

    Important because shell scripts that conditionally export the var
    sometimes leave it as empty rather than unset; we MUST NOT accept
    an empty Bearer as the production token."""
    monkeypatch.setenv("OC_DASHBOARD_TOKEN", "")
    module = _reimport_dashboard_server()
    try:
        assert module._SESSION_TOKEN != ""
        assert len(module._SESSION_TOKEN) == 43
    finally:
        sys.modules.pop(_DASHBOARD_SERVER_MODULE, None)


def test_reload_resets_module_state(monkeypatch):
    """Sanity: reimport actually does re-derive the value. If this fails,
    every other test in this file becomes meaningless."""
    monkeypatch.setenv("OC_DASHBOARD_TOKEN", "first-value")
    first = _reimport_dashboard_server()._SESSION_TOKEN
    assert first == "first-value"

    monkeypatch.setenv("OC_DASHBOARD_TOKEN", "second-value")
    second = _reimport_dashboard_server()._SESSION_TOKEN
    try:
        assert second == "second-value"
        assert first != second
    finally:
        sys.modules.pop(_DASHBOARD_SERVER_MODULE, None)
