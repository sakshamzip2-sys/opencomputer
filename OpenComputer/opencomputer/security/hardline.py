"""Hardline blocklist — non-bypassable refusal patterns.

Unlike :mod:`opencomputer.tools.bash_safety` (advisory; used by plan-mode
to inform the user), the patterns here are **enforcement**: if any tool
sees a match, the call is refused with no possibility of approval —
not via consent grant, not via ``--auto``, not via a config knob.

Mirrors the Hermes "Hardline Blocklist (Always-On — No Override)"
section. The list is intentionally small: only patterns whose intent
is unmistakably catastrophic. Anything ambiguous (``rm -r``,
``chmod 777``) belongs in the heuristic detector + consent gate, not
here.

The check fires BEFORE the consent gate so a tripped hardline never
produces a user-visible approval prompt.

Defence-in-depth note: even when running inside a sandboxed container,
hardline still applies. Docker bind mounts (``-v host:container:rw``)
and persistent ``--workspace`` mode mean ``rm -rf /`` inside the
container can still erase host data. Cost of the regex check is
negligible; cost of skipping is unbounded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HardlinePattern:
    """One non-bypassable pattern.

    Attributes:
        pattern_id: Stable identifier for logs and audit trails. NEVER
            renamed once shipped — appears in user-facing refusal
            messages and may be referenced in escalation tickets.
        pattern: Compiled regex. Applied with :meth:`re.Pattern.search`
            against the raw command string.
        reason: One-line refusal message. Should describe the risk in
            plain terms a sysadmin would recognise.
    """

    pattern_id: str
    pattern: re.Pattern[str]
    reason: str


# Statement-start anchor: matches beginning-of-string, shell statement
# separators, OR quote characters. The quote chars are included so the
# ExecuteCode source-scan path catches patterns embedded as string
# literals (e.g., ``subprocess.run("rm -rf /", shell=True)``); the
# bash-tool path doesn't typically see quoted forms but the union is
# still correct (``" rm -rf /``  doesn't appear in valid shell).
_STMT_START = r"""(?:^|[\s;|&`("'])"""


HARDLINE_PATTERNS: list[HardlinePattern] = [
    # rm -rf / — the canonical hardline. Match `rm -rf /`, `rm -rf /*`,
    # `rm -fr /`, `rm -Rf /`, etc. The flag block must contain both r/R
    # and f, and the target must be exactly `/` or `/*` or `/<token>`.
    HardlinePattern(
        pattern_id="rm_rf_root",
        pattern=re.compile(
            rf"{_STMT_START}rm\s+"
            r"(?:-[a-zA-Z]*[rR][a-zA-Z]*[fF][a-zA-Z]*"
            r"|-[a-zA-Z]*[fF][a-zA-Z]*[rR][a-zA-Z]*)\s+"
            r"""/(?:\s|$|\*|"|')"""
        ),
        reason="`rm -rf /` would wipe the filesystem root",
    ),
    # Explicit no-preserve-root variant. GNU coreutils added
    # --preserve-root as default in 2003; users who pass
    # --no-preserve-root are deliberately overriding the safety net.
    HardlinePattern(
        pattern_id="rm_rf_no_preserve_root",
        pattern=re.compile(
            rf"{_STMT_START}rm\s+.*--no-preserve-root.*\s/"
        ),
        reason="`rm --no-preserve-root /` deliberately bypasses GNU's filesystem-root guard",
    ),
    # Bash fork bomb — same regex shape as bash_safety. Documented
    # cross-reference: keep these two regexes in sync if the canonical
    # form changes.
    HardlinePattern(
        pattern_id="fork_bomb",
        pattern=re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:"),
        reason="bash fork bomb — exhausts process table and locks up the system",
    ),
    # mkfs against a real block device. Match mkfs / mkfs.<fs> followed
    # by /dev/sd*|nvme*|hd*|xvd*|vd*. Loop devices and ramdisks (loop*,
    # ram*) NOT matched — those are common in CI and sandboxes and not
    # catastrophic on host.
    HardlinePattern(
        pattern_id="mkfs_root_device",
        pattern=re.compile(
            rf"{_STMT_START}mkfs(?:\.\w+)?\s+"
            r"(?:/dev/(?:sd[a-z]|nvme\d+n\d+|hd[a-z]|xvd[a-z]|vd[a-z]))"
        ),
        reason="`mkfs` against a physical block device — formats the live disk",
    ),
    # dd ... of=/dev/sd* — write raw bytes to a real disk. Match any
    # source against a /dev/sd*|nvme*|hd*|xvd*|vd* destination.
    # Source-side `if=/dev/zero` not required since
    # `dd if=anything of=/dev/sda` is equally destructive.
    HardlinePattern(
        pattern_id="dd_zero_to_disk",
        pattern=re.compile(
            rf"{_STMT_START}dd\s+(?:[^|;&]*\s+)?"
            r"of=/dev/(?:sd[a-z]|nvme\d+n\d+|hd[a-z]|xvd[a-z]|vd[a-z])"
        ),
        reason="`dd of=/dev/sd*` — destroys disk contents",
    ),
    # `curl URL | sh` / `wget URL | sh` — pipe untrusted bytes to a
    # shell at top level. Any flags before the shell are allowed.
    # Excludes `| sh -n` (syntax check); requires literal end of word.
    HardlinePattern(
        pattern_id="curl_pipe_sh",
        pattern=re.compile(
            rf"{_STMT_START}(?:curl|wget)\s+[^|]*\|\s*(?:sh|bash|zsh)(?:\s|$)"
        ),
        reason="piping untrusted URL contents directly to a shell — RCE attack vector",
    ),
]


def check_command(cmd: str) -> HardlinePattern | None:
    """Return the matching pattern (refusal trigger) or ``None``.

    Empty / whitespace-only commands return ``None`` (no command, no
    risk). First-match-wins — patterns are ordered most-specific to
    most-general so the refusal message is precise.

    This function is the SOLE source of hardline-policy decisions.
    Callers must propagate the returned pattern's ``reason`` to the
    user verbatim.

    Args:
        cmd: Raw shell command string.

    Returns:
        First matching :class:`HardlinePattern`, or ``None`` if no
        hardline pattern fires.

    Example:
        >>> check_command("rm -rf /").pattern_id
        'rm_rf_root'
        >>> check_command("ls -la") is None
        True
    """
    if not cmd or not cmd.strip():
        return None
    for pat in HARDLINE_PATTERNS:
        if pat.pattern.search(cmd):
            return pat
    return None


__all__ = ["HardlinePattern", "HARDLINE_PATTERNS", "check_command"]
