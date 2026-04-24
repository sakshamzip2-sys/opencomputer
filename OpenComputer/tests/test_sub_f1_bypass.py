"""BypassManager — emergency consent bypass with audit."""
from opencomputer.agent.consent.bypass import BypassManager


def test_inactive_by_default(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_CONSENT_BYPASS", raising=False)
    assert BypassManager.is_active() is False


def test_active_via_env_1(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_CONSENT_BYPASS", "1")
    assert BypassManager.is_active() is True


def test_active_via_env_true(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_CONSENT_BYPASS", "true")
    assert BypassManager.is_active() is True


def test_inactive_via_empty(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_CONSENT_BYPASS", "")
    assert BypassManager.is_active() is False


def test_inactive_via_zero(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_CONSENT_BYPASS", "0")
    assert BypassManager.is_active() is False


def test_banner_contains_warning():
    text = BypassManager.banner()
    assert "CONSENT BYPASS ACTIVE" in text
    assert "OPENCOMPUTER_CONSENT_BYPASS" in text
