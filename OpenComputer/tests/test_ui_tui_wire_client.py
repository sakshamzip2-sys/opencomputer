"""Contract test — the TUI's TypeScript wire client must cover the whole
Python wire protocol.

TUI-parity Milestone 2 (spec: docs/superpowers/specs/2026-05-17-tui-parity/
TUI.md). The M1 audit found OC shipped only a compiled ``ui-tui/dist/`` with
no source in the repo. M2 starts OC's real TUI source tree at
``opencomputer/ui-tui/src/`` — ``protocol.ts`` (constants + types) and
``wireClient.ts`` (the 27-method JSON-RPC client).

This test pins the cross-language contract so the two halves cannot drift:
every ``METHOD_*`` the Python wire server (``gateway/protocol.py``) defines
MUST have a matching string constant in ``protocol.ts`` and a call site in
``wireClient.ts``. If a future batch adds a wire method on the Python side
without updating the TS client, this test fails loudly in CI rather than
the TUI silently lacking the capability.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PROTOCOL_PY = _REPO / "opencomputer" / "gateway" / "protocol.py"
_PROTOCOL_TS = _REPO / "opencomputer" / "ui-tui" / "src" / "protocol.ts"
_WIRE_TS = _REPO / "opencomputer" / "ui-tui" / "src" / "wireClient.ts"


def _python_method_values() -> set[str]:
    """Every ``METHOD_X = "value"`` string defined in gateway/protocol.py."""
    text = _PROTOCOL_PY.read_text(encoding="utf-8")
    return set(re.findall(r'METHOD_\w+\s*=\s*"([^"]+)"', text))


def test_tui_source_tree_exists() -> None:
    """OC's TUI now ships real source, not just a compiled dist/."""
    assert _PROTOCOL_TS.is_file(), "missing opencomputer/ui-tui/src/protocol.ts"
    assert _WIRE_TS.is_file(), "missing opencomputer/ui-tui/src/wireClient.ts"


def test_python_protocol_has_methods() -> None:
    """Guard the regex itself — a parse drift must not pass silently."""
    methods = _python_method_values()
    assert len(methods) >= 25, f"expected 25+ wire methods, found {len(methods)}"
    assert "hello" in methods and "session.resume" in methods


def test_protocol_ts_covers_every_server_method() -> None:
    """protocol.ts METHOD constants ⊇ every Python METHOD_* value."""
    methods = _python_method_values()
    ts = _PROTOCOL_TS.read_text(encoding="utf-8")
    missing = sorted(m for m in methods if f'"{m}"' not in ts)
    assert not missing, (
        f"protocol.ts is missing server methods {missing} — the TUI "
        f"client would silently lack these RPCs"
    )


def test_wire_client_has_a_wrapper_per_method() -> None:
    """wireClient.ts must have at least one call() site per server method."""
    methods = _python_method_values()
    ts = _WIRE_TS.read_text(encoding="utf-8")
    call_sites = ts.count("this.call")
    assert call_sites >= len(methods), (
        f"wireClient.ts has {call_sites} call() sites but the server "
        f"exposes {len(methods)} methods — a typed wrapper is missing"
    )


def test_wire_client_imports_protocol_constants() -> None:
    """The client must source method names from protocol.ts, not inline
    string literals — the single source of truth keeps the two in sync."""
    ts = _WIRE_TS.read_text(encoding="utf-8")
    assert 'from "./protocol.js"' in ts
    assert "METHOD." in ts
