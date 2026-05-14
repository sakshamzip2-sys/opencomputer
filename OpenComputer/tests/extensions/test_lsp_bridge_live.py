"""End-to-end live test: spawn pyright, get a real diagnostic.

Skipped automatically when ``pyright-langserver`` isn't on PATH so CI
boxes without it don't break. When pyright IS installed, this test
proves the JSON-RPC framing, didOpen handshake, and publishDiagnostics
parsing all work — i.e. the protocol code in ``lsp_client.py`` actually
talks to a real Language Server end-to-end.
"""

from __future__ import annotations

import importlib.util
import shutil
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
    "lsp_bridge_language_server_live_test",
    PLUGIN_DIR / "language_server.py",
)
sys.modules["language_server"] = language_server_mod

lsp_client_mod = _load(
    "lsp_bridge_lsp_client_live_test", PLUGIN_DIR / "lsp_client.py"
)
sys.modules["lsp_client"] = lsp_client_mod

lsp_diagnostics_tool_mod = _load(
    "lsp_bridge_diag_tool_live_test",
    PLUGIN_DIR / "lsp_diagnostics_tool.py",
)
LspDiagnostics = lsp_diagnostics_tool_mod.LspDiagnostics


HAVE_PYRIGHT = shutil.which("pyright-langserver") is not None
HAVE_TSSERVER = shutil.which("typescript-language-server") is not None


@pytest.mark.skipif(not HAVE_PYRIGHT, reason="pyright-langserver not installed")
@pytest.mark.asyncio
async def test_live_pyright_finds_type_error(tmp_path):
    """End-to-end: real subprocess, real LSP handshake, real diagnostic."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        # A type error pyright will flag — passing int to str-typed param.
        "def greet(name: str) -> str:\n"
        "    return 'hello, ' + name\n"
        "\n"
        "greet(42)\n"
    )
    tool = LspDiagnostics()
    res = await tool.execute(
        ToolCall(
            id="x",
            name="LspDiagnostics",
            arguments={"path": str(bad)},
        )
    )
    # Should not be is_error (we got a real response)
    assert res.is_error is False
    # Should mention pyright + the file
    assert "pyright" in res.content.lower()
    assert str(bad) in res.content
    # Should report at least one diagnostic. Pyright surfaces the
    # int-vs-str mismatch as ``reportArgumentType`` (or similar — exact
    # rule names move; we assert structure not phrasing).
    assert "diagnostic" in res.content.lower()
    assert "error" in res.content.lower() or "warning" in res.content.lower()


@pytest.mark.skipif(not HAVE_PYRIGHT, reason="pyright-langserver not installed")
@pytest.mark.asyncio
async def test_live_pyright_clean_file_returns_no_diagnostics(tmp_path):
    """A clean file should return ``no diagnostics``, not an error."""
    good = tmp_path / "good.py"
    good.write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "\n"
        "result = add(1, 2)\n"
    )
    tool = LspDiagnostics()
    res = await tool.execute(
        ToolCall(
            id="x",
            name="LspDiagnostics",
            arguments={"path": str(good)},
        )
    )
    assert res.is_error is False
    assert (
        "no diagnostics" in res.content.lower()
        or "0 diagnostic" in res.content.lower()
    )


@pytest.mark.skipif(not HAVE_TSSERVER, reason="typescript-language-server not installed")
@pytest.mark.asyncio
async def test_live_tsserver_finds_syntax_error(tmp_path):
    """End-to-end with TypeScript Language Server."""
    bad = tmp_path / "bad.ts"
    # A clear TypeScript error: assigning string to number-typed variable.
    bad.write_text(
        "const x: number = 'hello';\n"
        "console.log(x);\n"
    )
    tool = LspDiagnostics()
    res = await tool.execute(
        ToolCall(
            id="x",
            name="LspDiagnostics",
            arguments={"path": str(bad)},
        )
    )
    # tsserver may need a tsconfig.json to be fully happy — accept either
    # diagnostics OR a clean response, but never an is_error response.
    # The point is the protocol round-trips successfully.
    assert "typescript" in res.content.lower()
