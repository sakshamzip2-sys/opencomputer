"""Security pattern catalogue for the PreToolUse warning hook.

Patterns are checked in declaration order — first match wins. Each
pattern is one of:

- ``path_check``: a callable ``(normalized_path) -> bool`` (e.g. for
  GitHub Actions workflow files where the risk is in the file's role).
- ``substrings``: a tuple of strings to search inside the new content
  (e.g. ``"eval("``, ``"pickle"``).

Both kinds attach a ``reminder`` body that the hook injects via
``HookDecision.reason`` when the pattern fires.

This catalogue is a near-verbatim port of Anthropic's ``security-guidance``
plugin (commit reference in PR body) — the rules themselves are language
patterns that don't depend on the host harness.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SecurityPattern:
    """One rule the hook can fire on."""

    rule_name: str
    reminder: str
    path_check: Callable[[str], bool] | None = None
    substrings: tuple[str, ...] = ()


def _is_workflow_path(path: str) -> bool:
    """True for GitHub Actions workflow files."""
    return ".github/workflows/" in path and path.endswith((".yml", ".yaml"))


_GITHUB_ACTIONS_REMINDER = """You are editing a GitHub Actions workflow file. Common injection sinks:

1. **Command injection** via untrusted ${{ github.event.* }} interpolated directly into `run:` commands.
2. Use env: with quoted values instead of inline interpolation. Example:

   UNSAFE:
   run: echo "${{ github.event.issue.title }}"

   SAFE:
   env:
     TITLE: ${{ github.event.issue.title }}
   run: echo "$TITLE"

Other risky inputs to watch:
- github.event.issue.body
- github.event.pull_request.{title,body,head.ref,head.label}
- github.event.{comment,review,review_comment}.body
- github.event.{commits,head_commit}.{message,author.email,author.name}
- github.head_ref"""


SECURITY_PATTERNS: tuple[SecurityPattern, ...] = (
    SecurityPattern(
        rule_name="github_actions_workflow",
        path_check=_is_workflow_path,
        reminder=_GITHUB_ACTIONS_REMINDER,
    ),
    SecurityPattern(
        rule_name="child_process_exec",
        substrings=("child_process.exec", "exec(", "execSync("),
        reminder=(
            "Security: child_process.exec() can lead to command injection. "
            "Prefer execFile() with an args array, or a hardened wrapper "
            "(e.g. execFileNoThrow). Only use exec() when shell features are "
            "required AND every argument is statically known."
        ),
    ),
    SecurityPattern(
        rule_name="new_function_injection",
        substrings=("new Function",),
        reminder=(
            "Security: new Function() with dynamic strings is code injection. "
            "Replace with a structured representation (JSON config, AST, "
            "rule engine) unless evaluating arbitrary user-supplied code is "
            "the explicit feature."
        ),
    ),
    SecurityPattern(
        rule_name="eval_injection",
        substrings=("eval(",),
        reminder=(
            "Security: eval() executes arbitrary code. Prefer JSON.parse() "
            "for data parsing, or a domain-specific parser. Only retain "
            "eval() if evaluating arbitrary code is the actual feature."
        ),
    ),
    SecurityPattern(
        rule_name="react_dangerously_set_html",
        substrings=("dangerouslySetInnerHTML",),
        reminder=(
            "Security: dangerouslySetInnerHTML is XSS-prone unless the value "
            "is sanitized. Pass content through DOMPurify (or equivalent) "
            "first, or render via React children to avoid raw HTML."
        ),
    ),
    SecurityPattern(
        rule_name="document_write_xss",
        substrings=("document.write",),
        reminder=(
            "Security: document.write() is an XSS sink and has performance "
            "issues. Use createElement/appendChild or a templating layer."
        ),
    ),
    SecurityPattern(
        rule_name="innerHTML_xss",
        substrings=(".innerHTML =", ".innerHTML="),
        reminder=(
            "Security: setting .innerHTML with untrusted content is XSS. "
            "Use textContent for text, or sanitize via DOMPurify before "
            "assigning HTML."
        ),
    ),
    SecurityPattern(
        rule_name="pickle_deserialization",
        substrings=("pickle",),
        reminder=(
            "Security: pickle deserializes arbitrary code on load. Replace "
            "with JSON, msgpack, or another safe format unless the data is "
            "produced AND consumed within the same trust boundary."
        ),
    ),
    SecurityPattern(
        rule_name="os_system_injection",
        substrings=("os.system", "from os import system"),
        reminder=(
            "Security: os.system() runs through the shell. Prefer "
            "subprocess.run([...]) with an args list. Only use os.system "
            "with statically-known arguments."
        ),
    ),
)


def find_match(file_path: str, content: str) -> SecurityPattern | None:
    """Return the first pattern matching path or content, or None."""
    normalized_path = file_path.lstrip("/")
    for p in SECURITY_PATTERNS:
        if p.path_check is not None and p.path_check(normalized_path):
            return p
        if p.substrings and content:
            for needle in p.substrings:
                if needle in content:
                    return p
    return None
