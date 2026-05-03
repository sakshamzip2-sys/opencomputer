"""Tests for credential_sources.py — env / config / keyring loaders."""

from __future__ import annotations

import pytest

from opencomputer.agent.credential_sources import (
    load_from_config,
    load_from_env,
    load_from_keyring,
    resolve_keys,
)


def test_load_from_env_picks_numbered_vars(monkeypatch):
    monkeypatch.setenv("TEST_KEY_1", "sk-aaa")
    monkeypatch.setenv("TEST_KEY_2", "sk-bbb")
    monkeypatch.setenv("TEST_KEY_3", "sk-ccc")
    assert load_from_env("TEST_KEY") == ["sk-aaa", "sk-bbb", "sk-ccc"]


def test_load_from_env_stops_at_gap(monkeypatch):
    monkeypatch.setenv("MYKEY_1", "val1")
    # _2 is missing
    monkeypatch.setenv("MYKEY_3", "val3")
    result = load_from_env("MYKEY")
    assert result == ["val1"]  # stops at gap


def test_load_from_env_returns_empty_if_none(monkeypatch):
    monkeypatch.delenv("EMPTY_1", raising=False)
    assert load_from_env("EMPTY") == []


def test_load_from_config_returns_keys():
    cfg = {"credential_pools": {"openai": ["key-a", "key-b"]}}
    assert load_from_config("openai", cfg) == ["key-a", "key-b"]


def test_load_from_config_missing_provider():
    cfg = {"credential_pools": {"anthropic": ["k1"]}}
    assert load_from_config("openai", cfg) == []


def test_load_from_config_no_pools_key():
    assert load_from_config("openai", {}) == []


def test_load_from_keyring_unavailable_returns_empty():
    result = load_from_keyring("nonexistent-service-xyz-opencomputer")
    assert result == []


def test_resolve_keys_deduplicates(monkeypatch):
    monkeypatch.setenv("DEDUP_1", "sk-same")
    monkeypatch.setenv("DEDUP_2", "sk-same")
    result = resolve_keys("any", env_prefix="DEDUP")
    assert result == ["sk-same"]  # deduped


def test_resolve_keys_combines_sources(monkeypatch):
    monkeypatch.setenv("COMBINED_1", "env-key")
    cfg = {"credential_pools": {"svc": ["cfg-key"]}}
    result = resolve_keys("svc", env_prefix="COMBINED", config=cfg)
    assert "env-key" in result
    assert "cfg-key" in result
