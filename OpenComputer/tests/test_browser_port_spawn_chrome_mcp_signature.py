"""Regression test for Wave 4 hotfix: spawn_chrome_mcp signature mismatch."""
from __future__ import annotations

import inspect

from extensions.browser_control.snapshot.chrome_mcp import spawn_chrome_mcp


def test_spawn_chrome_mcp_is_keyword_only() -> None:
    """spawn_chrome_mcp's signature is keyword-only.

    The dispatcher-bootstrap wrapper that calls it must pass arguments
    as kwargs, not positionally. Wave 4 hotfix: a wrapper passing
    `profile` positionally crashed with 'takes 0 positional arguments
    but 1 was given' when the agent first exercised the user profile.
    """
    sig = inspect.signature(spawn_chrome_mcp)
    for name, param in sig.parameters.items():
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"spawn_chrome_mcp param {name!r} must be KEYWORD_ONLY; "
            f"got {param.kind}. If you change this, update "
            f"_dispatcher_bootstrap.py:_spawn_chrome_mcp accordingly."
        )
