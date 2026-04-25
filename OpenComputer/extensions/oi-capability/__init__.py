"""Compat shim — extensions.oi_capability is deprecated; use extensions.coding_harness.oi_bridge.

PR-3 of the 2026-04-25 Hermes parity plan moved OI from a standalone plugin
into the coding-harness as a bridge layer (per docs/f7/interweaving-plan.md).
This shim keeps the old import path working for one release window with a
DeprecationWarning. Remove on the next major version bump.
"""
# ruff: noqa: N999  # directory name 'oi-capability' has a hyphen (required by plugin manifest)
from __future__ import annotations

import warnings as _w

_NEW_LOCATION = "extensions.coding_harness.oi_bridge"
_w.warn(
    f"extensions.oi_capability is deprecated; use {_NEW_LOCATION} instead. "
    "This shim will be removed in the next major release.",
    DeprecationWarning,
    stacklevel=2,
)

__version__ = "0.1.0"
