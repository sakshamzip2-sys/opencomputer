"""Deprecated stub plugin — register() is a no-op. See compat shim in __init__.py.

PR-3 (2026-04-25): OI tools were moved from extensions/oi-capability/ into
extensions/coding-harness/oi_bridge/ per docs/f7/interweaving-plan.md.
This plugin manifest is kept for one release window so existing installations
do not crash on load. Remove this directory in the next major version bump.
"""
# ruff: noqa: N999
from __future__ import annotations


def register(api) -> None:  # noqa: ANN001
    """No-op. The real OI bridge tools register via extensions/coding-harness/plugin.py."""
    return None
