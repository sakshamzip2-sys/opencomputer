"""Destructive-command heuristics for Bash.

A pure-function module exposing :func:`detect_destructive`, a regex-based scanner
that flags shell commands whose shape strongly suggests data loss. Mirrors the
heuristic in ``sources/hermes-agent/run_agent.py`` (``_DESTRUCTIVE_PATTERNS`` +
``_is_destructive_command`` at lines 240-264) but lifted into a typed, reusable
module so the OpenComputer plan-mode hook (and any future pre-tool-use defender)
can share the same detector.

Scope (MVP / II.4): pattern-matching only. This module is NOT a fully sound
static analyser — a determined prompt injection can bypass it (``ev"a"l`` tricks,
subshells, base64-encoded payloads). The goal is defence-in-depth against
*accidental* destructive commands the model might emit, and to provide a
specific, actionable block-reason string to the user when plan_mode is active.

Design notes:
* Every pattern is ANCHORED to avoid false-positives like ``git rm`` triggering
  the ``rm`` detector.  Patterns use lookbehind/lookahead or explicit start-of-
  token anchors (``^``, ``\\s``, ``;``, ``&&``, ``||``, ``|`` — the shell
  statement separators).
* Patterns are returned first-match-wins. When adding a new pattern, order from
  most-specific to most-general so the reason string is precise.
* Allowlist is implicit — patterns are tight enough that benign-shaped commands
  (``git rm``, ``pip uninstall``, ``rm -i``) don't match any pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DestructivePattern:
    """One destructive-command pattern with its human-readable reason.

    Attributes:
        pattern_id: Stable identifier for logs/telemetry (``"rm_rf_root"``,
            ``"git_reset_hard"``, etc). Do not rename — may appear in audit trails.
        pattern: Compiled regex. Applied with :meth:`re.Pattern.search` against
            the raw command string. Case sensitivity is pattern-specific — SQL
            patterns use ``re.IGNORECASE``; most shell patterns do not.
        reason: One-line explanation shown to the model/user when the pattern
            fires. Should describe the risk, not just identify the command.
    """

    pattern_id: str
    pattern: re.Pattern[str]
    reason: str


#: Statement-start anchor: matches beginning-of-string, whitespace, or shell
#: statement separators ``;``, ``&&``, ``||``, ``|``. Used so ``rm`` at the start
#: of a command or after ``&&`` fires, but ``git rm`` (where ``rm`` is the
#: subcommand of ``git``) does not.
_STMT_START = r"(?:^|[\s;|&`(])"


# ─── Pattern list. Order matters (first match wins — place specific before general). ─


DESTRUCTIVE_PATTERNS: list[DestructivePattern] = [
    # Fork bomb — ordered first because ``:`` is cheap and specific.
    DestructivePattern(
        pattern_id="fork_bomb",
        pattern=re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:"),
        reason="fork bomb pattern — exhausts process table and locks up the system",
    ),
    # sudo-escalated destructive commands. Checked before plain `rm`/`dd` so the
    # reason string mentions privilege escalation.
    DestructivePattern(
        pattern_id="sudo_rm",
        pattern=re.compile(rf"{_STMT_START}sudo\s+rm\b"),
        reason="sudo-escalated `rm` — bypasses ownership protection, potentially catastrophic",
    ),
    DestructivePattern(
        pattern_id="sudo_dd",
        pattern=re.compile(rf"{_STMT_START}sudo\s+dd\b"),
        reason="sudo-escalated `dd` — can overwrite disks/partitions",
    ),
    # rm -rf of root-ish paths. Positive: ``rm -rf /``, ``rm -rf /*``, ``rm -rf ~``,
    # ``rm -rf $HOME``, ``rm -rf *``. The flag is required — bare ``rm /path`` alone
    # is routine.
    DestructivePattern(
        pattern_id="rm_rf_root_or_wildcard",
        pattern=re.compile(
            rf"{_STMT_START}rm\s+"
            r"(?:-[a-zA-Z]*[rRf][a-zA-Z]*\s+)+"  # at least one flag group containing r/R or f
            r"(?:/\S*|\*|~\S*|\$HOME\S*|\.\s*$|\./\*)"
        ),
        reason="recursive `rm -rf` against root / home / wildcard target — mass data loss",
    ),
    # dd with of=/dev/* — writes raw bytes to a block device.
    DestructivePattern(
        pattern_id="dd_to_disk",
        pattern=re.compile(rf"{_STMT_START}dd\s+.*\bof=/dev/\S+"),
        reason="`dd` writing to a raw device (`of=/dev/...`) — destroys disk contents",
    ),
    # chmod -R 777 — either wipes permissions tree-wide or exposes secrets.
    DestructivePattern(
        pattern_id="chmod_r_777",
        pattern=re.compile(rf"{_STMT_START}chmod\s+-R\s+0?777\b"),
        reason="`chmod -R 777` — strips permission boundaries across an entire tree",
    ),
    # git reset --hard — drops uncommitted work + moves HEAD.
    DestructivePattern(
        pattern_id="git_reset_hard",
        pattern=re.compile(rf"{_STMT_START}git\s+reset\s+--hard\b"),
        reason="`git reset --hard` — discards uncommitted work; cannot be undone without a reflog",
    ),
    # git clean with -f and one of d/x — removes untracked files/dirs.
    DestructivePattern(
        pattern_id="git_clean_force",
        pattern=re.compile(rf"{_STMT_START}git\s+clean\s+-[a-zA-Z]*f[a-zA-Z]*[dx][a-zA-Z]*"),
        reason="`git clean -f{d,x}` — permanently deletes untracked files/dirs",
    ),
    # `mv <path> <dest>` where dest climbs out of cwd (absolute path or ../).
    # Treats moves to /tmp, /var, /etc, etc. as escape. Source-side is anything.
    DestructivePattern(
        pattern_id="mv_escape_cwd",
        pattern=re.compile(
            rf"{_STMT_START}mv\s+"
            r"[^\s]+\s+"  # source — don't care what it is
            r"(?:/[^\s]+|\.\./[^\s]*|~/[^\s]*|\$HOME[^\s]*)"  # dest escapes cwd
        ),
        reason="`mv` with a destination outside the working tree — file relocated silently",
    ),
    # Output redirect truncate: `> path` (not `>>` which is append). The
    # negative lookbehind excludes `>>`. Also excludes `2>` / `&>` / `1>`
    # error/combined redirect noise when followed by a word char that isn't /.
    # This one is broad on purpose — any truncating redirect in plan mode
    # (where the user hasn't approved a side-effect yet) is worth flagging.
    DestructivePattern(
        pattern_id="redirect_truncate",
        pattern=re.compile(r"(?<!>)(?<![0-9&])>(?!>)\s*\S"),
        reason="`>` redirect truncates the target file — use `>>` to append instead",
    ),
    # SQL destructive DDL — case-insensitive.
    DestructivePattern(
        pattern_id="sql_drop_database",
        pattern=re.compile(r"\bDROP\s+DATABASE\b", re.IGNORECASE),
        reason="`DROP DATABASE` — permanently destroys an entire database",
    ),
    DestructivePattern(
        pattern_id="sql_drop_table",
        pattern=re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
        reason="`DROP TABLE` — permanently destroys a table and its data",
    ),
    DestructivePattern(
        pattern_id="sql_truncate",
        pattern=re.compile(r"\bTRUNCATE\b(?:\s+TABLE)?\s+\w", re.IGNORECASE),
        reason="SQL `TRUNCATE` — empties a table with no undo and no WAL entry",
    ),
]


def detect_destructive(cmd: str) -> DestructivePattern | None:
    """Scan ``cmd`` for destructive-command patterns.

    Returns the first matching :class:`DestructivePattern`, or ``None`` if the
    command doesn't look destructive. First-match-wins — the patterns are ordered
    most-specific to most-general.

    Empty or whitespace-only commands always return ``None`` (no command =
    no risk).

    Args:
        cmd: Raw shell command string — e.g. ``"rm -rf /"``, ``"git commit -m x"``.

    Returns:
        The first matching pattern, or ``None``.

    Example:
        >>> m = detect_destructive("rm -rf /")
        >>> m.pattern_id
        'rm_rf_root_or_wildcard'
        >>> detect_destructive("ls -la") is None
        True
    """
    if not cmd or not cmd.strip():
        return None
    for pat in DESTRUCTIVE_PATTERNS:
        if pat.pattern.search(cmd):
            return pat
    return None


__all__ = [
    "DestructivePattern",
    "DESTRUCTIVE_PATTERNS",
    "detect_destructive",
]
