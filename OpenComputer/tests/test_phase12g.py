"""Phase 12g: SDK boundary hardening.

Two new mechanical surfaces tested + the discovery integration:

1. opencomputer/plugins/manifest_validator.py
   Typed pydantic schema for plugin.json. Catches malformed manifests
   before they reach the loader.

2. opencomputer/gateway/protocol_v2.py
   Per-method / per-event pydantic schemas extending v1. Lets wire
   clients validate both directions of any RPC.

3. opencomputer/plugins/discovery.py
   Now calls validate_manifest inside _parse_manifest — bad manifests
   are rejected at scan time instead of crashing the loader.

The 3 new boundary CLAUDE.md files (plugin_sdk/, opencomputer/plugins/,
opencomputer/gateway/) are reviewed-by-humans, not asserted by tests.
The existing SDK-boundary linter test (test_phase6a) still enforces the
hard rule.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ─── manifest_validator: happy path ────────────────────────────────────


def test_validator_accepts_minimal_valid_manifest() -> None:
    from opencomputer.plugins.manifest_validator import validate_manifest

    data = {"id": "my-plugin", "name": "My Plugin", "version": "1.0.0", "entry": "plugin"}
    schema, err = validate_manifest(data)
    assert err == ""
    assert schema is not None
    assert schema.id == "my-plugin"
    # Defaults applied
    assert schema.kind == "mixed"
    assert schema.license == "MIT"
    assert schema.description == ""


def test_validator_accepts_full_manifest_with_all_optional_fields() -> None:
    from opencomputer.plugins.manifest_validator import validate_manifest

    data = {
        "id": "weather",
        "name": "Weather Plugin",
        "version": "0.2.1-beta",
        "entry": "plugin",
        "description": "Fetches the weather.",
        "author": "Saksham",
        "homepage": "https://example.com",
        "license": "Apache-2.0",
        "kind": "tool",
    }
    schema, err = validate_manifest(data)
    assert err == ""
    assert schema is not None
    assert schema.kind == "tool"
    assert schema.version == "0.2.1-beta"


@pytest.mark.parametrize("version", ["1", "1.2", "1.2.3", "10.20.30", "2.0.0-beta", "1.0.0-rc.1"])
def test_validator_accepts_lenient_version_formats(version: str) -> None:
    from opencomputer.plugins.manifest_validator import validate_manifest

    schema, err = validate_manifest({"id": "x", "name": "X", "version": version, "entry": "plugin"})
    assert err == "", f"rejected valid version {version!r}: {err}"
    assert schema is not None


# ─── manifest_validator: rejection paths ───────────────────────────────


def test_validator_rejects_missing_required_fields() -> None:
    from opencomputer.plugins.manifest_validator import validate_manifest

    schema, err = validate_manifest({"id": "x"})
    assert schema is None
    assert "name" in err and "version" in err and "entry" in err


def test_validator_rejects_wrong_type() -> None:
    from opencomputer.plugins.manifest_validator import validate_manifest

    schema, err = validate_manifest({"id": "x", "name": 123, "version": "1.0", "entry": "plugin"})
    assert schema is None
    assert "name" in err


def test_validator_rejects_unknown_kind() -> None:
    from opencomputer.plugins.manifest_validator import validate_manifest

    schema, err = validate_manifest(
        {"id": "x", "name": "X", "version": "1.0", "entry": "plugin", "kind": "channelizer"}
    )
    assert schema is None
    assert "kind" in err


@pytest.mark.parametrize("bad_id", ["UPPER", "with space", "-leads", "trails-", "has.dots", ""])
def test_validator_rejects_malformed_ids(bad_id: str) -> None:
    from opencomputer.plugins.manifest_validator import validate_manifest

    schema, err = validate_manifest(
        {"id": bad_id, "name": "X", "version": "1.0", "entry": "plugin"}
    )
    assert schema is None
    assert "id" in err


def test_validator_rejects_empty_entry() -> None:
    from opencomputer.plugins.manifest_validator import validate_manifest

    schema, err = validate_manifest({"id": "x", "name": "X", "version": "1.0", "entry": ""})
    assert schema is None
    assert "entry" in err


def test_validator_rejects_entry_as_path() -> None:
    """Common copy-paste bug: putting `src/plugin.py` instead of `plugin`."""
    from opencomputer.plugins.manifest_validator import validate_manifest

    schema, err = validate_manifest(
        {"id": "x", "name": "X", "version": "1.0", "entry": "src/plugin.py"}
    )
    assert schema is None
    assert "entry" in err


def test_validator_rejects_extra_unknown_fields() -> None:
    """extra=forbid catches typos like `discription` instead of `description`."""
    from opencomputer.plugins.manifest_validator import validate_manifest

    schema, err = validate_manifest(
        {
            "id": "x",
            "name": "X",
            "version": "1.0",
            "entry": "plugin",
            "discription": "typo",
        }
    )
    assert schema is None
    assert "discription" in err


# ─── discovery integration ─────────────────────────────────────────────


def test_discovery_skips_invalid_manifest_silently(tmp_path: Path) -> None:
    """A bad manifest should NOT prevent good ones from loading. Discovery
    logs the error and moves on."""
    from opencomputer.plugins.discovery import discover

    # bad plugin: invalid kind
    bad = tmp_path / "bad-plugin"
    bad.mkdir()
    (bad / "plugin.json").write_text(
        json.dumps(
            {"id": "bad", "name": "Bad", "version": "1.0", "entry": "plugin", "kind": "weird"}
        )
    )
    # good plugin alongside
    good = tmp_path / "good-plugin"
    good.mkdir()
    (good / "plugin.json").write_text(
        json.dumps({"id": "good", "name": "Good", "version": "1.0", "entry": "plugin"})
    )
    candidates = discover([tmp_path])
    ids = {c.manifest.id for c in candidates}
    assert "good" in ids
    assert "bad" not in ids


def test_discovery_propagates_validated_manifest_fields(tmp_path: Path) -> None:
    """Confirm the existing path through PluginManifest still works post-validator."""
    from opencomputer.plugins.discovery import discover

    p = tmp_path / "x"
    p.mkdir()
    (p / "plugin.json").write_text(
        json.dumps(
            {
                "id": "x",
                "name": "X",
                "version": "1.0",
                "entry": "plugin",
                "kind": "tool",
                "description": "d",
                "license": "MIT",
            }
        )
    )
    candidates = discover([tmp_path])
    assert len(candidates) == 1
    m = candidates[0].manifest
    assert m.id == "x" and m.kind == "tool" and m.description == "d"


# ─── protocol_v2: per-method round-trips ───────────────────────────────


def test_protocol_v2_chat_params_round_trip() -> None:
    from opencomputer.gateway.protocol_v2 import ChatParams

    p = ChatParams(message="hello", session_id="s-1", plan_mode=True)
    j = p.model_dump()
    assert j == {"message": "hello", "session_id": "s-1", "plan_mode": True}
    p2 = ChatParams.model_validate(j)
    assert p2 == p


def test_protocol_v2_chat_params_rejects_extra_field() -> None:
    """Strict mode: silent typos fail loudly."""
    from pydantic import ValidationError

    from opencomputer.gateway.protocol_v2 import ChatParams

    with pytest.raises(ValidationError):
        ChatParams.model_validate({"message": "hi", "sesion_id": "typo"})


def test_protocol_v2_chat_result_required_fields() -> None:
    from pydantic import ValidationError

    from opencomputer.gateway.protocol_v2 import ChatResult

    # Missing iterations + tokens
    with pytest.raises(ValidationError):
        ChatResult.model_validate({"final_message": "ok", "session_id": "s-1"})


def test_protocol_v2_method_schemas_cover_every_v1_method() -> None:
    """Catch drift: if a new method is added to protocol.py, v2 must add a schema."""
    from opencomputer.gateway import protocol as v1
    from opencomputer.gateway.protocol_v2 import METHOD_SCHEMAS

    v1_methods = {
        getattr(v1, name)
        for name in dir(v1)
        if name.startswith("METHOD_") and isinstance(getattr(v1, name), str)
    }
    assert v1_methods <= set(METHOD_SCHEMAS.keys()), (
        f"protocol_v2 missing schemas for: {v1_methods - set(METHOD_SCHEMAS.keys())}"
    )


def test_protocol_v2_event_schemas_cover_every_v1_event() -> None:
    from opencomputer.gateway import protocol as v1
    from opencomputer.gateway.protocol_v2 import EVENT_SCHEMAS

    v1_events = {
        getattr(v1, name)
        for name in dir(v1)
        if name.startswith("EVENT_") and isinstance(getattr(v1, name), str)
    }
    assert v1_events <= set(EVENT_SCHEMAS.keys()), (
        f"protocol_v2 missing schemas for: {v1_events - set(EVENT_SCHEMAS.keys())}"
    )


# ─── protocol_v2: per-event round-trips ────────────────────────────────


def test_protocol_v2_tool_call_payload_round_trip() -> None:
    from opencomputer.gateway.protocol_v2 import ToolCallPayload

    p = ToolCallPayload(tool_call_id="t1", name="Read", arguments={"path": "/tmp"})
    p2 = ToolCallPayload.model_validate(p.model_dump())
    assert p == p2


def test_protocol_v2_assistant_message_payload_kind_enum() -> None:
    from pydantic import ValidationError

    from opencomputer.gateway.protocol_v2 import AssistantMessagePayload

    AssistantMessagePayload(text="x", kind="delta")
    AssistantMessagePayload(text="x", kind="final")
    with pytest.raises(ValidationError):
        AssistantMessagePayload(text="x", kind="streaming")


# ─── boundary doc presence (smoke) ─────────────────────────────────────


def test_three_boundary_claude_md_files_exist() -> None:
    """The 3 boundary contracts must all ship together — they reference each other."""
    repo = Path(__file__).resolve().parent.parent
    assert (repo / "plugin_sdk" / "CLAUDE.md").exists()
    assert (repo / "opencomputer" / "plugins" / "CLAUDE.md").exists()
    assert (repo / "opencomputer" / "gateway" / "CLAUDE.md").exists()
