"""Defense-in-depth denylist for PythonExec scripts.

This is NOT a sandbox — the actual isolation is venv + subprocess. This
module just rejects scripts containing patterns that no legitimate
data-analysis script would need. False positives are acceptable; false
negatives are not.
"""
from __future__ import annotations


class PythonSafetyError(RuntimeError):
    """Raised when a script fails the safety check."""


#: Substring patterns that indicate dangerous intent. We use literal
#: substring matching, not full AST parsing — because the threat model is
#: "stop the obvious bad calls", not "prevent a determined attacker."
_BLOCKED_PATTERNS: tuple[str, ...] = (
    "os.system",
    "os.popen",
    "subprocess.",
    "subprocess ",
    "eval(",
    "exec(",
    "__import__",
    "/.ssh/",
    "/.aws/",
    "/.config/gh/",
    "/etc/passwd",
    "/etc/shadow",
    "compile(",
    "getattr(__builtins__",
    "globals()[",
    "shutil.rmtree",
    "Path(\"/\").",
    "rm -rf",
)


def is_safe_script(script: str) -> bool:
    """Return False if the script contains any denylist pattern."""
    return not any(p in script for p in _BLOCKED_PATTERNS)
