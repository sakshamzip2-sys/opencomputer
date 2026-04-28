"""Ensure plugin_sdk/consent.py does not import from opencomputer.*"""
import re
from pathlib import Path


def test_consent_module_has_no_opencomputer_imports():
    src = Path("plugin_sdk/consent.py").read_text()
    assert "from opencomputer" not in src
    assert "import opencomputer" not in src


def test_no_opencomputer_imports_anywhere_in_plugin_sdk():
    """Generalized boundary check: no plugin_sdk module imports from opencomputer.*.

    PR-1 review M4: the original test only inspected consent.py. As more
    modules land in plugin_sdk/, regression risk grows. This walks the
    whole package and fails on any ``from opencomputer`` / ``import
    opencomputer`` line, indented or not.
    """
    plugin_sdk_root = Path(__file__).parent.parent / "plugin_sdk"
    bad: list[str] = []
    for py in plugin_sdk_root.rglob("*.py"):
        src = py.read_text()
        if re.search(r"^\s*(from opencomputer|import opencomputer)", src, re.MULTILINE):
            bad.append(str(py.relative_to(plugin_sdk_root.parent)))
    assert not bad, f"plugin_sdk modules importing opencomputer.*: {bad}"
