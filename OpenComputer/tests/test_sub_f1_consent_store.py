"""ConsentStore — SQLite-backed grant CRUD, ACID across sessions."""
import sqlite3
import tempfile
import time
from pathlib import Path

from opencomputer.agent.consent.store import ConsentStore
from opencomputer.agent.state import apply_migrations
from plugin_sdk import ConsentGrant, ConsentTier


def _make_conn() -> sqlite3.Connection:
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    conn = sqlite3.connect(tmp, check_same_thread=False)
    apply_migrations(conn)
    return conn


def test_grant_and_get():
    conn = _make_conn()
    store = ConsentStore(conn)
    g = ConsentGrant(
        capability_id="read_files", tier=ConsentTier.EXPLICIT,
        scope_filter="/Users/saksham/Projects", granted_at=time.time(),
        expires_at=None, granted_by="user",
    )
    store.upsert(g)
    got = store.get("read_files", "/Users/saksham/Projects")
    assert got is not None
    assert got.capability_id == "read_files"
    assert got.granted_by == "user"
    assert got.tier == ConsentTier.EXPLICIT


def test_get_returns_none_when_no_match():
    conn = _make_conn()
    store = ConsentStore(conn)
    assert store.get("missing", None) is None


def test_expiry_enforcement():
    conn = _make_conn()
    store = ConsentStore(conn)
    past = time.time() - 1
    g = ConsentGrant(
        capability_id="x", tier=ConsentTier.EXPLICIT, scope_filter=None,
        granted_at=past - 10, expires_at=past, granted_by="user",
    )
    store.upsert(g)
    assert store.get("x", None) is None  # expired → treated as absent


def test_revoke_removes_grant():
    conn = _make_conn()
    store = ConsentStore(conn)
    g = ConsentGrant(
        capability_id="x", tier=ConsentTier.EXPLICIT, scope_filter=None,
        granted_at=time.time(), expires_at=None, granted_by="user",
    )
    store.upsert(g)
    store.revoke("x", None)
    assert store.get("x", None) is None


def test_list_all_active():
    conn = _make_conn()
    store = ConsentStore(conn)
    now = time.time()
    store.upsert(ConsentGrant("a", ConsentTier.EXPLICIT, None, now, None, "user"))
    store.upsert(ConsentGrant("b", ConsentTier.EXPLICIT, None, now, now - 1, "user"))  # expired
    active = store.list_active()
    ids = {g.capability_id for g in active}
    assert ids == {"a"}


def test_upsert_replaces_existing():
    conn = _make_conn()
    store = ConsentStore(conn)
    now = time.time()
    store.upsert(ConsentGrant("x", ConsentTier.EXPLICIT, None, now, None, "user"))
    store.upsert(ConsentGrant("x", ConsentTier.PER_ACTION, None, now, None, "auto"))
    got = store.get("x", None)
    assert got is not None
    assert got.tier == ConsentTier.PER_ACTION
    assert got.granted_by == "auto"


def test_scope_filter_nulls_match_separately():
    """A global grant (scope=None) and a scoped grant coexist for same capability."""
    conn = _make_conn()
    store = ConsentStore(conn)
    now = time.time()
    store.upsert(ConsentGrant("x", ConsentTier.EXPLICIT, None, now, None, "user"))
    store.upsert(ConsentGrant("x", ConsentTier.PER_ACTION, "/a", now, None, "user"))
    g_global = store.get("x", None)
    g_scoped = store.get("x", "/a")
    assert g_global is not None
    assert g_scoped is not None
    assert g_global.tier == ConsentTier.EXPLICIT
    assert g_scoped.tier == ConsentTier.PER_ACTION
