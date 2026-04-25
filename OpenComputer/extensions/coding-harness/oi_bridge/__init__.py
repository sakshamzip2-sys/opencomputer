"""OI bridge — Open Interpreter interwoven into coding-harness (PR-3, 2026-04-25).

This package was refactored from the standalone extensions/oi-capability/
plugin per docs/f7/interweaving-plan.md. Tools are registered by
extensions/coding-harness/plugin.py alongside the standard coding tools.

AGPL boundary: open-interpreter (AGPL v3) runs ONLY in the isolated
subprocess venv managed by subprocess/server.py. No `import interpreter`
outside that file.

See docs/f7/design.md for the full architectural rationale.
"""

from __future__ import annotations
