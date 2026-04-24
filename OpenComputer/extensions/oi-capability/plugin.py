"""OI Capability plugin entry-point.

Tools are NOT registered here — registration waits for Session A's Phase 5
consent + sandbox + coding-harness interweaving.

See docs/f7/interweaving-plan.md for the Phase 5 refactor contract.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MANIFEST = {
    "id": "oi-capability",
    "name": "Open Interpreter capability",
    "kind": "tools",
    "version": "0.1.0",
    "enabled_by_default": False,
    "description": (
        "Wraps Open Interpreter as a sandboxed subprocess. "
        "AGPL v3 — kept isolated via subprocess boundary. "
        "Tools are registered in Phase 5 after consent + sandbox integration."
    ),
    "schema_version": 1,
    "tiers": {
        1: "Introspection (read-only)",
        2: "Communication (reads + drafts)",
        3: "Browser",
        4: "System Control (mutating)",
        5: "Advanced",
    },
    "tool_count": 23,
    "agpl_boundary": (
        "OI (AGPL v3) runs in an isolated subprocess venv. "
        "No `import interpreter` outside subprocess/server.py."
    ),
}


def register(api) -> None:  # noqa: ANN001
    """Plugin entry-point — stub until Phase 5 wires consent + sandbox gates."""
    logger.info(
        "oi-capability: register() called — awaiting Phase 5 integration. "
        "23 tools NOT registered yet (consent + sandbox gates required). "
        "See docs/f7/interweaving-plan.md."
    )
    # Phase 5 will add tool registration here after consent + sandbox gates are wired.
    # See docs/f7/interweaving-plan.md for the refactor contract.
    return
