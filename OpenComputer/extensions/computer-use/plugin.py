"""computer-use plugin entry — registers the ``computer_use`` tool, the
``oc computer-use`` CLI verb, and a doctor row.

macOS-only feature. On non-macOS hosts the plugin still loads cleanly (so
``oc plugins`` lists it) but registers nothing tool-facing: the doctor row
reports a clean ``skip`` and the tool is not surfaced to the model.

Sibling-import discipline: the OC plugin loader puts the plugin root on
``sys.path[0]``, so flat imports (``from cu_tool import ComputerUseTool``)
resolve against THIS directory. Every internal module is prefixed ``cu_``
(``cu_backend.py`` / ``cu_schema.py`` / …) so no bare name can collide in
``sys.modules`` with another plugin shipping a generic ``backend.py`` /
``schema.py`` — the same unique-filename pattern ``browser-harness`` uses.
The entry itself additionally loads its siblings via ``importlib`` with
synthetic plugin-scoped module names for belt-and-braces isolation.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

_log = logging.getLogger("opencomputer.computer_use.plugin")

_PLUGIN_ROOT = Path(__file__).resolve().parent


def _load_local(mod_name: str, file_name: str) -> Any:
    """Import a sibling file under a synthetic plugin-scoped module name."""
    synthetic = f"_oc_computer_use_{mod_name}"
    if synthetic in sys.modules:
        return sys.modules[synthetic]
    path = _PLUGIN_ROOT / file_name
    spec = importlib.util.spec_from_file_location(synthetic, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {file_name} at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[synthetic] = mod
    spec.loader.exec_module(mod)
    return mod


def register(api: Any) -> None:  # PluginAPI duck-typed
    """Register the ComputerUseTool, the CLI verb, and the doctor row."""
    # Ensure the plugin root is importable so ``cu_tool.py``'s own
    # ``from cu_backend import ...`` siblings resolve (the loader normally
    # does this; belt-and-braces for the test conftest path).
    root = str(_PLUGIN_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

    # ── CLI: `oc computer-use …` ───────────────────────────────────
    if hasattr(api, "register_cli_command"):
        try:
            cli_mod = _load_local("cli", "cu_cli.py")
            api.register_cli_command("computer-use", cli_mod.app)
        except Exception as exc:  # noqa: BLE001
            _log.warning("computer-use: CLI registration failed: %s", exc)

    # ── Tool: `computer_use` (macOS only) ──────────────────────────
    if sys.platform == "darwin":
        try:
            tool_mod = _load_local("tool", "cu_tool.py")
            api.register_tool(tool_mod.ComputerUseTool())
        except Exception as exc:  # noqa: BLE001
            _log.warning("computer-use: tool registration failed: %s", exc)

        # ── System-prompt guidance (macOS only) ────────────────────
        # Splice the computer-use workflow + safety guidance into the
        # system prompt while the tool is registered. Mirrors hermes's
        # COMPUTER_USE_GUIDANCE block. Registered alongside the tool so
        # the guidance is only present when the tool actually is.
        if hasattr(api, "register_injection_provider"):
            try:
                injection_mod = _load_local("injection", "cu_injection.py")
                api.register_injection_provider(
                    injection_mod.ComputerUseGuidanceProvider()
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "computer-use: injection provider registration "
                    "failed: %s",
                    exc,
                )
    else:
        _log.debug(
            "computer-use: not registering computer_use tool — macOS only "
            "(host platform is %s)",
            sys.platform,
        )

    # ── Doctor row ─────────────────────────────────────────────────
    if hasattr(api, "register_doctor_contribution"):
        try:
            from plugin_sdk.doctor import HealthContribution

            doctor_mod = _load_local("doctor", "cu_doctor.py")
            api.register_doctor_contribution(
                HealthContribution(
                    id="computer-use",
                    description=(
                        "computer-use: cua-driver binary + macOS background "
                        "computer-use availability"
                    ),
                    run=doctor_mod.run,
                )
            )
        except AttributeError:
            # Older PluginAPI without the doctor surface — quietly skip.
            pass
        except Exception as exc:  # noqa: BLE001
            _log.warning("computer-use: doctor registration failed: %s", exc)

    _log.debug("computer-use plugin registered")


__all__ = ["register"]
