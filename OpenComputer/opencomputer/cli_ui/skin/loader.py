"""Load a SkinSpec from built-in YAML or user override."""
from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

import yaml

from .spec import SkinSpec

logger = logging.getLogger("opencomputer.cli_ui.skin")

DEFAULT_NAME = "default"
USER_SKINS_DIR = Path("~/.opencomputer/skins").expanduser()


def _resource_yaml(name: str) -> str | None:
    """Read a built-in YAML by skin name; None if missing."""
    try:
        files = resources.files("opencomputer.cli_ui.skin.builtins")
        target = files.joinpath(f"{name}.yaml")
        if target.is_file():
            return target.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        return None
    return None


def list_builtin_names() -> list[str]:
    """Return all built-in skin names (sorted)."""
    try:
        files = resources.files("opencomputer.cli_ui.skin.builtins")
    except (FileNotFoundError, ModuleNotFoundError):
        return [DEFAULT_NAME]
    names: list[str] = []
    for entry in files.iterdir():
        if entry.name.endswith(".yaml"):
            names.append(entry.name[:-5])
    return sorted(names)


def _user_yaml(name: str) -> str | None:
    p = USER_SKINS_DIR / f"{name}.yaml"
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("skin: failed to read %s — %s", p, exc)
    return None


def _parse_yaml(text: str, *, source: str) -> dict:
    try:
        loaded = yaml.safe_load(text) or {}
        if isinstance(loaded, dict):
            return loaded
        logger.warning("skin: %s did not parse as a dict — ignoring", source)
    except yaml.YAMLError as exc:
        logger.warning("skin: malformed YAML in %s — %s", source, exc)
    return {}


def _merge_with_default(default: dict, override: dict) -> dict:
    """One-level dict merge: override fills/overrides default keys.

    For nested dicts (``colors``, ``spinner``, ``branding``, ``tool_emojis``),
    perform a per-key merge so a custom skin can override a single color
    without redeclaring the whole palette.
    """
    out = dict(default)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def _spec_from_dict(name: str, data: dict) -> SkinSpec:
    spinner = data.get("spinner") or {}
    branding = data.get("branding") or {}

    wings_raw = spinner.get("wings") or [["⟨", "⟩"]]
    wings = tuple(
        (w[0], w[1])
        for w in wings_raw
        if isinstance(w, list | tuple) and len(w) == 2
    )
    if not wings:
        wings = (("⟨", "⟩"),)

    # Hermes v2 D5 (2026-05-08): waiting/thinking face cycles.
    # ``thinking_faces`` falls back to ``waiting_faces`` when only one
    # is configured (common single-face skins) so renderers can always
    # call ``current_spinner_thinking_faces`` without checking which
    # set was populated.
    waiting_faces = tuple(
        str(f) for f in (spinner.get("waiting_faces") or ()) if isinstance(f, str)
    )
    thinking_faces = tuple(
        str(f) for f in (spinner.get("thinking_faces") or ()) if isinstance(f, str)
    )

    return SkinSpec(
        name=name,
        description=str(data.get("description", "")),
        colors=dict(data.get("colors") or {}),
        spinner_thinking_verbs=tuple(
            spinner.get("thinking_verbs") or ("thinking",)
        ),
        spinner_wings=wings,
        spinner_waiting_faces=waiting_faces,
        spinner_thinking_faces=thinking_faces,
        agent_name=str(branding.get("agent_name", "OpenComputer")),
        response_label=str(branding.get("response_label", " ✦ OC ")),
        prompt_symbol=str(branding.get("prompt_symbol", ">")),
        banner_logo=str(data.get("banner_logo", "")),
        banner_hero=str(data.get("banner_hero", "")),
        tool_prefix=str(data.get("tool_prefix", "┊")),
        tool_emojis=dict(data.get("tool_emojis") or {}),
    )


def load_skin(name: str) -> SkinSpec:
    """Load a SkinSpec by name. Never raises.

    Resolution order:
      1. ``~/.opencomputer/skins/<name>.yaml`` (USER_SKINS_DIR)
      2. ``opencomputer/cli_ui/skin/builtins/<name>.yaml``
      3. ``default`` (always available)

    If the named YAML is malformed, falls back to ``default``. If neither
    user nor built-in YAML exists, returns the ``default`` spec.
    """
    name = (name or "").strip().lower() or DEFAULT_NAME

    default_text = _resource_yaml(DEFAULT_NAME) or "{}"
    default_data = _parse_yaml(default_text, source=f"builtins/{DEFAULT_NAME}.yaml")

    if name == DEFAULT_NAME:
        return _spec_from_dict(DEFAULT_NAME, default_data)

    user_text = _user_yaml(name)
    builtin_text = _resource_yaml(name)
    chosen_text = user_text or builtin_text

    if chosen_text is None:
        logger.warning("skin: %r not found — falling back to default", name)
        return _spec_from_dict(DEFAULT_NAME, default_data)

    override = _parse_yaml(
        chosen_text,
        source=("user" if user_text else "builtin") + f":{name}.yaml",
    )
    if not override:
        # malformed YAML — fall back to default body but keep the name
        return _spec_from_dict(name, default_data)

    merged = _merge_with_default(default_data, override)
    return _spec_from_dict(name, merged)


__all__ = ["DEFAULT_NAME", "USER_SKINS_DIR", "list_builtin_names", "load_skin"]
