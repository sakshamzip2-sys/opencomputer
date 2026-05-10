"""M5-gap closure: persist_command_rule writer + ConsentGate consults command_rules.

Two parts:
1. persist_command_rule appends an entry to ``security.approvals.command_rules``
   in ``<profile>/config.yaml`` and the result round-trips through
   ``load_approvals_from_active_config`` cleanly. Used to implement
   "allow-always" persistence at consent prompt time.
2. ConsentGate.check() consults command_rules against the scope string —
   ``deny`` short-circuits to refused, ``allow`` short-circuits to
   allowed, ``ask``/no-match falls through to the normal grant flow.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import yaml

from opencomputer.agent.consent.audit import AuditLogger
from opencomputer.agent.consent.gate import ConsentGate
from opencomputer.agent.consent.store import ConsentStore
from opencomputer.agent.state import apply_migrations
from opencomputer.security.approvals import (
    ApprovalsConfig,
    CommandRule,
    persist_command_rule,
)
from plugin_sdk.consent import CapabilityClaim, ConsentTier

# ─── persist_command_rule ─────────────────────────────────────────────


def test_persist_creates_config_yaml_if_absent(tmp_path: Path):
    rule = CommandRule(pattern="git push --force", verdict="deny")
    written = persist_command_rule(rule, profile_home=tmp_path)
    assert written == tmp_path / "config.yaml"
    assert written.is_file()
    data = yaml.safe_load(written.read_text())
    rules = data["security"]["approvals"]["command_rules"]
    assert len(rules) == 1
    assert rules[0] == {
        "pattern": "git push --force",
        "verdict": "deny",
        "matcher": "substring",
    }


def test_persist_appends_to_existing_rules(tmp_path: Path):
    existing = {
        "security": {"approvals": {"command_rules": [
            {"pattern": "git commit", "verdict": "allow", "matcher": "substring"},
        ]}},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(existing))
    persist_command_rule(
        CommandRule(pattern="npm install", verdict="ask"),
        profile_home=tmp_path,
    )
    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    rules = data["security"]["approvals"]["command_rules"]
    assert len(rules) == 2
    patterns = {r["pattern"] for r in rules}
    assert patterns == {"git commit", "npm install"}


def test_persist_preserves_other_config_keys(tmp_path: Path):
    existing = {
        "model": {"name": "claude-opus-4-7"},
        "loop": {"max_iterations": 50},
        "security": {"approvals": {"mode": "manual"}},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(existing))
    persist_command_rule(
        CommandRule(pattern="rm -rf", verdict="deny"),
        profile_home=tmp_path,
    )
    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert data["model"]["name"] == "claude-opus-4-7"
    assert data["loop"]["max_iterations"] == 50
    assert data["security"]["approvals"]["mode"] == "manual"
    rules = data["security"]["approvals"]["command_rules"]
    assert rules[0]["pattern"] == "rm -rf"


def test_persist_atomic_no_partial_file_on_crash(tmp_path: Path):
    """The write goes via .yaml.tmp + os.replace so a crash mid-write
    cannot leave a half-rendered config.yaml. We can't simulate a real
    crash, but we can verify the tmp file is cleaned up on success."""
    persist_command_rule(
        CommandRule(pattern="x", verdict="allow"),
        profile_home=tmp_path,
    )
    assert not (tmp_path / "config.yaml.tmp").exists()
    assert (tmp_path / "config.yaml").is_file()


def test_persist_replaces_malformed_top_level(tmp_path: Path, caplog):
    """A non-dict top-level (e.g. user pasted a list by mistake) is
    replaced with a minimal valid config carrying the new rule."""
    (tmp_path / "config.yaml").write_text("- some list\n- not a dict\n")
    import logging
    with caplog.at_level(logging.WARNING, logger="opencomputer.security.approvals"):
        persist_command_rule(
            CommandRule(pattern="ls", verdict="allow"),
            profile_home=tmp_path,
        )
    assert any("not dict" in r.message for r in caplog.records)
    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert isinstance(data, dict)
    assert data["security"]["approvals"]["command_rules"][0]["pattern"] == "ls"


def test_persist_then_load_round_trips(tmp_path: Path, monkeypatch):
    persist_command_rule(
        CommandRule(pattern="git push", verdict="ask", matcher="substring"),
        profile_home=tmp_path,
    )
    persist_command_rule(
        CommandRule(pattern="rm -rf", verdict="deny"),
        profile_home=tmp_path,
    )
    # Inject profile resolver to point at tmp_path.
    import opencomputer.profiles as profiles_mod
    import opencomputer.security.approvals as approvals_mod
    monkeypatch.setattr(profiles_mod, "read_active_profile", lambda: "default")
    monkeypatch.setattr(profiles_mod, "profile_home_dir", lambda n: tmp_path)
    cfg = approvals_mod.load_approvals_from_active_config()
    assert len(cfg.command_rules) == 2
    assert cfg.evaluate_command("git push origin") == "ask"
    assert cfg.evaluate_command("rm -rf /tmp") == "deny"


def test_persist_uses_active_profile_when_home_not_given(tmp_path: Path, monkeypatch):
    """No profile_home arg → resolver path is hit; we stub it to tmp_path."""
    import opencomputer.profiles as profiles_mod
    monkeypatch.setattr(profiles_mod, "read_active_profile", lambda: "default")
    monkeypatch.setattr(profiles_mod, "profile_home_dir", lambda n: tmp_path)
    written = persist_command_rule(CommandRule(pattern="x", verdict="allow"))
    assert written == tmp_path / "config.yaml"


def test_persist_no_active_profile_raises(monkeypatch):
    """When no profile is active, persist refuses rather than writing
    to an unknown location."""
    import opencomputer.profiles as profiles_mod
    from opencomputer.security.approvals import SecretsApprovalsError
    monkeypatch.setattr(profiles_mod, "read_active_profile", lambda: None)
    import pytest
    with pytest.raises(SecretsApprovalsError, match="no active profile"):
        persist_command_rule(CommandRule(pattern="x", verdict="allow"))


# ─── ConsentGate consultation ─────────────────────────────────────────


def _make_gate() -> ConsentGate:
    """Build a gate with a real in-memory SQLite + applied migrations."""
    tmp = Path(tempfile.mkdtemp())
    conn = sqlite3.connect(tmp / "t.db", check_same_thread=False)
    apply_migrations(conn)
    store = ConsentStore(conn)
    audit = AuditLogger(conn, hmac_key=b"k" * 16)
    return ConsentGate(store=store, audit=audit)


def _claim(cap_id: str = "bash.exec", tier: ConsentTier = ConsentTier.PER_ACTION) -> CapabilityClaim:
    return CapabilityClaim(
        capability_id=cap_id,
        tier_required=tier,
        human_description="bash command",
    )


def test_consent_gate_command_rule_deny_short_circuits():
    gate = _make_gate()
    # Pre-populate gate's approvals cache with a deny rule.
    gate._approvals_config = ApprovalsConfig(
        command_rules=(CommandRule(pattern="rm -rf /etc", verdict="deny"),),
    )
    decision = gate.check(_claim(), scope="rm -rf /etc/passwd", session_id="s1")
    assert decision.allowed is False
    assert "command_rules" in decision.reason
    assert "deny" in decision.reason


def test_consent_gate_command_rule_allow_short_circuits():
    gate = _make_gate()
    gate._approvals_config = ApprovalsConfig(
        command_rules=(CommandRule(pattern="git status", verdict="allow"),),
    )
    decision = gate.check(_claim(), scope="git status", session_id="s1")
    assert decision.allowed is True
    assert "command_rules" in decision.reason
    assert "allow" in decision.reason


def test_consent_gate_command_rule_ask_falls_through():
    """An ``ask`` verdict should NOT short-circuit — the normal grant
    flow continues. With no grant present, the gate denies."""
    gate = _make_gate()
    gate._approvals_config = ApprovalsConfig(
        command_rules=(CommandRule(pattern="some-cmd", verdict="ask"),),
    )
    decision = gate.check(_claim(), scope="some-cmd argv", session_id="s1")
    # No grant for "bash.exec", so check returns deny.
    assert decision.allowed is False
    # Reason should NOT mention command_rules — it fell through.
    assert "command_rules" not in decision.reason


def test_consent_gate_no_scope_skips_command_rule_check():
    """scope=None means there's no command string to pattern-match
    against; the command_rule branch must not fire."""
    gate = _make_gate()
    gate._approvals_config = ApprovalsConfig(
        command_rules=(CommandRule(pattern="rm -rf", verdict="deny"),),
    )
    decision = gate.check(_claim(), scope=None, session_id="s1")
    assert "command_rules" not in decision.reason


def test_consent_gate_auto_allow_takes_precedence_over_command_rules():
    """When mode=off, auto_allow returns allowed=True BEFORE
    command_rules deny gets consulted. Operators using --auto have
    explicitly opted out of all prompting."""
    gate = _make_gate()
    gate._approvals_config = ApprovalsConfig(
        mode="off",
        command_rules=(CommandRule(pattern="rm -rf", verdict="deny"),),
    )
    decision = gate.check(_claim(), scope="rm -rf anything", session_id="s1")
    # auto_allow wins; the deny rule is skipped.
    assert decision.allowed is True
    assert "mode=off" in decision.reason
