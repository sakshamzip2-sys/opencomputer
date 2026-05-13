"""Per-language LSP server configuration table.

Maps a file extension to the executable + args needed to spawn the
corresponding Language Server. ``which()`` is checked at call time so a
missing server returns a guided error rather than a generic crash.

Supported out of the box: Python (pyright), TypeScript / JavaScript
(typescript-language-server). Adding a new language is a one-line entry
in :data:`SERVERS` plus the install hint.

The tool itself stays language-agnostic — see
``lsp_diagnostics_tool.LspDiagnostics``.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LanguageServer:
    """How to spawn the LSP for one language family."""

    name: str
    executable: str
    args: tuple[str, ...]
    install_hint: str
    extensions: tuple[str, ...]

    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None


SERVERS: tuple[LanguageServer, ...] = (
    LanguageServer(
        name="pyright",
        executable="pyright-langserver",
        args=("--stdio",),
        install_hint="Install with: npm install -g pyright",
        extensions=(".py", ".pyi"),
    ),
    LanguageServer(
        name="typescript-language-server",
        executable="typescript-language-server",
        args=("--stdio",),
        install_hint=(
            "Install with: npm install -g typescript "
            "typescript-language-server"
        ),
        extensions=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    ),
)


def server_for_extension(ext: str) -> LanguageServer | None:
    """Return the configured server for ``ext`` or ``None`` if unsupported."""
    ext = ext.lower()
    for srv in SERVERS:
        if ext in srv.extensions:
            return srv
    return None
