"""Ensure plugin_sdk/consent.py does not import from opencomputer.*"""
from pathlib import Path


def test_consent_module_has_no_opencomputer_imports():
    src = Path("plugin_sdk/consent.py").read_text()
    assert "from opencomputer" not in src
    assert "import opencomputer" not in src
