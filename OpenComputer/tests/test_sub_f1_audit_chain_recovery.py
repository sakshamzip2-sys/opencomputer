"""Chain head export/import for post-wipe recovery."""
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from opencomputer.agent.consent.audit import AuditEvent, AuditLogger
from opencomputer.agent.state import apply_migrations


def _setup():
    tmp = Path(tempfile.mkdtemp())
    c = sqlite3.connect(tmp / "t.db", check_same_thread=False)
    apply_migrations(c)
    log = AuditLogger(c, hmac_key=b"k" * 16)
    log.append(AuditEvent("s1", "user", "grant", "x", 1, None, "allow", ""))
    log.append(AuditEvent("s1", "user", "check", "x", 1, None, "allow", ""))
    return tmp, c, log


def test_export_chain_head_writes_file():
    tmp, c, log = _setup()
    out = tmp / "head.json"
    log.export_chain_head(out)
    assert out.exists()
    data = json.loads(out.read_text())
    assert "row_hmac" in data
    assert "row_id" in data
    assert "as_of" in data
    assert data["row_id"] == 2


def test_export_chain_head_on_empty_log(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", check_same_thread=False)
    apply_migrations(c)
    log = AuditLogger(c, hmac_key=b"k" * 16)
    out = tmp_path / "head.json"
    log.export_chain_head(out)
    data = json.loads(out.read_text())
    assert data["row_id"] == 0
    assert data["row_hmac"] == "0" * 64


def test_import_chain_head_accepts_matching_head():
    tmp, c, log = _setup()
    out = tmp / "head.json"
    log.export_chain_head(out)
    # Fresh logger over same DB should accept the imported head
    log2 = AuditLogger(c, hmac_key=b"k" * 16)
    log2.import_chain_head(out)  # no exception = accepted


def test_import_chain_head_rejects_mismatched_head(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db", check_same_thread=False)
    apply_migrations(c)
    log = AuditLogger(c, hmac_key=b"k" * 16)
    log.append(AuditEvent("s1", "user", "grant", "x", 1, None, "allow", ""))
    # Forge a "backup" with the wrong row_hmac
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "row_id": 1,
        "row_hmac": "f" * 64,
        "as_of": 100.0,
    }))
    with pytest.raises(ValueError, match="does not match"):
        log.import_chain_head(bad)


def test_restart_chain_appends_marker():
    tmp, c, log = _setup()
    log.restart_chain(reason="keyring_wipe_recovery")
    rows = c.execute(
        "SELECT actor, action, reason FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert rows == ("system", "chain_restart", "keyring_wipe_recovery")
