"""pytest conftest — test infrastructure for all tests in this directory.

This file registers module aliases so the oi-capability plugin (which has a
hyphenated directory name, `extensions/oi-capability/`) can be imported as
`extensions.oi_capability` in test code.

The alias is injected into sys.modules BEFORE any test module is collected,
so all test_oi_*.py files can use:

    from extensions.oi_capability.subprocess.protocol import ...
    from extensions.oi_capability.tools.tier_1_introspection import ...
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


def _register_oi_capability_alias() -> None:
    """Register extensions.oi_capability → extensions/oi-capability/ in sys.modules."""
    if "extensions" not in sys.modules:
        # Synthesise a namespace package for 'extensions'
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(_EXT_DIR)]
        ext_pkg.__package__ = "extensions"
        sys.modules["extensions"] = ext_pkg

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

    # Register sub-packages: subprocess, tools, and use_cases
    for sub in ("subprocess", "tools", "use_cases"):
        full_name = f"extensions.oi_capability.{sub}"
        if full_name not in sys.modules:
            sub_dir = _OI_DIR / sub
            spec = importlib.util.spec_from_file_location(
                full_name,
                str(sub_dir / "__init__.py"),
                submodule_search_locations=[str(sub_dir)],
            )
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            mod.__package__ = full_name
            mod.__path__ = [str(sub_dir)]
            sys.modules[full_name] = mod
            spec.loader.exec_module(mod)


_register_oi_capability_alias()
