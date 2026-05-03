"""AI / Chrome MCP / role-snapshot pipeline (Wave W1b).

Public surface:
  - INTERACTIVE_ROLES, CONTENT_ROLES, STRUCTURAL_ROLES (46 ARIA roles total)
  - RoleRef, SnapshotResult dataclasses
  - build_role_snapshot_from_aria_snapshot (Path 2 — Playwright aria_snapshot text)
  - build_ai_snapshot_from_chrome_mcp_snapshot (Path 3 — Chrome MCP tree)
  - ChromeMcpClient, spawn_chrome_mcp (subprocess client)
  - normalize_screenshot (7 × 6 size/quality grid → smallest variant under bytes cap)

Path 1 (`page._snapshot_for_ai`) is intentionally NOT shipped for v0.1 —
playwright-python doesn't reliably expose the underscore API. See
BLUEPRINT §11 question 3.
"""

from __future__ import annotations

from .chrome_mcp import (
    DEFAULT_CHROME_MCP_ARGS,
    DEFAULT_CHROME_MCP_COMMAND,
    ChromeMcpClient,
    ChromeMcpToolError,
    ChromeMcpTransportError,
    spawn_chrome_mcp,
)
from .chrome_mcp_snapshot import (
    ChromeMcpSnapshotNode,
    build_ai_snapshot_from_chrome_mcp_snapshot,
    flatten_chrome_mcp_snapshot,
)
from .role_snapshot import (
    RoleNameTracker,
    RoleRef,
    SnapshotResult,
    build_role_snapshot_from_aria_snapshot,
    parse_role_ref,
)
from .screenshot import (
    DEFAULT_BROWSER_SCREENSHOT_MAX_BYTES,
    DEFAULT_BROWSER_SCREENSHOT_MAX_SIDE,
    JPEG_QUALITY_STEPS,
    SIDE_GRID_BASE,
    ScreenshotTooLargeError,
    normalize_screenshot,
)
from .snapshot_roles import (
    CONTENT_ROLES,
    INTERACTIVE_ROLES,
    STRUCTURAL_ROLES,
)

__all__ = [
    "CONTENT_ROLES",
    "DEFAULT_BROWSER_SCREENSHOT_MAX_BYTES",
    "DEFAULT_BROWSER_SCREENSHOT_MAX_SIDE",
    "DEFAULT_CHROME_MCP_ARGS",
    "DEFAULT_CHROME_MCP_COMMAND",
    "INTERACTIVE_ROLES",
    "JPEG_QUALITY_STEPS",
    "SIDE_GRID_BASE",
    "STRUCTURAL_ROLES",
    "ChromeMcpClient",
    "ChromeMcpSnapshotNode",
    "ChromeMcpToolError",
    "ChromeMcpTransportError",
    "RoleNameTracker",
    "RoleRef",
    "ScreenshotTooLargeError",
    "SnapshotResult",
    "build_ai_snapshot_from_chrome_mcp_snapshot",
    "build_role_snapshot_from_aria_snapshot",
    "flatten_chrome_mcp_snapshot",
    "normalize_screenshot",
    "parse_role_ref",
    "spawn_chrome_mcp",
]
