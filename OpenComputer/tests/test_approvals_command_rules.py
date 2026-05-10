"""Per-command pattern approvals — OpenClaw parity.

Pins:
* matcher semantics (substring / glob / regex)
* first-match-wins ordering
* malformed rules degrade safely (don't break evaluation)
* YAML parser accepts both list-of-rules and short-form mapping
"""
from __future__ import annotations

import logging

from opencomputer.security.approvals import (
    ApprovalsConfig,
    CommandRule,
    parse_command_rules,
)

# ─── matchers ─────────────────────────────────────────────────────────


def test_substring_matcher_default():
    cfg = ApprovalsConfig(
        command_rules=(
            CommandRule(pattern="git push", verdict="ask"),
        ),
    )
    assert cfg.evaluate_command("git push origin main") == "ask"
    assert cfg.evaluate_command("git status") is None


def test_glob_matcher():
    cfg = ApprovalsConfig(
        command_rules=(
            CommandRule(pattern="rm -rf /tmp/*", verdict="allow", matcher="glob"),
        ),
    )
    assert cfg.evaluate_command("rm -rf /tmp/anything") == "allow"
    assert cfg.evaluate_command("rm -rf /etc/anything") is None


def test_regex_matcher():
    cfg = ApprovalsConfig(
        command_rules=(
            CommandRule(
                pattern=r"^git\s+push\s+--force\b", verdict="deny", matcher="regex",
            ),
        ),
    )
    assert cfg.evaluate_command("git push --force origin main") == "deny"
    assert cfg.evaluate_command("git push origin main") is None


def test_invalid_regex_does_not_crash(caplog):
    cfg = ApprovalsConfig(
        command_rules=(
            CommandRule(pattern="(unclosed", verdict="deny", matcher="regex"),
            CommandRule(pattern="ls", verdict="allow"),  # subsequent rule still fires
        ),
    )
    with caplog.at_level(logging.WARNING, logger="opencomputer.security.approvals"):
        # The bad regex is skipped; the second rule still matches.
        assert cfg.evaluate_command("ls -la") == "allow"
    assert any("invalid regex" in r.message for r in caplog.records)


# ─── ordering ─────────────────────────────────────────────────────────


def test_first_match_wins():
    cfg = ApprovalsConfig(
        command_rules=(
            CommandRule(pattern="git push", verdict="ask"),
            CommandRule(pattern="git", verdict="allow"),  # would also match
        ),
    )
    assert cfg.evaluate_command("git push origin") == "ask"


def test_no_rule_matches_returns_none():
    cfg = ApprovalsConfig(
        command_rules=(
            CommandRule(pattern="git", verdict="allow"),
        ),
    )
    assert cfg.evaluate_command("npm install") is None


def test_empty_rules_returns_none():
    cfg = ApprovalsConfig()
    assert cfg.evaluate_command("anything") is None


# ─── parser ───────────────────────────────────────────────────────────


def test_parse_list_form():
    raw = [
        {"pattern": "git commit", "verdict": "allow"},
        {"pattern": "git push", "verdict": "ask"},
        {"pattern": "rm -rf", "verdict": "deny"},
    ]
    rules = parse_command_rules(raw)
    assert len(rules) == 3
    assert rules[0] == CommandRule(pattern="git commit", verdict="allow")
    assert rules[2].verdict == "deny"


def test_parse_list_with_matcher():
    raw = [
        {"pattern": "rm -rf *", "verdict": "deny", "matcher": "glob"},
        {"pattern": r"^sudo", "verdict": "ask", "matcher": "regex"},
    ]
    rules = parse_command_rules(raw)
    assert rules[0].matcher == "glob"
    assert rules[1].matcher == "regex"


def test_parse_short_mapping_form():
    raw = {
        "git commit": "allow",
        "git push": "ask",
        "rm -rf": "deny",
    }
    rules = parse_command_rules(raw)
    # Must produce 3 rules (order is dict insertion order in Py 3.7+).
    assert len(rules) == 3
    by_pattern = {r.pattern: r for r in rules}
    assert by_pattern["git commit"].verdict == "allow"
    assert by_pattern["git push"].verdict == "ask"
    assert by_pattern["rm -rf"].verdict == "deny"


def test_parse_skips_unknown_verdicts(caplog):
    raw = [
        {"pattern": "x", "verdict": "yolo"},   # invalid
        {"pattern": "y", "verdict": "deny"},
    ]
    with caplog.at_level(logging.WARNING, logger="opencomputer.security.approvals"):
        rules = parse_command_rules(raw)
    assert len(rules) == 1
    assert rules[0].pattern == "y"
    assert any("unknown verdict" in r.message for r in caplog.records)


def test_parse_skips_blank_patterns():
    raw = [{"pattern": "", "verdict": "allow"}, {"pattern": "good", "verdict": "ask"}]
    rules = parse_command_rules(raw)
    assert len(rules) == 1
    assert rules[0].pattern == "good"


def test_parse_skips_non_dict_entries():
    raw = ["bare-string", 42, {"pattern": "x", "verdict": "deny"}]
    rules = parse_command_rules(raw)
    assert len(rules) == 1
    assert rules[0].pattern == "x"


def test_parse_handles_unknown_matcher_as_substring():
    raw = [{"pattern": "x", "verdict": "deny", "matcher": "yolo"}]
    rules = parse_command_rules(raw)
    assert rules[0].matcher == "substring"


def test_parse_returns_empty_for_invalid_root():
    assert parse_command_rules("nope") == ()
    assert parse_command_rules(None) == ()
    assert parse_command_rules(42) == ()


# ─── end-to-end via load_approvals_from_active_config ─────────────────


def test_load_from_yaml_with_command_rules(tmp_path, monkeypatch):
    """End-to-end: write a profile config.yaml; load returns parsed rules."""
    import yaml

    # Stand up a minimal profile env.
    profile_home = tmp_path / "default"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(yaml.safe_dump({
        "security": {
            "approvals": {
                "mode": "manual",
                "timeout": 60,
                "command_rules": [
                    {"pattern": "git push --force", "verdict": "deny"},
                    {"pattern": "git push", "verdict": "ask"},
                    {"pattern": "git commit", "verdict": "allow"},
                ],
            },
        },
    }))

    # Stub the profile resolver to point at our tmp_path.
    import opencomputer.security.approvals as approvals_mod

    def fake_read_active() -> str:
        return "default"

    def fake_home_dir(name: str):
        return profile_home

    monkeypatch.setattr(
        approvals_mod,
        "load_approvals_from_active_config",
        approvals_mod.load_approvals_from_active_config,
    )
    # Patch the underlying functions used inside load_approvals_from_active_config.
    import opencomputer.profiles as profiles_mod  # local import

    monkeypatch.setattr(profiles_mod, "read_active_profile", fake_read_active)
    monkeypatch.setattr(profiles_mod, "profile_home_dir", fake_home_dir)

    cfg = approvals_mod.load_approvals_from_active_config()
    assert cfg.mode == "manual"
    assert cfg.timeout_s == 60.0
    assert len(cfg.command_rules) == 3
    assert cfg.evaluate_command("git push --force origin") == "deny"
    assert cfg.evaluate_command("git push origin") == "ask"
    assert cfg.evaluate_command("git commit -m foo") == "allow"
    assert cfg.evaluate_command("ls") is None
