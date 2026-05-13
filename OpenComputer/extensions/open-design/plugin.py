"""Open Design plugin — entry module.

Registers (in order):
    * ``oc design …`` Typer subcommand for start/stop/status/url/restart.
    * ``/design`` chat slash command.
    * Doctor contributions for ``oc doctor``.

The plugin is ``kind: "mixed"`` because it spans CLI + chat + doctor
without registering an agent-facing tool/provider/channel. The daemon
itself runs as a Node subprocess managed by ``lifecycle.py``.

Manifest ``single_instance: true`` guards against double-spawn from
parallel sessions — the loader holds an exclusive PID lock at
``~/.opencomputer/.locks/open-design.lock`` for the duration of
``register()``.

The plugin loads but is disabled by default (``enabled_by_default:
false``). The user opts in via ``oc plugins enable open-design``.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

_log = logging.getLogger("opencomputer.open_design.plugin")


def _load_local(module_name: str, file_name: str):
    """File-path import that side-steps sys.modules collisions.

    Sibling modules ``cli.py`` / ``slash.py`` / ``doctor.py`` are shared
    file names that other plugins may have already cached under those
    bare names. Synthesise a unique module identity to bypass that.
    """
    path = Path(__file__).resolve().parent / file_name
    synthetic = f"_open_design_{module_name}"
    if synthetic in sys.modules:
        return sys.modules[synthetic]
    spec = importlib.util.spec_from_file_location(synthetic, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {file_name} at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[synthetic] = mod
    spec.loader.exec_module(mod)
    return mod


def register(api) -> None:  # PluginAPI duck-typed
    """Wire CLI verbs + slash command + doctor rows."""
    # ── CLI: `oc design …` ────────────────────────────────────────────
    if hasattr(api, "register_cli_command"):
        try:
            cli_mod = _load_local("cli", "cli.py")
            api.register_cli_command("design", cli_mod.app)
        except Exception as exc:  # noqa: BLE001
            _log.warning("open-design: CLI registration failed: %s", exc)
    else:
        _log.debug("open-design: api.register_cli_command unavailable; skipping")

    # ── Slash: `/design …` ─────────────────────────────────────────────
    if hasattr(api, "register_slash_command"):
        try:
            slash_mod = _load_local("slash", "slash.py")
            api.register_slash_command(slash_mod.DesignCommand())
        except Exception as exc:  # noqa: BLE001
            _log.warning("open-design: /design slash registration failed: %s", exc)

    # ── Doctor rows ───────────────────────────────────────────────────
    if hasattr(api, "register_doctor_contribution"):
        try:
            doctor_mod = _load_local("doctor", "doctor.py")
            for contribution in doctor_mod.build_contributions():
                api.register_doctor_contribution(contribution)
        except Exception as exc:  # noqa: BLE001
            _log.warning("open-design: doctor contribution registration failed: %s", exc)

    _log.debug("open-design plugin registered")
