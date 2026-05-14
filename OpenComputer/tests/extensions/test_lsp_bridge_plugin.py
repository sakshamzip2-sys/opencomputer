"""Tests for the lsp-bridge plugin.

Covers the static parts (extension lookup table, tool schema, error
paths for missing executables / unsupported extensions). The actual
JSON-RPC interaction with a real server is exercised in a separate
integration test that's marked to skip when no LSP server is on PATH.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from plugin_sdk.core import ToolCall

PLUGIN_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "extensions"
    / "lsp-bridge"
)


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


language_server_mod = _load(
    "lsp_bridge_language_server_test_only",
    PLUGIN_DIR / "language_server.py",
)
SERVERS = language_server_mod.SERVERS
server_for_extension = language_server_mod.server_for_extension

# lsp_client and lsp_diagnostics_tool reference each other and the
# language_server module by short name — alias before loading.
sys.modules["language_server"] = language_server_mod
lsp_client_mod = _load(
    "lsp_bridge_lsp_client_test_only", PLUGIN_DIR / "lsp_client.py"
)
sys.modules["lsp_client"] = lsp_client_mod
lsp_diagnostics_tool_mod = _load(
    "lsp_bridge_diag_tool_test_only",
    PLUGIN_DIR / "lsp_diagnostics_tool.py",
)
LspDiagnostics = lsp_diagnostics_tool_mod.LspDiagnostics


def test_server_lookup_python():
    s = server_for_extension(".py")
    assert s is not None
    assert s.name == "pyright"


def test_server_lookup_typescript():
    s = server_for_extension(".tsx")
    assert s is not None
    assert s.name == "typescript-language-server"


def test_server_lookup_unsupported():
    # An extension with no LSP server in the table — pick one that isn't
    # in any known LSP catalogue. Update if we ever add support for this.
    assert server_for_extension(".invalidext") is None
    assert server_for_extension(".xyz") is None


def test_servers_have_install_hints():
    # Defensive: every entry in the table must give the user a way to
    # recover from "not installed".
    for s in SERVERS:
        assert s.install_hint, f"{s.name} missing install_hint"
        assert s.executable, f"{s.name} missing executable"
        assert s.extensions, f"{s.name} missing extensions"


def test_tool_schema_shape():
    tool = LspDiagnostics()
    schema = tool.schema
    assert schema.name == "LspDiagnostics"
    params = schema.parameters
    assert params["type"] == "object"
    assert params["additionalProperties"] is False
    assert "path" in params["properties"]
    assert params["required"] == ["path"]


def test_tool_is_parallel_safe():
    # Concurrent file checks each spawn their own subprocess — no
    # shared state between calls.
    assert LspDiagnostics.parallel_safe is True


def test_missing_path_is_user_error():
    tool = LspDiagnostics()
    res = asyncio.run(tool.execute(ToolCall(id="x", name="LspDiagnostics", arguments={})))
    assert res.is_error is True
    assert "path" in res.content.lower()


def test_unsupported_extension_is_user_error():
    tool = LspDiagnostics()
    res = asyncio.run(
        tool.execute(
            ToolCall(
                id="x",
                name="LspDiagnostics",
                arguments={"path": "/tmp/foo.invalidext"},
            )
        )
    )
    assert res.is_error is True
    assert "unsupported" in res.content.lower()


@pytest.mark.parametrize(
    "ext",
    [
        # pyright
        ".py", ".pyi",
        # typescript-language-server
        ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
        # gopls
        ".go",
        # rust-analyzer
        ".rs",
        # clangd
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh",
        # omnisharp
        ".cs",
        # jdtls
        ".java",
        # kotlin-language-server
        ".kt", ".kts",
        # lua-language-server
        ".lua",
        # intelephense
        ".php",
        # solargraph
        ".rb",
        # sourcekit-lsp
        ".swift",
    ],
)
def test_all_supported_extensions_resolve(ext):
    assert server_for_extension(ext) is not None


def test_missing_server_returns_install_hint(tmp_path, monkeypatch):
    """When the configured executable isn't on PATH, the tool should
    return ``is_error=True`` AND mention how to install."""
    target = tmp_path / "foo.py"
    target.write_text("x = 1\n")

    # Force shutil.which to miss for both servers.
    import shutil

    real_which = shutil.which

    def fake_which(name):
        return None if "pyright" in name or "typescript" in name else real_which(name)

    monkeypatch.setattr(shutil, "which", fake_which)

    tool = LspDiagnostics()
    res = asyncio.run(
        tool.execute(
            ToolCall(
                id="x",
                name="LspDiagnostics",
                arguments={"path": str(target)},
            )
        )
    )
    assert res.is_error is True
    assert (
        "install" in res.content.lower()
        or "not found" in res.content.lower()
    )
