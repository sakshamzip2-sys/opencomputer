"""Tests for service-name hashing — multi-installation support.

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (Task 1.1).

Single-install (canonical OPENCOMPUTER_HOME + 'default' profile) preserves
the historical 'opencomputer-gateway' label so existing service files keep
working untouched. Multi-install (non-canonical HOME OR named profile)
appends a sha256[:8] hash so two daemons can coexist.
"""
from __future__ import annotations

import hashlib

import pytest

from opencomputer.service import _naming


def test_canonical_home_default_profile_returns_canonical_label(monkeypatch):
    """Default home + default profile → backward-compat label."""
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    assert _naming.service_label("default") == "opencomputer-gateway"


def test_canonical_home_named_profile_returns_hashed(monkeypatch):
    """Default home but a named profile → hashed (concurrent-install safe)."""
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    label = _naming.service_label("work")
    assert label.startswith("opencomputer-gateway-")
    assert len(label) == len("opencomputer-gateway-") + 8


def test_non_canonical_home_default_profile_returns_hashed(monkeypatch, tmp_path):
    """Non-canonical OPENCOMPUTER_HOME → hashed even for 'default'."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    label = _naming.service_label("default")
    assert label.startswith("opencomputer-gateway-")
    assert len(label) == len("opencomputer-gateway-") + 8


def test_distinct_homes_distinct_labels(monkeypatch, tmp_path):
    """Two distinct OPENCOMPUTER_HOMEs must produce distinct labels."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "a"))
    a = _naming.service_label("default")
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "b"))
    b = _naming.service_label("default")
    assert a != b


def test_same_home_same_profile_deterministic(monkeypatch, tmp_path):
    """Hash must be deterministic across calls."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    a = _naming.service_label("default")
    b = _naming.service_label("default")
    assert a == b


def test_hash_label_suffix_format():
    """Helper produces 8 hex chars suitable for unit names."""
    suffix = _naming._hash_label_suffix("/home/u/.opencomputer", "default")
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)
    # Determinism is the contract — recompute and compare.
    expected = hashlib.sha256(b"/home/u/.opencomputer|default").hexdigest()[:8]
    assert suffix == expected


def test_canonical_label_constant_matches():
    """Sanity — the exposed canonical constant matches public expectation."""
    assert _naming._CANONICAL_LABEL == "opencomputer-gateway"
