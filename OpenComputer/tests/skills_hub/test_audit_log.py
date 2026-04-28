"""Tests for Skills Hub append-only audit log (JSONL)."""
import json

import pytest

from opencomputer.skills_hub.audit_log import AuditLog


def test_empty_audit_log_returns_empty(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    assert log.entries() == []


def test_record_install_event(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    log.record(
        action="install",
        identifier="well-known/foo",
        source="well-known",
        version="1.0.0",
        verdict="safe",
    )
    entries = log.entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "install"
    assert entries[0]["verdict"] == "safe"


def test_audit_log_is_append_only(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(p)
    log.record(action="install", identifier="x", source="w", version="1", verdict="safe")
    raw_after_first = p.read_text()
    log.record(action="uninstall", identifier="x", source="w")
    raw_after_second = p.read_text()
    assert raw_after_second.startswith(raw_after_first)


def test_audit_log_jsonl_format(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(p)
    log.record(action="install", identifier="x", source="w", version="1", verdict="safe")
    log.record(action="uninstall", identifier="x", source="w")
    lines = p.read_text().strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        json.loads(line)


def test_audit_log_filter_by_action(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    log.record(action="install", identifier="a", source="w", version="1", verdict="safe")
    log.record(action="uninstall", identifier="a", source="w")
    log.record(action="install", identifier="b", source="w", version="1", verdict="safe")
    installs = log.entries(action="install")
    assert len(installs) == 2
    uninstalls = log.entries(action="uninstall")
    assert len(uninstalls) == 1


def test_audit_log_unknown_action_raises(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    with pytest.raises(ValueError, match="unknown action"):
        log.record(action="hack", identifier="x", source="w")


def test_audit_log_records_extra_fields(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    log.record(
        action="scan_blocked",
        identifier="x",
        source="w",
        verdict="dangerous",
        decision_reason="explicit shell escape",
    )
    entry = log.entries()[0]
    assert entry["decision_reason"] == "explicit shell escape"
