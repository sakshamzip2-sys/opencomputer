"""A6 / A9 — per-chat ``cwd`` and ``queue_mode`` binding overrides.

Covers the ``bindings.yaml`` schema additions and
``BindingResolver.resolve_binding`` (the full-binding accessor that the
gateway dispatcher uses to read a chat's working directory + queue
mode).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.bindings_config import load_bindings
from opencomputer.gateway.binding_resolver import BindingResolver
from plugin_sdk.core import MessageEvent, Platform


def _event(chat_id: str = "12345") -> MessageEvent:
    return MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        user_id="u1",
        text="hi",
        timestamp=0.0,
    )


def test_binding_parses_cwd_and_queue_mode(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: default\n"
        "bindings:\n"
        "  - match: { platform: telegram, chat_id: '12345' }\n"
        "    profile: coding\n"
        "    cwd: /Users/saksham/Vscode/claude/OpenComputer\n"
        "    queue_mode: collect\n"
    )
    cfg = load_bindings(cfg_path)
    b = cfg.bindings[0]
    assert b.cwd == "/Users/saksham/Vscode/claude/OpenComputer"
    assert b.queue_mode == "collect"


def test_binding_cwd_queue_mode_default_none(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: default\n"
        "bindings:\n"
        "  - match: { platform: telegram }\n"
        "    profile: personal\n"
    )
    b = load_bindings(cfg_path).bindings[0]
    assert b.cwd is None
    assert b.queue_mode is None


def test_invalid_queue_mode_rejected(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: default\n"
        "bindings:\n"
        "  - match: { platform: telegram }\n"
        "    profile: p\n"
        "    queue_mode: turbo\n"
    )
    with pytest.raises(ValueError, match="queue_mode"):
        load_bindings(cfg_path)


def test_empty_cwd_rejected(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: default\n"
        "bindings:\n"
        "  - match: { platform: telegram }\n"
        "    profile: p\n"
        "    cwd: ''\n"
    )
    with pytest.raises(ValueError, match="cwd"):
        load_bindings(cfg_path)


def test_resolve_binding_returns_winning_binding(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: default\n"
        "bindings:\n"
        "  - match: { platform: telegram }\n"
        "    profile: personal\n"
        "  - match: { platform: telegram, chat_id: '12345' }\n"
        "    profile: coding\n"
        "    cwd: /tmp/project\n"
        "    queue_mode: interrupt\n"
    )
    resolver = BindingResolver(load_bindings(cfg_path))
    # The chat-id-specific binding (specificity 5) beats platform-only.
    winner = resolver.resolve_binding(_event("12345"))
    assert winner is not None
    assert winner.profile == "coding"
    assert winner.cwd == "/tmp/project"
    assert winner.queue_mode == "interrupt"


def test_resolve_binding_miss_returns_none(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text("default_profile: default\nbindings: []\n")
    resolver = BindingResolver(load_bindings(cfg_path))
    assert resolver.resolve_binding(_event()) is None


def test_resolve_still_returns_profile_string(tmp_path: Path) -> None:
    """``resolve`` keeps its profile-string contract for routing callers."""
    cfg_path = tmp_path / "bindings.yaml"
    cfg_path.write_text(
        "default_profile: fallback\n"
        "bindings:\n"
        "  - match: { platform: telegram }\n"
        "    profile: tg\n"
    )
    resolver = BindingResolver(load_bindings(cfg_path))
    assert resolver.resolve(_event()) == "tg"
    assert (
        resolver.resolve(
            MessageEvent(
                platform=Platform.DISCORD,
                chat_id="x",
                user_id="u",
                text="t",
                timestamp=0.0,
            )
        )
        == "fallback"
    )
