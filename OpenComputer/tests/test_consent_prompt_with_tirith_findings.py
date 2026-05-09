"""Hermes parity: Tirith findings surface in the consent prompt before user approves."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from opencomputer.agent.consent.audit import AuditLogger
from opencomputer.agent.consent.gate import ConsentGate
from opencomputer.agent.consent.store import ConsentStore
from opencomputer.agent.state import apply_migrations
from plugin_sdk import CapabilityClaim, ConsentTier


def _gate():
    tmp = Path(tempfile.mkdtemp())
    c = sqlite3.connect(tmp / "t.db", check_same_thread=False)
    apply_migrations(c)
    store = ConsentStore(c)
    log = AuditLogger(c, hmac_key=b"k" * 16)
    return ConsentGate(store=store, audit=log)


def _claim() -> CapabilityClaim:
    return CapabilityClaim(
        "execute_code.run", ConsentTier.PER_ACTION, "run user code",
    )


def test_render_prompt_appends_findings_when_session_has_pending_findings():
    gate = _gate()
    gate._pending_tirith_findings[("s1", "execute_code.run")] = (
        "[block] curl-pipe-to-shell pattern: untrusted RCE"
    )
    msg = gate.render_prompt(_claim(), None, session_id="s1")
    assert "Tirith" in msg
    assert "curl-pipe-to-shell" in msg
    # Base 4-verb prompt still there.
    assert "[y/N/session/always]" in msg


def test_render_prompt_no_findings_when_session_has_no_pending():
    gate = _gate()
    msg = gate.render_prompt(_claim(), None, session_id="s1")
    assert "Tirith" not in msg
    assert "[y/N/session/always]" in msg


def test_render_prompt_no_findings_for_other_session():
    gate = _gate()
    gate._pending_tirith_findings[("s1", "execute_code.run")] = "should not leak"
    msg = gate.render_prompt(_claim(), None, session_id="s2")
    assert "should not leak" not in msg


def test_render_prompt_legacy_no_session_id_returns_base_prompt():
    """Old callers passing only (claim, scope) must keep working."""
    gate = _gate()
    gate._pending_tirith_findings[("s1", "execute_code.run")] = "stash"
    msg = gate.render_prompt(_claim(), None)  # no session_id
    assert "Tirith" not in msg
    assert "[y/N/session/always]" in msg
