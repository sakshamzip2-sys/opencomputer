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


# ─── H1 regression: path-anchored prefix match (no scope escape) ───


def test_prefix_match_rejects_scope_escape():
    """Regression for review finding H1. A grant on `/Users/saksham/Projects`
    must NOT allow a call on `/Users/saksham/Projects-secret/anything` —
    that's a scope escape bug (`startswith` is too permissive without anchor).
    """
    c, store, log, gate = _setup()
    store.upsert(ConsentGrant(
        "read_files", ConsentTier.EXPLICIT,
        "/Users/saksham/Projects", time.time(), None, "user",
    ))
    claim = CapabilityClaim("read_files", ConsentTier.EXPLICIT, "")
    # Legitimate subpath: allowed
    allowed = gate.check(
        claim, scope="/Users/saksham/Projects/foo.py", session_id="s1",
    )
    assert allowed.allowed is True
    # Scope escape attempt: must be denied
    denied = gate.check(
        claim, scope="/Users/saksham/Projects-secret/.env", session_id="s1",
    )
    assert denied.allowed is False, (
        "scope escape: /Projects-secret must NOT match grant on /Projects"
    )


def test_prefix_match_allows_exact_directory_match():
    """A grant on a directory path must also match the directory itself."""
    c, store, log, gate = _setup()
    store.upsert(ConsentGrant(
        "read_files", ConsentTier.EXPLICIT,
        "/a/b", time.time(), None, "user",
    ))
    claim = CapabilityClaim("read_files", ConsentTier.EXPLICIT, "")
    d = gate.check(claim, scope="/a/b", session_id="s1")
    assert d.allowed is True


def test_prefix_match_handles_trailing_slash_in_filter():
    """Grant stored with trailing slash still matches anchored paths."""
    c, store, log, gate = _setup()
    store.upsert(ConsentGrant(
        "read_files", ConsentTier.EXPLICIT,
        "/a/b/", time.time(), None, "user",
    ))
    claim = CapabilityClaim("read_files", ConsentTier.EXPLICIT, "")
    d_sub = gate.check(claim, scope="/a/b/c.py", session_id="s1")
    d_escape = gate.check(claim, scope="/a/b-other/c.py", session_id="s1")
    assert d_sub.allowed is True
    assert d_escape.allowed is False


# ─── 2.B.2: per-resource consent prompt rendering ────────────────────


def test_render_prompt_includes_scope_when_present():
    """The prompt names the specific resource being accessed."""
    from opencomputer.agent.consent.gate import render_prompt_message

    claim = CapabilityClaim(
        "read_files.metadata", ConsentTier.PER_ACTION, "",
    )
    msg = render_prompt_message(claim, "/Users/saksham/Projects/foo.py")
    assert "read_files.metadata" in msg
    assert "/Users/saksham/Projects/foo.py" in msg
    assert " on " in msg
    assert "[y/N/always]" in msg


def test_render_prompt_falls_back_when_no_scope():
    """Without a scope the prompt is the generic capability-only form."""
    from opencomputer.agent.consent.gate import render_prompt_message

    claim = CapabilityClaim(
        "read_files.metadata", ConsentTier.PER_ACTION, "",
    )
    msg = render_prompt_message(claim, None)
    assert msg == "Allow read_files.metadata? [y/N/always]"


def test_check_deny_reason_includes_scope_aware_prompt_text():
    """When the gate denies and a scope is known, the deny reason embeds the
    scope-aware prompt — so callers surfacing reason to the user see the
    specific resource, not just the capability class.
    """
    c, store, log, gate = _setup()
    claim = CapabilityClaim(
        "read_files.metadata", ConsentTier.PER_ACTION, "",
    )
    d = gate.check(
        claim,
        scope="/Users/saksham/Projects/foo.py",
        session_id="s1",
    )
    assert d.allowed is False
    assert "/Users/saksham/Projects/foo.py" in d.reason
    assert "read_files.metadata" in d.reason


# ─── 2.B.3: consent-expiry regression ────────────────────────────────


def test_grant_expiry_is_rechecked_per_call():
    """Regression for F1 2.B.3 — expiry MUST be enforced at every gate.check.

    Seed a grant with expires_at slightly in the future, call the gate
    once (allowed), wait for expiry, call again — the second call must
    deny ("no grant for capability") because ConsentStore.get filters
    out expired rows at read time.
    """
    c, store, log, gate = _setup()
    now = time.time()
    store.upsert(ConsentGrant(
        "read_files", ConsentTier.EXPLICIT, None,
        now, now + 1.0, "user",
    ))
    claim = CapabilityClaim("read_files", ConsentTier.EXPLICIT, "")
    d_first = gate.check(claim, scope=None, session_id="s1")
    assert d_first.allowed is True, (
        "first call should hit the still-valid grant"
    )
    # Sleep until past expiry.
    time.sleep(1.2)
    d_second = gate.check(claim, scope=None, session_id="s1")
    assert d_second.allowed is False
    assert "no grant" in d_second.reason.lower(), (
        "after expiry the gate should treat the grant as absent and deny"
    )
