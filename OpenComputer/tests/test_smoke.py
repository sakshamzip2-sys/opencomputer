"""Smoke tests — verify the package imports and CLI wiring works."""

from __future__ import annotations


def test_package_imports() -> None:
    import opencomputer

    assert opencomputer.__version__ == "0.0.1"


def test_cli_module_imports() -> None:
    from opencomputer import cli

    assert hasattr(cli, "main")
    assert hasattr(cli, "app")


def test_plugin_sdk_imports() -> None:
    import plugin_sdk

    assert plugin_sdk.__version__ == "0.1.0"
