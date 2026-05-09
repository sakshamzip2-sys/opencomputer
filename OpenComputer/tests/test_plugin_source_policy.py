"""Plugin source + policy tests (v1.1 plan-3 M11.3)."""

from __future__ import annotations

import pytest

from opencomputer.plugins.source_policy import (
    PluginSource,
    PluginSourceKind,
    PluginSourcePolicy,
    PolicyDeniedError,
    load_policy,
    parse_source,
)

# ─── parse_source ─────────────────────────────────────────────────


def test_parse_pypi_simple() -> None:
    src = parse_source("pypi:my-plugin")
    assert src.kind == PluginSourceKind.PYPI
    assert src.target == "my-plugin"
    assert src.ref is None


def test_parse_pypi_with_version() -> None:
    src = parse_source("pypi:my-plugin==1.2.3")
    assert src.kind == PluginSourceKind.PYPI
    assert src.target == "my-plugin"
    assert src.ref == "1.2.3"


def test_parse_github_short_gh_prefix() -> None:
    src = parse_source("gh:owner/repo")
    assert src.kind == PluginSourceKind.GITHUB
    assert src.target == "owner/repo"


def test_parse_github_short_github_prefix() -> None:
    src = parse_source("github:owner/repo")
    assert src.kind == PluginSourceKind.GITHUB
    assert src.target == "owner/repo"


def test_parse_github_short_with_ref() -> None:
    src = parse_source("gh:owner/repo@v1.0.0")
    assert src.kind == PluginSourceKind.GITHUB
    assert src.target == "owner/repo"
    assert src.ref == "v1.0.0"


def test_parse_github_https() -> None:
    src = parse_source("https://github.com/owner/repo")
    assert src.kind == PluginSourceKind.GITHUB
    assert src.target == "owner/repo"


def test_parse_github_https_with_dot_git() -> None:
    src = parse_source("https://github.com/owner/repo.git")
    assert src.kind == PluginSourceKind.GITHUB
    assert src.target == "owner/repo"


def test_parse_github_https_with_branch() -> None:
    src = parse_source("https://github.com/owner/repo/tree/main")
    assert src.kind == PluginSourceKind.GITHUB
    assert src.target == "owner/repo"
    assert src.ref == "main"


def test_parse_git_plus_https() -> None:
    src = parse_source("git+https://gitlab.com/foo/bar.git")
    assert src.kind == PluginSourceKind.GIT
    assert src.target == "git+https://gitlab.com/foo/bar.git"


def test_parse_git_plus_ssh() -> None:
    src = parse_source("git+ssh://git@example.com/foo/bar.git")
    assert src.kind == PluginSourceKind.GIT


def test_parse_url_non_github() -> None:
    src = parse_source("https://example.com/release.tar.gz")
    assert src.kind == PluginSourceKind.URL


def test_parse_directory() -> None:
    src = parse_source("/local/path/to/plugin")
    assert src.kind == PluginSourceKind.DIRECTORY
    assert src.target == "/local/path/to/plugin"


def test_parse_relative_directory() -> None:
    src = parse_source("./my-plugin")
    assert src.kind == PluginSourceKind.DIRECTORY


def test_parse_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_source("")


def test_parse_whitespace_only_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_source("   ")


# ─── policy default behavior ──────────────────────────────────────


def test_default_policy_allows_directory() -> None:
    policy = PluginSourcePolicy()
    src = parse_source("/some/dir")
    assert policy.is_allowed(src)


def test_default_policy_denies_pypi() -> None:
    policy = PluginSourcePolicy()
    src = parse_source("pypi:my-plugin")
    assert not policy.is_allowed(src)


def test_default_policy_denies_github() -> None:
    policy = PluginSourcePolicy()
    src = parse_source("gh:owner/repo")
    assert not policy.is_allowed(src)


def test_default_policy_denies_git() -> None:
    policy = PluginSourcePolicy()
    src = parse_source("git+https://example.com/foo.git")
    assert not policy.is_allowed(src)


def test_default_policy_denies_url() -> None:
    policy = PluginSourcePolicy()
    src = parse_source("https://example.com/x.tar.gz")
    assert not policy.is_allowed(src)


# ─── policy with allow list ───────────────────────────────────────


def test_pypi_allow_list_admits_match() -> None:
    policy = load_policy(
        {"sources": {"pypi": {"allow": ["opencomputer-*", "oc-*"]}}}
    )
    src = parse_source("pypi:opencomputer-foo")
    assert policy.is_allowed(src)


def test_pypi_allow_list_rejects_non_match() -> None:
    policy = load_policy(
        {"sources": {"pypi": {"allow": ["opencomputer-*"]}}}
    )
    src = parse_source("pypi:other-plugin")
    assert not policy.is_allowed(src)


