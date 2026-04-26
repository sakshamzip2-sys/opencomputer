"""LLM extractor — Ollama subprocess wrapper tests."""
from unittest.mock import patch

import pytest

from opencomputer.profile_bootstrap.llm_extractor import (
    ArtifactExtraction,
    OllamaUnavailable,
    extract_artifact,
    is_ollama_available,
)


def test_extraction_dataclass_defaults():
    e = ArtifactExtraction()
    assert e.topic == ""
    assert e.people == ()
    assert e.sentiment == "unknown"


def test_is_ollama_available_returns_false_without_binary():
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.shutil.which",
        return_value=None,
    ):
        assert is_ollama_available() is False


def test_extract_raises_when_unavailable():
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=False,
    ):
        with pytest.raises(OllamaUnavailable):
            extract_artifact("some content")


def test_extract_parses_ollama_json_output():
    fake_json = (
        '{"topic": "stocks", "people": ["Warren Buffett"], '
        '"intent": "research a stock", "sentiment": "neutral", '
        '"timestamp": "2026-04-26T10:00:00"}'
    )
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.llm_extractor.subprocess.run",
    ) as run:
        run.return_value.stdout = fake_json
        run.return_value.returncode = 0
        ex = extract_artifact("text about stocks")
    assert ex.topic == "stocks"
    assert "Warren Buffett" in ex.people
    assert ex.sentiment == "neutral"


def test_extract_returns_blank_on_malformed_json():
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.llm_extractor.subprocess.run",
    ) as run:
        run.return_value.stdout = "not json"
        run.return_value.returncode = 0
        ex = extract_artifact("anything")
    assert ex.topic == ""


def test_extract_returns_blank_on_timeout():
    import subprocess as _sp
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.llm_extractor.subprocess.run",
        side_effect=_sp.TimeoutExpired(cmd="ollama", timeout=15.0),
    ):
        ex = extract_artifact("anything")
    assert ex.topic == ""


def test_extract_truncates_long_content():
    huge = "a" * 50000
    captured: dict = {}
    def capture_run(cmd, **kwargs):
        captured["prompt"] = cmd[-1]
        class _R:
            returncode = 0
            stdout = "{}"
        return _R()
    with patch(
        "opencomputer.profile_bootstrap.llm_extractor.is_ollama_available",
        return_value=True,
    ), patch(
        "opencomputer.profile_bootstrap.llm_extractor.subprocess.run",
        side_effect=capture_run,
    ):
        extract_artifact(huge)
    # Prompt template + 4000-char truncated content < 50000
    assert len(captured["prompt"]) < 5000
