"""pytest conftest — test infrastructure for all tests in this directory.

This file registers module aliases so hyphenated extension directories can be
imported with underscores in test code:

1.  extensions.oi_capability  → extensions/oi-capability/  (legacy compat
    shim left in place after the 2026-04-25 trim; the use_cases sub-package
    that used to live here was deleted along with its tests since the OI
    Tier 1 tools it depended on were also removed as redundant. The shim
    itself can be deleted on the next major version bump.)
2.  extensions.coding_harness → extensions/coding-harness/  (PR-3; makes the
    new test_coding_harness_oi_*.py tests importable)

The aliases are injected into sys.modules BEFORE any test module is collected.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

# Project root (parent of tests/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_OI_DIR = _EXT_DIR / "oi-capability"
_CH_DIR = _EXT_DIR / "coding-harness"
_BEDROCK_DIR = _EXT_DIR / "aws-bedrock-provider"


def _ensure_extensions_pkg() -> None:
    """Synthesise a namespace package for 'extensions' if not already registered."""
    if "extensions" not in sys.modules:
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(_EXT_DIR)]
        ext_pkg.__package__ = "extensions"
        sys.modules["extensions"] = ext_pkg


def _register_oi_capability_alias() -> None:
    """Register extensions.oi_capability → extensions/oi-capability/ in sys.modules.

    Legacy compat shim retained after the 2026-04-25 trim. The
    ``use_cases`` sub-package was deleted along with its tests when the
    Tier 1 tools it depended on (read_file_region, search_files,
    read_git_log) were removed as redundant with built-in OC tools.
    The remaining alias still maps subprocess + tools sub-packages so
    test fixtures referring to ``extensions.oi_capability.tools.*``
    keep resolving — those names live at coding-harness/oi_bridge/.
    """
    _ensure_extensions_pkg()

    if "extensions.oi_capability" not in sys.modules:
        # Load the actual package from the hyphenated directory
        spec = importlib.util.spec_from_file_location(
            "extensions.oi_capability",
            str(_OI_DIR / "__init__.py"),
            submodule_search_locations=[str(_OI_DIR)],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot find extensions/oi-capability/__init__.py at {_OI_DIR}")

        oi_mod = importlib.util.module_from_spec(spec)
        oi_mod.__package__ = "extensions.oi_capability"
        oi_mod.__path__ = [str(_OI_DIR)]
        sys.modules["extensions.oi_capability"] = oi_mod
        spec.loader.exec_module(oi_mod)

    # Register sub-packages.
    # PR-3 (2026-04-25): subprocess/ and tools/ were moved to
    # coding-harness/oi_bridge/; the oi_capability.* aliases redirect
    # there so any legacy test that still imports from the old path
    # keeps resolving. The use_cases sub-package was deleted in the
    # 2026-04-25 trim along with its tests — no alias needed.
    _OI_BRIDGE_DIR = _CH_DIR / "oi_bridge"
    _sub_dirs = {
        "subprocess": _OI_BRIDGE_DIR / "subprocess",
        "tools": _OI_BRIDGE_DIR / "tools",
    }
    for sub, sub_dir in _sub_dirs.items():
        full_name = f"extensions.oi_capability.{sub}"
        if full_name not in sys.modules:
            init = sub_dir / "__init__.py"
            spec = importlib.util.spec_from_file_location(
                full_name,
                str(init),
                submodule_search_locations=[str(sub_dir)],
            ) if init.exists() else None
            if spec is None or not init.exists():
                continue
            mod = importlib.util.module_from_spec(spec)
            mod.__package__ = full_name
            mod.__path__ = [str(sub_dir)]
            sys.modules[full_name] = mod
            if spec.loader is not None:
                spec.loader.exec_module(mod)


def _register_coding_harness_alias() -> None:
    """Register extensions.coding_harness → extensions/coding-harness/ in sys.modules.

    Added in PR-3 (2026-04-25): makes test_coding_harness_oi_*.py importable now that
    OI tools live at extensions/coding-harness/oi_bridge/ per interweaving-plan.md.
    Mirrors the pattern used for extensions.oi_capability above.
    """
    _ensure_extensions_pkg()

    if "extensions.coding_harness" not in sys.modules:
        # coding-harness is a plugin dir — no __init__.py at the root; treat as namespace.
        ch_mod = types.ModuleType("extensions.coding_harness")
        ch_mod.__path__ = [str(_CH_DIR)]
        ch_mod.__package__ = "extensions.coding_harness"
        sys.modules["extensions.coding_harness"] = ch_mod

    # Register the oi_bridge sub-package and its children
    _oi_bridge_dir = _CH_DIR / "oi_bridge"
    for rel in ("oi_bridge", "oi_bridge/subprocess", "oi_bridge/tools"):
        full_name = "extensions.coding_harness." + rel.replace("/", ".")
        if full_name not in sys.modules:
            sub_dir = _CH_DIR / rel
            init = sub_dir / "__init__.py"
            spec = importlib.util.spec_from_file_location(
                full_name,
                str(init),
                submodule_search_locations=[str(sub_dir)],
            ) if init.exists() else None
            if spec is None or not init.exists():
                mod = types.ModuleType(full_name)
                mod.__path__ = [str(sub_dir)]
                mod.__package__ = full_name
                sys.modules[full_name] = mod
            else:
                mod = importlib.util.module_from_spec(spec)
                mod.__package__ = full_name
                mod.__path__ = [str(sub_dir)]
                sys.modules[full_name] = mod
                if spec.loader is not None:
                    spec.loader.exec_module(mod)


def _register_aws_bedrock_provider_alias() -> None:
    """Register extensions.aws_bedrock_provider → extensions/aws-bedrock-provider/.

    PR-C: allows test_bedrock_provider.py to import via the underscore form
    (Python module name) while the directory keeps the canonical hyphenated name.
    Mirrors the pattern used for coding_harness above.
    """
    _ensure_extensions_pkg()

    if "extensions.aws_bedrock_provider" not in sys.modules:
        mod = types.ModuleType("extensions.aws_bedrock_provider")
        mod.__path__ = [str(_BEDROCK_DIR)]
        mod.__package__ = "extensions.aws_bedrock_provider"
        sys.modules["extensions.aws_bedrock_provider"] = mod

    # Register transport.py and provider.py as importable sub-modules
    for sub in ("transport", "provider", "plugin"):
        full_name = f"extensions.aws_bedrock_provider.{sub}"
        if full_name not in sys.modules:
            init = _BEDROCK_DIR / f"{sub}.py"
            if not init.exists():
                continue
            spec = importlib.util.spec_from_file_location(
                full_name,
                str(init),
            )
            if spec is None or spec.loader is None:
                continue
            sub_mod = importlib.util.module_from_spec(spec)
            sub_mod.__package__ = "extensions.aws_bedrock_provider"
            sys.modules[full_name] = sub_mod
            # Do NOT exec yet — tests control when the module loads


def _register_browser_bridge_alias() -> None:
    """Register extensions.browser_bridge → extensions/browser-bridge/.

    Mirrors the pattern used for ``extensions.aws_bedrock_provider`` —
    plugins live in hyphenated dirs, but Python modules need underscores.
    Layered Awareness MVP T10: lets tests import the adapter / plugin
    Python modules from the hyphenated ``browser-bridge/`` directory.

    We register the parent package (with ``__path__`` pointing at the
    hyphenated dir) so Python's standard import machinery resolves
    ``extensions.browser_bridge.adapter`` against ``adapter.py`` in
    that directory. We pre-stub the sub-modules with their spec but
    actually execute them on first import — unlike the bedrock pattern
    (which expects test fixtures to ``sys.modules.pop()`` before import),
    the browser-bridge tests import directly, so leaving an unexecuted
    stub in ``sys.modules`` would mask the real module.
    """
    _ensure_extensions_pkg()
    _BB_DIR = _EXT_DIR / "browser-bridge"

    if "extensions.browser_bridge" not in sys.modules:
        mod = types.ModuleType("extensions.browser_bridge")
        mod.__path__ = [str(_BB_DIR)]
        mod.__package__ = "extensions.browser_bridge"
        sys.modules["extensions.browser_bridge"] = mod

    for sub in ("adapter", "plugin"):
        full_name = f"extensions.browser_bridge.{sub}"
        if full_name not in sys.modules:
            init = _BB_DIR / f"{sub}.py"
            if not init.exists():
                continue
            spec = importlib.util.spec_from_file_location(full_name, str(init))
            if spec is None or spec.loader is None:
                continue
            sub_mod = importlib.util.module_from_spec(spec)
            sub_mod.__package__ = "extensions.browser_bridge"
            sys.modules[full_name] = sub_mod
            spec.loader.exec_module(sub_mod)


_register_oi_capability_alias()
_register_coding_harness_alias()
_register_aws_bedrock_provider_alias()
_register_browser_bridge_alias()
