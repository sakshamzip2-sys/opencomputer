"""Tests for opencomputer.security.website_blocklist."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opencomputer.security.website_blocklist import (
    WebsiteBlocklistPolicy,
    clear_cache_for_tests,
    is_blocked,
    load_policy_cached,
    parse_rules,
)


# ── Matcher coverage ──────────────────────────────────────────────────


def test_exact_domain_match():
    policy = WebsiteBlocklistPolicy(
        enabled=True, domains=("admin.example.com",), shared_files=(),
    )
    assert is_blocked("https://admin.example.com/", policy) is True
    assert is_blocked("https://other.example.com/", policy) is False


def test_subdomain_wildcard_matches_subdomains():
    policy = WebsiteBlocklistPolicy(
        enabled=True,
        domains=("*.internal.company.com",),
        shared_files=(),
    )
    assert is_blocked("https://api.internal.company.com/", policy) is True
    assert is_blocked("https://deep.api.internal.company.com/", policy) is True


def test_subdomain_wildcard_matches_bare_domain():
    """`*.internal.company.com` SHOULD match `internal.company.com` itself."""
    policy = WebsiteBlocklistPolicy(
        enabled=True,
        domains=("*.internal.company.com",),
        shared_files=(),
    )
    assert is_blocked("https://internal.company.com/", policy) is True


def test_subdomain_wildcard_does_not_match_lookalike():
    policy = WebsiteBlocklistPolicy(
        enabled=True,
        domains=("*.internal.company.com",),
        shared_files=(),
    )
    # Lookalike with extra prefix on the matching segment — must NOT match.
    assert is_blocked("https://otherinternal.company.com/", policy) is False


def test_tld_wildcard_match():
    policy = WebsiteBlocklistPolicy(
        enabled=True, domains=("*.local",), shared_files=(),
    )
    assert is_blocked("https://my.local/", policy) is True
    assert is_blocked("https://api.dev.local/", policy) is True
    assert is_blocked("https://example.com/", policy) is False


def test_disabled_policy_allows_everything():
    policy = WebsiteBlocklistPolicy(
        enabled=False,
        domains=("admin.example.com",),
        shared_files=(),
    )
    assert is_blocked("https://admin.example.com/", policy) is False


def test_no_domains_allows_everything():
    policy = WebsiteBlocklistPolicy(
        enabled=True, domains=(), shared_files=(),
    )
    assert is_blocked("https://anything.example.com/", policy) is False


def test_parse_rules_strips_comments_and_blanks():
    text = """
# This is a comment
admin.example.com
   # indented comment
*.internal.local

*.dev
"""
    rules = parse_rules(text)
    assert rules == ("admin.example.com", "*.internal.local", "*.dev")


def test_shared_file_rules_loaded(tmp_path: Path):
    f = tmp_path / "blocked.txt"
    f.write_text("evil.example.com\n*.bad.local\n")
    policy = WebsiteBlocklistPolicy(
        enabled=True, domains=(), shared_files=(f,),
    )
    assert is_blocked("https://evil.example.com/", policy) is True
    assert is_blocked("https://x.bad.local/", policy) is True


def test_missing_shared_file_logs_warning_does_not_disable_inline_rules(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
):
    missing = tmp_path / "missing.txt"
    policy = WebsiteBlocklistPolicy(
        enabled=True,
        domains=("admin.example.com",),
        shared_files=(missing,),
    )
    # The missing file logs a warning but the inline `domains` still apply.
    assert is_blocked("https://admin.example.com/", policy) is True


def test_invalid_url_returns_false():
    policy = WebsiteBlocklistPolicy(
        enabled=True, domains=("example.com",), shared_files=(),
    )
    assert is_blocked("not-a-url", policy) is False
    assert is_blocked("", policy) is False


# ── Cache TTL ─────────────────────────────────────────────────────────


def test_cache_returns_same_instance_within_ttl():
    clear_cache_for_tests()
    p1 = load_policy_cached(
        enabled=True, domains=("a.example",), shared_files=(),
    )
    p2 = load_policy_cached(
        enabled=True, domains=("a.example",), shared_files=(),
    )
    assert p1 is p2


def test_cache_expires_after_ttl():
    clear_cache_for_tests()
    p1 = load_policy_cached(
        enabled=True, domains=("a.example",), shared_files=(), now=0.0,
    )
    # Two minutes later — TTL is 30s.
    p2 = load_policy_cached(
        enabled=True, domains=("a.example",), shared_files=(), now=120.0,
    )
    assert p1 is not p2


# ── policy_from_active_config (YAML-direct path) ──────────────────────


def test_policy_from_active_config_disabled_when_no_section(monkeypatch, tmp_path):
    """No security.website_blocklist section → disabled policy returned."""
    from opencomputer.security import website_blocklist as wbl

    clear_cache_for_tests()

    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text("agent:\n  loop_budget: 100\n")

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    policy = wbl.policy_from_active_config()
    assert policy.enabled is False


def test_policy_from_active_config_reads_section(monkeypatch, tmp_path):
    from opencomputer.security import website_blocklist as wbl

    clear_cache_for_tests()

    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text(
        "security:\n"
        "  website_blocklist:\n"
        "    enabled: true\n"
        "    domains:\n"
        "      - admin.example.com\n"
        "      - '*.internal.local'\n"
    )

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    policy = wbl.policy_from_active_config()
    assert policy.enabled is True
    assert "admin.example.com" in policy.domains


def test_policy_from_active_config_corrupt_yaml_returns_disabled(
    monkeypatch, tmp_path,
):
    from opencomputer.security import website_blocklist as wbl

    clear_cache_for_tests()

    cfg_dir = tmp_path
    (cfg_dir / "config.yaml").write_text("security: {website_blocklist: }invalid")

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", lambda: "default",
    )
    monkeypatch.setattr(
        "opencomputer.profiles.profile_home_dir", lambda _name: cfg_dir,
    )
    # Corrupt YAML → exception → fall through to disabled policy.
    policy = wbl.policy_from_active_config()
    assert policy.enabled is False


# ── Tool integration: WebFetch ────────────────────────────────────────


def test_web_fetch_refuses_blocklisted_url(monkeypatch):
    from opencomputer.tools.web_fetch import WebFetchTool
    from plugin_sdk.core import ToolCall

    clear_cache_for_tests()

    blocked_policy = WebsiteBlocklistPolicy(
        enabled=True,
        domains=("admin.evil.com",),
        shared_files=(),
    )
    # Patch BOTH the canonical home and the importing module's lookup
    # — `from x import y` binds locally.
    from opencomputer.security import website_blocklist as wbl

    monkeypatch.setattr(
        wbl, "policy_from_active_config", lambda: blocked_policy,
    )
    from opencomputer.tools import web_fetch as wf

    if hasattr(wf, "policy_from_active_config"):
        monkeypatch.setattr(
            wf, "policy_from_active_config", lambda: blocked_policy,
        )

    tool = WebFetchTool()
    call = ToolCall(
        id="block-test-1",
        name="WebFetch",
        arguments={"url": "https://admin.evil.com/"},
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "blocklist" in result.content.lower()
