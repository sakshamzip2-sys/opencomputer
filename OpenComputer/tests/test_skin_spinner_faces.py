"""D5 + D6: Production-grade skin parity (faces + 22-key palette).

Each built-in skin defines two distinct face cycles AND the full
22-key Hermes color palette so renderers (Rich Theme, prompt-toolkit
Style, status bar, completion menu) can pull whichever key they need
without falling back to defaults.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from rich.console import Console

from opencomputer.cli_ui.skin import (
    apply_skin,
    current_spinner_thinking_faces,
    current_spinner_waiting_faces,
    list_builtin_names,
    load_skin,
)

_BUILTINS_DIR = (
    Path(__file__).resolve().parent.parent
    / "opencomputer"
    / "cli_ui"
    / "skin"
    / "builtins"
)

# Hermes v2 spec (D6) — these 22 keys cover banner, UI semantics, prompt,
# input rule, response box, session label/border, status bar, voice
# status, selection, and completion menu.
_HERMES_REQUIRED_COLOR_KEYS = frozenset({
    # banner
    "banner_border", "banner_title", "banner_accent", "banner_dim", "banner_text",
    # ui semantic
    "ui_accent", "ui_label", "ui_ok", "ui_error", "ui_warn",
    # prompt + input
    "prompt", "input_rule",
    # response box
    "response_border",
    # session
    "session_label", "session_border",
    # status bars
    "status_bar_bg", "voice_status_bg",
    # selection
    "selection_bg",
    # completion menu
    "completion_menu_bg", "completion_menu_current_bg",
    "completion_menu_meta_bg", "completion_menu_meta_current_bg",
})


# ─── D5: spinner faces ────────────────────────────────────────────


def test_spec_has_waiting_and_thinking_face_fields():
    spec = load_skin("default")
    assert hasattr(spec, "spinner_waiting_faces")
    assert hasattr(spec, "spinner_thinking_faces")
    assert isinstance(spec.spinner_waiting_faces, tuple)
    assert isinstance(spec.spinner_thinking_faces, tuple)


def test_default_skin_provides_face_cycles():
    spec = load_skin("default")
    assert len(spec.spinner_waiting_faces) >= 2
    assert len(spec.spinner_thinking_faces) >= 2
    for face in spec.spinner_waiting_faces + spec.spinner_thinking_faces:
        assert isinstance(face, str)
        assert face.strip()


def test_all_nine_builtin_skins_have_faces():
    """Hermes v2 production-grade: every built-in defines both cycles."""
    names = list_builtin_names()
    assert len(names) >= 9
    for name in names:
        spec = load_skin(name)
        assert spec.spinner_waiting_faces, f"skin {name!r} missing waiting_faces"
        assert spec.spinner_thinking_faces, f"skin {name!r} missing thinking_faces"


def test_apply_skin_publishes_faces_to_module_state():
    spec = load_skin("default")
    apply_skin(spec, Console())
    assert current_spinner_waiting_faces() == spec.spinner_waiting_faces
    assert current_spinner_thinking_faces() == spec.spinner_thinking_faces


def test_each_builtin_yaml_declares_both_face_cycles():
    """Source-level pin: future contributors don't accidentally drop."""
    names = list_builtin_names()
    for name in names:
        path = _BUILTINS_DIR / f"{name}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        spinner = data.get("spinner") or {}
        assert spinner.get("waiting_faces"), f"{name}.yaml: spinner.waiting_faces missing"
        assert spinner.get("thinking_faces"), f"{name}.yaml: spinner.thinking_faces missing"


# ─── D6: full 22-key palette (Hermes v2 covers 22 distinct keys) ──


def test_default_skin_has_all_hermes_color_keys():
    spec = load_skin("default")
    missing = _HERMES_REQUIRED_COLOR_KEYS - set(spec.colors.keys())
    assert not missing, f"default missing Hermes color keys: {sorted(missing)}"


def test_all_nine_builtin_skins_have_all_hermes_color_keys():
    """Every built-in must define all 22 Hermes color keys after merging
    with default. This is the production-grade parity gate."""
    names = list_builtin_names()
    for name in names:
        spec = load_skin(name)  # load_skin merges with default
        missing = _HERMES_REQUIRED_COLOR_KEYS - set(spec.colors.keys())
        assert not missing, f"skin {name!r} missing keys after merge: {sorted(missing)}"


def test_color_values_are_hex_strings():
    """Sanity: every color value should be a 7-char hex string ('#rrggbb')."""
    spec = load_skin("default")
    for key in _HERMES_REQUIRED_COLOR_KEYS:
        val = spec.colors.get(key)
        assert isinstance(val, str)
        assert val.startswith("#")
        assert len(val) == 7  # '#' + 6 hex digits


def test_each_builtin_yaml_literally_has_all_keys():
    """Source-level pin: not just merge-derived, but literally in the YAML
    so a contributor reading the file sees every key. The default.yaml
    is the canonical reference; other skins inherit-then-override."""
    default_path = _BUILTINS_DIR / "default.yaml"
    data = yaml.safe_load(default_path.read_text(encoding="utf-8"))
    colors = data.get("colors") or {}
    missing = _HERMES_REQUIRED_COLOR_KEYS - set(colors.keys())
    assert not missing, (
        f"default.yaml missing Hermes color keys (canonical reference): "
        f"{sorted(missing)}"
    )
