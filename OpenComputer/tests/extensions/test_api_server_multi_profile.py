"""Hermes parity (2026-05-08): api-server multi-profile model-name advertise."""

import importlib.util
from pathlib import Path

# Load openai_format via importlib because extensions/api-server/ has a hyphen
# and isn't importable as a normal Python package. Mirrors the load pattern
# used by adapter.py.
_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "extensions"
    / "api-server"
    / "openai_format.py"
)
_SPEC = importlib.util.spec_from_file_location("api_server_openai_format_test", _PATH)
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

list_models = _MOD.list_models
oc_response_to_responses_api = _MOD.oc_response_to_responses_api


def test_default_model_id_is_default():
    result = list_models("default", env_override=None)
    assert result["data"][0]["id"] == "default"


def test_named_profile_advertised():
    result = list_models("alice", env_override=None)
    assert result["data"][0]["id"] == "alice"


def test_env_override_wins_over_profile():
    result = list_models("alice", env_override="custom-name")
    assert result["data"][0]["id"] == "custom-name"


def test_owned_by_is_opencomputer():
    result = list_models("default", env_override=None)
    assert result["data"][0]["owned_by"] == "opencomputer"


def test_model_object_field():
    result = list_models("default", env_override=None)
    assert result["data"][0]["object"] == "model"


def test_envelope_object_is_list():
    result = list_models("default", env_override=None)
    assert result["object"] == "list"


def test_empty_profile_falls_back():
    result = list_models("", env_override=None)
    assert result["data"][0]["id"] == "opencomputer"


def test_whitespace_env_override_falls_back_to_profile():
    result = list_models("alice", env_override="   ")
    assert result["data"][0]["id"] == "alice"


def test_responses_api_envelope_shape():
    """Hermes-parity stub: responses-api envelope wraps text correctly."""
    body = oc_response_to_responses_api(
        "hello world", model="alice", input_tokens=2, output_tokens=2
    )
    assert body["object"] == "response"
    assert body["model"] == "alice"
    assert body["output"][0]["type"] == "message"
    assert body["output"][0]["content"][0]["type"] == "output_text"
    assert body["output"][0]["content"][0]["text"] == "hello world"
    assert body["usage"]["total_tokens"] == 4


def test_responses_api_unique_id_per_call():
    a = oc_response_to_responses_api("x")
    b = oc_response_to_responses_api("y")
    assert a["id"] != b["id"]
    assert a["id"].startswith("resp-")
