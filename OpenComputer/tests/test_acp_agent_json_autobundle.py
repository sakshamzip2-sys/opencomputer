"""Hermes parity G15: agent.json appears at canonical path on serve."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest


@pytest.fixture
def isolated_home(tmp_path):
    with patch("opencomputer.agent.config._home", return_value=tmp_path):
        yield tmp_path


def test_default_agent_json_path_is_canonical(isolated_home):
    from opencomputer.cli import _default_agent_json_path

    p = _default_agent_json_path()
    assert p.parent.name == "acp_registry"
    assert p.name == "agent.json"
    # path inside profile home (the patched tmp_path)
    assert isolated_home in p.parents


def test_ensure_agent_json_writes_when_missing(isolated_home):
    from opencomputer.cli import _ensure_agent_json

    p = _ensure_agent_json()
    assert p.exists()
    assert p.parent.name == "acp_registry"
    data = json.loads(p.read_text())
    # Manifest emits at least name + transport
    assert data.get("name") == "opencomputer"
    assert data.get("transport") == "stdio"


def test_ensure_agent_json_no_op_when_present(isolated_home):
    from opencomputer.cli import _ensure_agent_json

    p1 = _ensure_agent_json()
    p1.write_text('{"manual": "edited"}')
    p2 = _ensure_agent_json()
    # Should NOT overwrite a user-edited file
    assert p2.read_text() == '{"manual": "edited"}'


def test_ensure_agent_json_handles_existing_directory(isolated_home):
    """Pre-created acp_registry/ directory shouldn't trip mkdir."""
    from opencomputer.cli import _ensure_agent_json

    (isolated_home / "acp_registry").mkdir()
    p = _ensure_agent_json()
    assert p.exists()


def test_ensure_agent_json_returns_path_when_present(isolated_home):
    """Even when a user-edited file exists, return its Path."""
    from opencomputer.cli import _ensure_agent_json, _default_agent_json_path

    target = _default_agent_json_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}")
    p = _ensure_agent_json()
    assert p == target
