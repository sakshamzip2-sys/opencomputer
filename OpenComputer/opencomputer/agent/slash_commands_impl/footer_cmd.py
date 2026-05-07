"""``/footer [on|off|status]`` — toggle the runtime metadata footer.

SlashCommand-class twin of the existing CLI handler in
:mod:`opencomputer.cli_ui.slash_handlers` (``_handle_footer``). The CLI
handler already covers the REPL path, but the gateway / wire / ACP
surfaces dispatch through the SlashCommand registry — this class makes
the same toggle reachable there.

Persists to ``<profile_home>/config.yaml`` under
``display.runtime_footer.enabled``. ``status`` (or empty args) just
shows the current value without touching the file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


class FooterCommand(SlashCommand):
    name = "footer"
    description = "Toggle the runtime metadata footer (on/off/status)"

    async def execute(
        self, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult:
        sub = (args or "").strip().lower() or "status"
        cfg_path = _config_path(runtime)
        cfg = _read_cfg(cfg_path)

        if sub == "status":
            enabled = (
                bool(
                    (
                        (cfg.get("display") or {}).get("runtime_footer") or {}
                    ).get("enabled", False)
                )
            )
            label = "on" if enabled else "off (disabled)"
            return SlashCommandResult(
                output=(
                    f"runtime footer: {label}\n"
                    f"  config: {cfg_path}\n"
                    f"  /footer on|off to toggle and persist"
                ),
                handled=True,
            )

        if sub in ("on", "off"):
            target = sub == "on"
            display = cfg.setdefault("display", {})
            rf = display.setdefault("runtime_footer", {})
            rf["enabled"] = target
            try:
                cfg_path.parent.mkdir(parents=True, exist_ok=True)
                cfg_path.write_text(
                    yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False),
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001 — surface error to user
                return SlashCommandResult(
                    output=f"/footer write failed: {exc}", handled=True,
                )
            # Reflect immediately so the next turn renders accordingly.
            try:
                runtime.custom["show_footer"] = target
            except Exception:  # noqa: BLE001 — runtime hint is best-effort
                pass
            return SlashCommandResult(
                output=f"runtime footer: {'on' if target else 'off'} (saved to {cfg_path})",
                handled=True,
            )

        return SlashCommandResult(
            output="usage: /footer [on|off|status]", handled=True,
        )


def _config_path(runtime: RuntimeContext) -> Path:
    """Resolve ``<profile_home>/config.yaml`` from runtime + env fallbacks.

    Same precedence as :mod:`sethome_cmd`: ``runtime.custom['profile_home']``
    wins; otherwise ``OPENCOMPUTER_HOME`` + ``OPENCOMPUTER_PROFILE``;
    otherwise ``~/.opencomputer/<profile>/config.yaml``.
    """
    custom = runtime.custom or {}
    home = custom.get("profile_home")
    if home is not None:
        return Path(home) / "config.yaml"

    env_home = os.environ.get("OPENCOMPUTER_HOME")
    profile = os.environ.get("OPENCOMPUTER_PROFILE", "default")
    if env_home:
        return Path(env_home) / profile / "config.yaml"
    return Path.home() / ".opencomputer" / profile / "config.yaml"


def _read_cfg(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001 — corrupt → start fresh
        return {}
    return {}


__all__ = ["FooterCommand"]
