"""AST + regex guard that runs after extract, before BEFORE_INSTALL hook.

Scope (Phase 1, see docs/superpowers/specs/2026-05-06-openclaw-deep-comparison-followup-design.md §3.3):

* `eval`/`exec`/`compile` whose argument is a network-fetch chain → BLOCK.
* `subprocess` / `os.system` calls referencing `rm -rf` → WARN.
* Suspicious raw-socket usage (DNS/TCP exfil shapes) → WARN.
* Unparseable .py files → WARN (the Python loader will catch real syntax errors at import).

Initial pattern severities are deliberately conservative; promotion of WARN
patterns to BLOCK is a one-line change after dogfooding.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Severity = Literal["info", "warn", "block"]


@dataclass(frozen=True)
class Finding:
    severity: Severity
    file: str
    line: int
    pattern: str
    excerpt: str  # truncated source snippet, ≤ 240 chars

    def __post_init__(self) -> None:
        # Frozen dataclass — must use object.__setattr__ to mutate.
        if len(self.excerpt) > 240:
            object.__setattr__(self, "excerpt", self.excerpt[:237] + "...")


@dataclass(frozen=True)
class ScanReport:
    findings: list[Finding] = field(default_factory=list)

    def has_blocks(self) -> bool:
        return any(f.severity == "block" for f in self.findings)

    def raise_for_blocks(self) -> None:
        if self.has_blocks():
            raise InstallSecurityScanError(self)


class InstallSecurityScanError(Exception):
    """Raised when ScanReport.raise_for_blocks() finds a block-severity finding."""

    def __init__(self, report: ScanReport) -> None:
        self.report = report
        blocks = [f for f in report.findings if f.severity == "block"]
        msg = (
            f"plugin security scan blocked install ({len(blocks)} blocking finding(s)):\n"
            + "\n".join(
                f"  - {f.file}:{f.line} [{f.pattern}] {f.excerpt}" for f in blocks
            )
        )
        super().__init__(msg)


# ─── Regex patterns (line-level) ───────────────────────────────────────

_REGEX_PATTERNS: list[tuple[str, Severity, re.Pattern[str]]] = [
    (
        "rm-rf-shell",
        "warn",
        # Catch both shell-string form (`rm -rf /tmp`) AND list-arg form
        # (`['rm', '-rf', ...]`) used with subprocess.run / Popen.
        re.compile(
            r"\brm\s+-rf\b"
            r"|\bshutil\.rmtree\b"
            r"|\bos\.unlink\b"
            r"|['\"]rm['\"]\s*,\s*['\"]\s*-rf\b"
        ),
    ),
    (
        "raw-socket",
        "warn",
        re.compile(r"\bsocket\.(socket|create_connection)\b"),
    ),
    (
        "os-system-shell",
        "warn",
        re.compile(r"\bos\.system\("),
    ),
]

# ─── AST visitor (block-severity patterns) ─────────────────────────────


_NETWORK_FETCH_NAMES = frozenset({"get", "post", "request", "urlopen", "Request"})


class _DangerousEvalVisitor(ast.NodeVisitor):
    """Detect eval/exec/compile whose argument is a network-fetch chain."""

    def __init__(self, file_str: str) -> None:
        self.file = file_str
        self.findings: list[Finding] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast naming
        func_name = _name_of(node.func)
        if func_name in {"eval", "exec", "compile"}:
            if any(_arg_chain_contains_network_fetch(a) for a in node.args):
                self.findings.append(
                    Finding(
                        severity="block",
                        file=self.file,
                        line=node.lineno,
                        pattern="eval-of-network-fetch",
                        excerpt=ast.unparse(node)[:240],
                    )
                )
        self.generic_visit(node)


def _name_of(node: ast.AST) -> str:
    """Return a short string name for an AST node (best-effort)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _arg_chain_contains_network_fetch(node: ast.AST) -> bool:
    """Walk an AST argument; return True if any sub-call's function name is a
    known network-fetch verb (requests.get, urllib.request.urlopen, etc.)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if _name_of(sub.func) in _NETWORK_FETCH_NAMES:
                return True
    return False


# ─── Top-level scan function ───────────────────────────────────────────


def scan_plugin_dir(plugin_dir: Path) -> ScanReport:
    """Scan every .py file under plugin_dir, return a ScanReport."""
    findings: list[Finding] = []
    for py in sorted(plugin_dir.rglob("*.py")):
        rel = str(py.relative_to(plugin_dir))
        try:
            source = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append(
                Finding(
                    severity="warn",
                    file=rel,
                    line=0,
                    pattern="unreadable-file",
                    excerpt="could not read source bytes",
                )
            )
            continue

        # AST pass (block patterns)
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as e:
            findings.append(
                Finding(
                    severity="warn",
                    file=rel,
                    line=getattr(e, "lineno", 0) or 0,
                    pattern="parse-error",
                    excerpt=str(e)[:240],
                )
            )
        else:
            visitor = _DangerousEvalVisitor(rel)
            visitor.visit(tree)
            findings.extend(visitor.findings)

        # Regex pass (warn patterns)
        for lineno, line_text in enumerate(source.splitlines(), start=1):
            for pattern_name, severity, regex in _REGEX_PATTERNS:
                if regex.search(line_text):
                    findings.append(
                        Finding(
                            severity=severity,
                            file=rel,
                            line=lineno,
                            pattern=pattern_name,
                            excerpt=line_text.strip()[:240],
                        )
                    )

    return ScanReport(findings=findings)


__all__ = [
    "Finding",
    "InstallSecurityScanError",
    "ScanReport",
    "Severity",
    "scan_plugin_dir",
]
