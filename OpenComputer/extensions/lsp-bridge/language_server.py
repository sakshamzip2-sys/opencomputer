"""Per-language LSP server configuration table.

Maps a file extension to the executable + args needed to spawn the
corresponding Language Server. ``which()`` is checked at call time so a
missing server returns a guided error rather than a generic crash.

Out-of-the-box support spans the 12 language families Anthropic ships
LSP-shell plugins for: Python (pyright), TypeScript/JavaScript
(typescript-language-server), Go (gopls), Rust (rust-analyzer), C/C++
(clangd), C# (omnisharp), Java (jdtls), Kotlin (kotlin-language-server),
Lua (lua-language-server), PHP (intelephense), Ruby (solargraph),
Swift (sourcekit-lsp).

Adding a new language is a one-line entry in :data:`SERVERS` plus the
install hint. The tool itself stays language-agnostic — see
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
    LanguageServer(
        name="gopls",
        executable="gopls",
        args=("serve",),
        install_hint=(
            "Install with: go install golang.org/x/tools/gopls@latest"
        ),
        extensions=(".go",),
    ),
    LanguageServer(
        name="rust-analyzer",
        executable="rust-analyzer",
        args=(),
        install_hint=(
            "Install with: rustup component add rust-analyzer "
            "(or download from rust-analyzer.github.io)"
        ),
        extensions=(".rs",),
    ),
    LanguageServer(
        name="clangd",
        executable="clangd",
        args=("--background-index",),
        install_hint=(
            "Install with: brew install llvm (macOS) or "
            "apt-get install clangd (Debian/Ubuntu)"
        ),
        extensions=(".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh"),
    ),
    LanguageServer(
        name="omnisharp",
        executable="omnisharp",
        args=("-lsp",),
        install_hint=(
            "Install with: brew install omnisharp-roslyn (macOS) or "
            "see github.com/OmniSharp/omnisharp-roslyn for binaries"
        ),
        extensions=(".cs",),
    ),
    LanguageServer(
        name="jdtls",
        executable="jdtls",
        args=(),
        install_hint=(
            "Install with: brew install jdtls (macOS) or "
            "see download.eclipse.org/jdtls/snapshots/?d for releases"
        ),
        extensions=(".java",),
    ),
    LanguageServer(
        name="kotlin-language-server",
        executable="kotlin-language-server",
        args=(),
        install_hint=(
            "Install with: brew install kotlin-language-server (macOS) "
            "or see github.com/fwcd/kotlin-language-server"
        ),
        extensions=(".kt", ".kts"),
    ),
    LanguageServer(
        name="lua-language-server",
        executable="lua-language-server",
        args=(),
        install_hint=(
            "Install with: brew install lua-language-server (macOS) "
            "or see github.com/LuaLS/lua-language-server"
        ),
        extensions=(".lua",),
    ),
    LanguageServer(
        name="intelephense",
        executable="intelephense",
        args=("--stdio",),
        install_hint="Install with: npm install -g intelephense",
        extensions=(".php",),
    ),
    LanguageServer(
        name="solargraph",
        executable="solargraph",
        args=("stdio",),
        install_hint="Install with: gem install solargraph",
        extensions=(".rb",),
    ),
    LanguageServer(
        name="sourcekit-lsp",
        executable="sourcekit-lsp",
        args=(),
        install_hint=(
            "Install with: xcode-select --install (macOS) — ships with "
            "the Swift toolchain"
        ),
        extensions=(".swift",),
    ),
)


def server_for_extension(ext: str) -> LanguageServer | None:
    """Return the configured server for ``ext`` or ``None`` if unsupported."""
    ext = ext.lower()
    for srv in SERVERS:
        if ext in srv.extensions:
            return srv
    return None
