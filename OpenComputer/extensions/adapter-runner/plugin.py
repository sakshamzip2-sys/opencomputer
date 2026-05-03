"""adapter-runner plugin entry — discovers adapters at boot + registers
each as a synthetic ``BaseTool``.

Tool naming: each adapter ``(site, name)`` becomes ``<Site><Name>`` in
PascalCase — e.g. ``hackernews/top`` → ``HackernewsTop``,
``atria/assignments`` → ``AtriaAssignments``.

Discovery sources (priority order — later wins on dup):
  1. Bundled curated pack: ``extensions/browser-control/adapters/**``
  2. Installed plugin packs: ``extensions/<plugin>/adapters/**``
  3. User-authored: ``~/.opencomputer/<profile>/adapters/**``

Doctor row reports the count of registered adapters + any import
errors encountered during discovery.
"""

from __future__ import annotations

import logging
import os
import sys
import types
from pathlib import Path
from typing import Any

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.doctor import HealthContribution, RepairResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_log = logging.getLogger("opencomputer.adapter_runner.plugin")

#: Hyphenated-on-disk → underscore alias (mirrors ``browser-control``'s
#: ``_bootstrap_package_namespace``). The decorator + ctx modules use
#: relative imports (``from ._decorator import ...``), so the package
#: must be visible under ``extensions.adapter_runner`` for them to
#: resolve.
_PKG = "extensions.adapter_runner"
_PARENT_PKG = "extensions"


def _bootstrap_package_namespace() -> None:
    """Make ``extensions.adapter_runner`` resolvable in sys.modules."""
    plugin_root = Path(__file__).resolve().parent

    if _PARENT_PKG not in sys.modules:
        parent = types.ModuleType(_PARENT_PKG)
        parent.__path__ = [str(plugin_root.parent)]
        parent.__package__ = _PARENT_PKG
        sys.modules[_PARENT_PKG] = parent

    if _PKG not in sys.modules:
        pkg = types.ModuleType(_PKG)
        pkg.__path__ = [str(plugin_root)]
        pkg.__package__ = _PKG
        sys.modules[_PKG] = pkg
        sys.modules[_PARENT_PKG].adapter_runner = pkg


def register(api: Any) -> None:
    """Plugin entrypoint: discover + register synthetic tools per adapter."""
    _bootstrap_package_namespace()

    from extensions.adapter_runner._discovery import (
        discover_adapters,  # type: ignore[import-not-found]
    )

    profile_home = _resolve_profile_home()
    extensions_root = Path(__file__).resolve().parent.parent
    result = discover_adapters(
        profile_home=profile_home,
        extensions_root=extensions_root,
    )

    # Stash on module for the doctor row
    global _LAST_DISCOVERY
    _LAST_DISCOVERY = result

    _register_specs_with_api(api, result.specs)

    # Doctor contribution
    try:
        api.register_doctor_contribution(
            HealthContribution(
                id="adapter-runner",
                description=(
                    "adapter-runner: count of discovered @adapter recipes + "
                    "any import errors"
                ),
                run=_doctor_run,
            )
        )
    except AttributeError:  # older PluginAPI without doctor surface
        pass
    except Exception as exc:  # noqa: BLE001
        _log.warning("Failed to register doctor contribution: %s", exc)


# Module-level cache for the doctor row.
_LAST_DISCOVERY: Any = None


def _register_specs_with_api(api: Any, specs: list[Any]) -> None:
    """Wrap each spec as an ``AdapterTool`` and register with the host."""
    for spec in specs:
        try:
            api.register_tool(_AdapterTool(spec))
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "Failed to register adapter tool %s: %s", spec.tool_name, exc
            )


def _resolve_profile_home() -> Path | None:
    """Best-effort: find the active profile home directory."""
    # Explicit env override (set by core when a profile is active).
    home_str = os.environ.get("OPENCOMPUTER_HOME")
    if home_str:
        return Path(home_str)
    # Fallback: the default location.
    return Path.home() / ".opencomputer" / "default"


# ─── synthetic tool wrapper ────────────────────────────────────────


class _AdapterTool(BaseTool):
    """One synthetic ``BaseTool`` per registered adapter."""

    parallel_safe = False

    def __init__(self, spec: Any) -> None:
        self._spec = spec
        # Capability claim — adapters that need browser/network are
        # gated behind the same browser.* tier as the Browser tool.
        self.capability_claims = (
            CapabilityClaim(
                capability_id=(
                    "adapter.browser" if spec.browser else "adapter.network"
                ),
                tier_required=(
                    ConsentTier.EXPLICIT if spec.browser else ConsentTier.IMPLICIT
                ),
                human_description=(
                    f"Run the {spec.tool_name} adapter against {spec.domain}."
                ),
            ),
        )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self._spec.tool_name,
            description=self._spec.description,
            parameters=self._spec.to_json_schema(),
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        from extensions.adapter_runner._runner import run_adapter  # type: ignore[import-not-found]

        profile_home = _resolve_profile_home() or Path.home() / ".opencomputer"
        return await run_adapter(
            self._spec,
            arguments=dict(call.arguments or {}),
            profile_home=profile_home,
            call_id=call.id,
        )


# ─── doctor ────────────────────────────────────────────────────────


async def _doctor_run(fix: bool) -> RepairResult:  # noqa: ARG001
    discovery = _LAST_DISCOVERY
    if discovery is None:
        return RepairResult(
            id="adapter-runner",
            status="warn",
            detail="adapter-runner has not run discovery yet",
        )
    n = len(discovery.specs)
    if discovery.errors:
        # Truncate noisy error lists for the doctor row.
        sample = "; ".join(discovery.errors[:3])
        return RepairResult(
            id="adapter-runner",
            status="warn",
            detail=(
                f"discovered {n} adapter(s); {len(discovery.errors)} import "
                f"error(s): {sample}"
            ),
        )
    return RepairResult(
        id="adapter-runner",
        status="pass",
        detail=f"discovered {n} adapter(s)",
    )


__all__ = ["register"]
