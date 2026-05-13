"""LspDiagnostics tool — agent-facing front for the LSP bridge.

Inputs: ``{"path": "/abs/or/relative/path/to/file.py"}``
Output: human-readable diagnostics summary or "no diagnostics" / install hint.

Per OC tool conventions (see ``opencomputer/tools/sessions.py``), this
tool is ``parallel_safe = True`` (multiple files can be checked
concurrently — each spawns its own subprocess) and never raises:
errors are surfaced as ``ToolResult`` ``content`` text with
``is_error=True``.
"""

from __future__ import annotations

from pathlib import Path

from language_server import server_for_extension  # type: ignore[import-not-found]
from lsp_client import collect_diagnostics  # type: ignore[import-not-found]

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class LspDiagnostics(BaseTool):
    """Run an LSP server against one file and return the diagnostics."""

    parallel_safe = True
    strict_mode = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="LspDiagnostics",
            description=(
                "Run a Language Server (pyright for Python; "
                "typescript-language-server for TS/JS) against one file "
                "and return the diagnostics it reports.\n"
                "\n"
                "Use this when:\n"
                "  - You want to know what's wrong with a file before editing it\n"
                "  - You just edited a file and want a fast type/lint check\n"
                "  - The user says 'check this file for errors' / "
                "'lint this' / 'are there any type errors here'\n"
                "\n"
                "Do NOT use this for:\n"
                "  - Running the full project test suite (use Bash with the "
                "project's test runner)\n"
                "  - Cross-file refactor previews (LSP gives per-file results)\n"
                "  - Large directories — call once per file, not per directory\n"
                "\n"
                "Returns a list of {line, column, severity, message, code, "
                "source} entries, or an empty list with a note when the file "
                "is clean. Returns an install hint if the relevant LSP "
                "server isn't on PATH."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute or working-directory-relative path to "
                            "the file to lint. The extension determines "
                            "which LSP server is used (.py → pyright; "
                            ".ts/.tsx/.js/.jsx → typescript-language-server)."
                        ),
                    },
                },
                "required": ["path"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        path = str(call.arguments.get("path", "")).strip()
        if not path:
            return ToolResult(
                tool_call_id=call.id,
                content="error: 'path' parameter is required",
                is_error=True,
            )
        ext = Path(path).suffix
        srv = server_for_extension(ext)
        if srv is None:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"unsupported file extension: {ext!r}. "
                    "LspDiagnostics supports .py, .pyi (pyright) and "
                    ".ts, .tsx, .js, .jsx, .mjs, .cjs "
                    "(typescript-language-server)."
                ),
                is_error=True,
            )
        if not srv.is_available():
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"{srv.executable!r} not found on PATH. "
                    f"{srv.install_hint}"
                ),
                is_error=True,
            )
        result = await collect_diagnostics(
            server_executable=srv.executable,
            server_args=srv.args,
            server_name=srv.name,
            file_path=path,
        )
        if result.error and not result.diagnostics:
            return ToolResult(
                tool_call_id=call.id,
                content=f"{srv.name}: {result.error}",
                is_error=True,
            )
        if not result.diagnostics:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"{srv.name}: no diagnostics for {path} "
                    f"(file is clean)."
                ),
                is_error=False,
            )
        lines = [
            f"{srv.name}: {len(result.diagnostics)} diagnostic(s) for {path}"
        ]
        for d in result.diagnostics:
            code = f" [{d.code}]" if d.code else ""
            src = f" ({d.source})" if d.source else ""
            lines.append(
                f"  {d.line}:{d.column} {d.severity}{code}{src}: {d.message}"
            )
        if result.error:
            lines.append(f"note: {result.error}")
        return ToolResult(
            tool_call_id=call.id,
            content="\n".join(lines),
            is_error=False,
        )
