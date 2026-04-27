from pathlib import Path
from opencomputer.awareness.personas.registry import get_persona, list_personas


def test_default_personas_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    personas = list_personas()
    ids = {p["id"] for p in personas}
    assert {"coding", "trading", "relaxed", "admin", "learning"}.issubset(ids)


def test_user_overrides_bundled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    user_dir = tmp_path / "personas"
    user_dir.mkdir()
    (user_dir / "coding.yaml").write_text(
        "id: coding\nname: My Coding\ndescription: my override\n"
        "system_prompt_overlay: 'custom'\npreferred_tone: warm\npreferred_response_format: prose\ndisabled_capabilities: []\n"
    )
    p = get_persona("coding")
    assert p["name"] == "My Coding"


def test_get_persona_unknown_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    assert get_persona("nonexistent") is None
