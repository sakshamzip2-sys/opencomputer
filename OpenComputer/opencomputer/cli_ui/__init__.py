"""Terminal UI helpers (Round 5 — Grok-style chat experience).

Kept under ``cli_ui/`` (not ``cli/``) to avoid colliding with
``cli.py``. Exports stay minimal — :class:`StreamingRenderer` and the
``current_renderer()`` accessor used by the hook subscriber in
``cli.py`` to deliver tool-call status events to the active renderer.
"""

from opencomputer.cli_ui.streaming import (
    StreamingRenderer,
    current_renderer,
)

__all__ = ["StreamingRenderer", "current_renderer"]
