"""Terminal bell helper — emit ``\\a`` after long-running turns.

Tier 2.B XS-effort feature from docs/refs/hermes-agent/2026-04-28-major-gaps.md.
The user enables it with ``/bell on``; the chat loop calls
``maybe_emit_bell(runtime)`` at turn-complete time.

Reads ``runtime.custom['bell_on_complete']``. Quietly no-ops when
unset / False / not in a TTY.
"""

from __future__ import annotations

import sys

from plugin_sdk.runtime_context import RuntimeContext


def maybe_emit_bell(runtime: RuntimeContext, *, stream=None) -> bool:
    """Emit ``\\a`` to the given stream (default: stderr) if bell is on.

    Returns True if a bell was emitted, False otherwise. The function
    only emits when:
      - ``runtime.custom['bell_on_complete']`` is truthy
      - ``stream.isatty()`` returns True (so piped output isn't polluted)
    """
    if not runtime or not runtime.custom.get("bell_on_complete"):
        return False
    out = stream if stream is not None else sys.stderr
    is_tty = getattr(out, "isatty", lambda: False)
    try:
        if not is_tty():
            return False
    except Exception:  # noqa: BLE001
        return False
    try:
        out.write("\a")
        try:
            out.flush()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        return False
    return True


__all__ = ["maybe_emit_bell"]
