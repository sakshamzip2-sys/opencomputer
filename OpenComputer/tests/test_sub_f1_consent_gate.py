"""ConsentGate — resolves CapabilityClaim → ConsentDecision."""
import sqlite3
import tempfile
import time
from pathlib import Path

from opencomputer.agent.consent.audit import AuditLogger
from opencomputer.agent.consent.gate import ConsentGate
from opencomputer.agent.consent.store import ConsentStore
from opencomputer.agent.state import apply_migrations
from plugin_sdk import CapabilityClaim, ConsentGrant, ConsentTier


def _setup():
    tmp = Path(tempfile.mkdtemp())
    c = sqlite3.connect(tmp / "t.db", check_same_thread=False)
    apply_migrations(c)
    store = ConsentStore(c)
    log = AuditLogger(c, hmac_key=b"k" * 16)
    gate = ConsentGate(store=store, audit=log)
    return c, store, log, gate


def test_denies_when_no_grant():
    c, store, log, gate = _setup()
    claim = CapabilityClaim("read_files", ConsentTier.EXPLICIT, "")
    d = gate.check(claim, scope=None, session_id="s1")
    assert d.allowed is False
    assert "no grant" in d.reason.lower()
    assert d.audit_event_id is not None


def test_allows_with_matching_global_grant():
    c, store, log, gate = _setup()
    store.upsert(ConsentGrant(
        "read_files", ConsentTier.EXPLICIT, None,
        time.time(), None, "user",
    ))
    claim = CapabilityClaim("read_files", ConsentTier.EXPLICIT, "")
    d = gate.check(claim, scope=None, session_id="s1")
    assert d.allowed is True
    assert d.tier_matched == ConsentTier.EXPLICIT


def test_denies_when_grant_expired():
    c, store, log, gate = _setup()
    store.upsert(ConsentGrant(
        "read_files", ConsentTier.EXPLICIT, None,
        time.time() - 10, time.time() - 1, "user",
    ))
    claim = CapabilityClaim("read_files", ConsentTier.EXPLICIT, "")
    d = gate.check(claim, scope=None, session_id="s1")
    assert d.allowed is False


def test_scope_filter_allows_prefix_match():
    c, store, log, gate = _setup()
    store.upsert(ConsentGrant(
        "read_files", ConsentTier.EXPLICIT,
        "/Users/saksham/Projects", time.time(), None, "user",
    ))
    claim = CapabilityClaim("read_files", ConsentTier.EXPLICIT, "")
    d_in = gate.check(claim, scope="/Users/saksham/Projects/foo.py", session_id="s1")
    d_out = gate.check(claim, scope="/Users/saksham/Documents/bar.md", session_id="s1")
    assert d_in.allowed is True
    assert d_out.allowed is False


def test_denies_when_grant_tier_insufficient():
    c, store, log, gate = _setup()
    # Grant is only IMPLICIT but claim requires PER_ACTION
    store.upsert(ConsentGrant(
        "x", ConsentTier.IMPLICIT, None, time.time(), None, "user",
    ))
    claim = CapabilityClaim("x", ConsentTier.PER_ACTION, "")
    d = gate.check(claim, scope=None, session_id="s1")
    assert d.allowed is False
    assert "tier" in d.reason.lower()


def test_check_writes_audit_entry():
    c, store, log, gate = _setup()
    claim = CapabilityClaim("x", ConsentTier.EXPLICIT, "")
    gate.check(claim, scope=None, session_id="s1")
    rows = c.execute("SELECT action, decision FROM audit_log").fetchall()
    assert rows[0] == ("check", "deny")


def test_check_returns_audit_event_id():
    c, store, log, gate = _setup()
    claim = CapabilityClaim("x", ConsentTier.EXPLICIT, "")
    d = gate.check(claim, scope=None, session_id="s1")
    # The id points to a real row
    assert d.audit_event_id is not None
    row = c.execute(
        "SELECT action, decision FROM audit_log WHERE id=?",
        (d.audit_event_id,),
    ).fetchone()
    assert row == ("check", "deny")
