"""Generate a `.env.template` for the active profile from plugin manifests.

Phase 14.G (2026-05-04). Plugin manifests already declare their required
env vars via ``PluginManifest.setup.providers[].env_vars`` and
``setup.channels[].env_vars``. This module turns those declarations into
a human-readable `.env.template` users can fill in. OC's existing per-
profile `.env` loader (see :func:`opencomputer.security.env_loader.load_for_profile`)
picks up `.env` at startup, so the round-trip is:

    oc profile env-template --write    # writes <profile>/.env.template
    cp <profile>/.env.template <profile>/.env
    # edit <profile>/.env, fill in real values
    oc                                 # next start auto-loads it

Pure function — no I/O dependencies. Easy to unit test.
"""

from __future__ import annotations

import os
import time
from typing import Any


def _truncate_secret(value: str, prefix_len: int = 5) -> str:
    """Render a non-leaking hint for a currently-set secret value.

    Returns ``"sk-an...(98 chars)"`` style — enough to confirm "yes I
    have something set" without echoing the secret. Empty values
    return empty string.
    """
    if not value:
        return ""
    if len(value) <= prefix_len:
        return f"({len(value)} chars)"
    return f"{value[:prefix_len]}...({len(value)} chars)"


def _render_env_var_block(
    var_name: str,
    *,
    current_env: dict[str, str] | None = None,
) -> str:
    """One env-var line with a non-leaking hint comment if currently set."""
    env = current_env if current_env is not None else os.environ
    val = env.get(var_name, "")
    if val:
        return f"{var_name}=  # currently: {_truncate_secret(val)}"
    return f"{var_name}="


def _render_plugin_section(
    *,
    plugin_id: str,
    plugin_description: str,
    setup_label: str,
    env_vars: tuple[str, ...],
    signup_url: str,
    disabled: bool = False,
    current_env: dict[str, str] | None = None,
) -> str:
    """Render one plugin/setup-target section. Empty if no env_vars."""
    if not env_vars:
        return ""
    label = setup_label or plugin_id
    disabled_tag = " [DISABLED]" if disabled else ""
    lines = [f"# === {label}{disabled_tag} ==="]
    if plugin_description:
        lines.append(f"# {plugin_id} — {plugin_description}")
    else:
        lines.append(f"# {plugin_id}")
    if signup_url:
        lines.append(f"# docs: {signup_url}")
    for var in env_vars:
        lines.append(_render_env_var_block(var, current_env=current_env))
    return "\n".join(lines)


def render_env_template(
    plugins: list[Any],
    *,
    profile_name: str = "default",
    enabled_ids: set[str] | None = None,
    include_disabled: bool = False,
    current_env: dict[str, str] | None = None,
    now_iso: str | None = None,
) -> str:
    """Render a `.env.template` from plugin candidate manifests.

    Args:
        plugins: list of objects with a ``manifest`` attribute (typically
            :class:`PluginCandidate`). Each manifest exposes
            ``manifest.setup.providers`` and ``manifest.setup.channels``.
        profile_name: shown in the header banner.
        enabled_ids: when set, plugins not in this set are skipped (or
            included with ``# [DISABLED]`` tag if ``include_disabled``).
            ``None`` = include everything.
        include_disabled: see ``enabled_ids``.
        current_env: env-mapping override (defaults to ``os.environ``).
            Used by tests to control "currently set" hints.
        now_iso: timestamp string for the header (defaults to now UTC).
    """
    if now_iso is None:
        now_iso = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    sections: list[str] = []
    for cand in plugins:
        manifest = getattr(cand, "manifest", None)
        if manifest is None:
            continue
        plugin_id = getattr(manifest, "id", "")
        if not plugin_id:
            continue

        is_enabled = enabled_ids is None or plugin_id in enabled_ids
        if not is_enabled and not include_disabled:
            continue
        disabled_tag = (not is_enabled) and include_disabled

        plugin_desc = getattr(manifest, "description", "")
        setup = getattr(manifest, "setup", None)
        if setup is None:
            continue

        for provider in getattr(setup, "providers", ()):
            section = _render_plugin_section(
                plugin_id=plugin_id,
                plugin_description=plugin_desc,
                setup_label=getattr(provider, "label", ""),
                env_vars=tuple(getattr(provider, "env_vars", ())),
                signup_url=getattr(provider, "signup_url", ""),
                disabled=disabled_tag,
                current_env=current_env,
            )
            if section:
                sections.append(section)

        for channel in getattr(setup, "channels", ()):
            section = _render_plugin_section(
                plugin_id=plugin_id,
                plugin_description=plugin_desc,
                setup_label=getattr(channel, "label", ""),
                env_vars=tuple(getattr(channel, "env_vars", ())),
                signup_url=getattr(channel, "signup_url", ""),
                disabled=disabled_tag,
                current_env=current_env,
            )
            if section:
                sections.append(section)

    header = (
        "# " + "=" * 64 + "\n"
        f"# OpenComputer profile: {profile_name}\n"
        f"# Generated: {now_iso}\n"
        "# Fill in values, rename to .env, OC will load on startup.\n"
        "# " + "=" * 64
    )
    if not sections:
        return header + "\n\n# (no plugin env vars to declare)\n"
    return header + "\n\n" + "\n\n".join(sections) + "\n"


__all__ = [
    "render_env_template",
]