def test_github_allow_list_admits_owner_glob() -> None:
    policy = load_policy(
        {"sources": {"github": {"allow": ["sakshamzip2-sys/*"]}}}
    )
    src = parse_source("gh:sakshamzip2-sys/oc-skill")
    assert policy.is_allowed(src)


def test_github_allow_rejects_other_owner() -> None:
    policy = load_policy(
        {"sources": {"github": {"allow": ["sakshamzip2-sys/*"]}}}
    )
    src = parse_source("gh:malicious/repo")
    assert not policy.is_allowed(src)


# ─── policy with deny list ────────────────────────────────────────


def test_pypi_deny_overrides_allow() -> None:
    """Deny always wins over allow — even with explicit allow on the same target."""
    policy = load_policy(
        {
            "sources": {
                "pypi": {
                    "allow": ["opencomputer-*"],
                    "deny": ["opencomputer-malware*"],
                }
            }
        }
    )
    src_clean = parse_source("pypi:opencomputer-foo")
    src_evil = parse_source("pypi:opencomputer-malware-x")
    assert policy.is_allowed(src_clean)
    assert not policy.is_allowed(src_evil)


def test_deny_star_disables_kind_entirely() -> None:
    """``deny: ['*']`` disables a source kind entirely."""
    policy = load_policy({"sources": {"git": {"deny": ["*"]}}})
    src = parse_source("git+https://example.com/x.git")
    assert not policy.is_allowed(src)


# ─── assert_allowed ──────────────────────────────────────────────


def test_assert_allowed_raises_on_denied() -> None:
    policy = PluginSourcePolicy()
    src = parse_source("pypi:my-plugin")
    with pytest.raises(PolicyDeniedError, match="not allowed by policy"):
        policy.assert_allowed(src)


def test_assert_allowed_silent_on_allowed() -> None:
    policy = PluginSourcePolicy()
    src = parse_source("/some/dir")
    policy.assert_allowed(src)  # no raise


# ─── load_policy validation ───────────────────────────────────────


def test_load_none_returns_empty_policy() -> None:
    policy = load_policy(None)
    assert policy.rules == {}


def test_load_empty_dict_returns_empty_policy() -> None:
    policy = load_policy({})
    assert policy.rules == {}


def test_load_rejects_non_mapping_top_level() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        load_policy([])


def test_load_rejects_non_mapping_sources() -> None:
    with pytest.raises(ValueError, match="sources must be a mapping"):
        load_policy({"sources": "not-a-dict"})


def test_load_rejects_unknown_source_kind() -> None:
    with pytest.raises(ValueError, match="unknown source kind"):
        load_policy({"sources": {"npm": {"allow": ["*"]}}})


def test_load_rejects_non_list_allow() -> None:
    with pytest.raises(ValueError, match="must be a list"):
        load_policy({"sources": {"pypi": {"allow": "x"}}})


def test_load_rejects_non_string_allow_entry() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        load_policy({"sources": {"pypi": {"allow": [42]}}})


def test_load_rejects_empty_string_allow_entry() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        load_policy({"sources": {"pypi": {"allow": [""]}}})


def test_load_full_policy_roundtrip() -> None:
    """End-to-end: every source kind populated with realistic rules."""
    policy = load_policy(
        {
            "sources": {
                "pypi": {"allow": ["opencomputer-*"], "deny": ["*-evil*"]},
                "github": {"allow": ["sakshamzip2-sys/*", "anthropics/*"]},
                "git": {"deny": ["*"]},
                "url": {"deny": ["*"]},
                "directory": {"allow": ["/Users/me/*"]},
            }
        }
    )
    assert policy.is_allowed(parse_source("pypi:opencomputer-foo"))
    assert not policy.is_allowed(parse_source("pypi:opencomputer-evil-x"))
    assert policy.is_allowed(parse_source("gh:sakshamzip2-sys/oc-skill"))
    assert policy.is_allowed(parse_source("gh:anthropics/oc-claude"))
    assert not policy.is_allowed(parse_source("gh:somebody/else"))
    assert not policy.is_allowed(parse_source("git+https://x.com/y.git"))
    assert not policy.is_allowed(parse_source("https://x.com/y.tar.gz"))
    assert policy.is_allowed(parse_source("/Users/me/dev/myplugin"))
    assert not policy.is_allowed(parse_source("/somewhere/else"))


# ─── glob semantics ──────────────────────────────────────────────


def test_glob_matches_with_question_mark() -> None:
    policy = load_policy({"sources": {"pypi": {"allow": ["plugin-?"]}}})
    assert policy.is_allowed(parse_source("pypi:plugin-1"))
    assert not policy.is_allowed(parse_source("pypi:plugin-12"))


def test_glob_exact_match() -> None:
    policy = load_policy({"sources": {"pypi": {"allow": ["exact-name"]}}})
    assert policy.is_allowed(parse_source("pypi:exact-name"))
    assert not policy.is_allowed(parse_source("pypi:exact-name-x"))
