"""Tests for A4 partial-stream recovery (2026-05-06 OpenClaw deep-comparison)."""

from __future__ import annotations

from opencomputer.gateway.replay_sanitizer import (
    PartialRecoveryResult,
    recover_partial_assistant,
)


def test_clean_text_is_recoverable_unchanged():
    text = "Here is the answer: 42. Hope that helps!"
    r = recover_partial_assistant(text)
    assert r.status == "recoverable"
    assert r.text == text


def test_empty_text_is_unrecoverable():
    r = recover_partial_assistant("")
    assert r.status == "unrecoverable"
    assert "empty" in r.reason


def test_whitespace_only_is_unrecoverable():
    r = recover_partial_assistant("   \n\t  ")
    assert r.status == "unrecoverable"


def test_trims_dangling_thinking_block():
    text = "Hi there, the user wants <thinking>Let me think about"
    r = recover_partial_assistant(text)
    assert r.status == "recoverable"
    assert "thinking" not in r.text
    assert r.text == "Hi there, the user wants"


def test_trims_dangling_function_calls():
    text = "I'll call a tool now <function_calls><invoke name="
    r = recover_partial_assistant(text)
    assert r.status == "recoverable"
    assert "function_calls" not in r.text
    assert r.text == "I'll call a tool now"


def test_unrecoverable_when_only_tag_present():
    text = "<thinking>only thoughts here"
    r = recover_partial_assistant(text)
    assert r.status == "unrecoverable"
    assert "clean prose" in r.reason


def test_balanced_tags_are_clean():
    text = "<thinking>Done</thinking>Now answering: hello!"
    r = recover_partial_assistant(text)
    assert r.status == "recoverable"
    assert r.text == text


def test_minimax_invoke_fragment_unrecoverable():
    text = "Sure, I'll help. <|invoke|>some_tool"
    r = recover_partial_assistant(text)
    assert r.status == "unrecoverable"
    assert "MiniMax" in r.reason


def test_drop_threshold_respected():
    """Tiny clean prefix below the threshold → unrecoverable."""
    text = "Hi <thinking>partial"
    r = recover_partial_assistant(text, drop_threshold_chars=10)
    assert r.status == "unrecoverable"


def test_returns_partial_recovery_result_dataclass():
    r = recover_partial_assistant("ok")
    assert isinstance(r, PartialRecoveryResult)
    assert hasattr(r, "status")
    assert hasattr(r, "text")
    assert hasattr(r, "reason")
