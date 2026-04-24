"""II.4 destructive-command heuristics for Bash.

Covers:
    * Unit tests over each pattern in ``opencomputer.tools.bash_safety`` (positive
      cases confirm each shape of destructive command is caught; negative cases
      confirm the allowlisted "looks-destructive-but-isn't" shapes are NOT caught).
    * Integration tests over the plan-mode pre-tool-use hook
      (``extensions/coding-harness/hooks/plan_block.py``) confirming the Bash
      ``command`` arg is scanned when plan_mode is active, and the scanner stays
      silent outside plan_mode (MVP scope — II.4 only gates in plan mode).

Mirrors Hermes's ``_is_destructive_command`` (``sources/hermes-agent/run_agent.py``
lines 240-264) but lifted into a typed, first-class module so plugins (and the
plan-mode hook) can reuse it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from opencomputer.tools.bash_safety import (
    DESTRUCTIVE_PATTERNS,
    DestructivePattern,
    detect_destructive,
)
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent
from plugin_sdk.runtime_context import RuntimeContext

# ─── Unit tests: positive cases — each pattern fires on its intended trigger ──


def test_rm_rf_root_fires() -> None:
    m = detect_destructive("rm -rf /")
    assert m is not None
    assert "rm" in m.reason.lower() or "recursive" in m.reason.lower()


def test_rm_rf_star_fires() -> None:
    assert detect_destructive("rm -rf /*") is not None


def test_rm_rf_home_fires() -> None:
    assert detect_destructive("rm -rf ~") is not None
    assert detect_destructive("rm -rf $HOME") is not None


def test_rm_rf_star_relative_fires() -> None:
    # rm -rf * in a shell drops the whole cwd tree — worth flagging.
    assert detect_destructive("rm -rf *") is not None


def test_sudo_rm_fires() -> None:
    m = detect_destructive("sudo rm -rf /etc")
    assert m is not None
    assert "sudo" in m.reason.lower() or "escalat" in m.reason.lower()


def test_sudo_dd_fires() -> None:
    m = detect_destructive("sudo dd if=/dev/zero of=/dev/sda")
    assert m is not None


def test_mv_escape_cwd_fires() -> None:
    # Destination climbs out of the working tree — potentially catastrophic.
    m = detect_destructive("mv ./file /tmp/elsewhere")
    assert m is not None
    assert "mv" in m.reason.lower() or "escape" in m.reason.lower() or "outside" in m.reason.lower()


def test_mv_to_absolute_path_fires() -> None:
    assert detect_destructive("mv data.csv /var/something") is not None


def test_git_reset_hard_fires() -> None:
    m = detect_destructive("git reset --hard HEAD~1")
    assert m is not None
    assert "git" in m.reason.lower() or "reset" in m.reason.lower()


def test_git_clean_fd_fires() -> None:
    assert detect_destructive("git clean -fd") is not None


def test_git_clean_fx_fires() -> None:
    assert detect_destructive("git clean -fx") is not None


def test_git_clean_ffdx_fires() -> None:
    # -ffdx combined single-flag style.
    assert detect_destructive("git clean -ffdx") is not None


def test_redirect_truncate_fires() -> None:
    # Single ">" overwrite truncates the target. Append (>>) is fine.
    m = detect_destructive("echo hi > important.txt")
    assert m is not None
    assert "redirect" in m.reason.lower() or "truncat" in m.reason.lower() or "overwrite" in m.reason.lower()


def test_redirect_to_config_fires() -> None:
    assert detect_destructive("cat something > /etc/hosts") is not None


def test_chmod_777_recursive_fires() -> None:
    m = detect_destructive("chmod -R 777 /")
    assert m is not None
    assert "chmod" in m.reason.lower() or "777" in m.reason


def test_dd_to_disk_fires() -> None:
    m = detect_destructive("dd if=/dev/zero of=/dev/sda bs=1M")
    assert m is not None
    assert "dd" in m.reason.lower()


def test_dd_to_disk2_fires() -> None:
    assert detect_destructive("dd if=backup.img of=/dev/disk2") is not None


def test_drop_database_fires() -> None:
    m = detect_destructive("mysql -e 'DROP DATABASE production'")
    assert m is not None
    assert "drop" in m.reason.lower() or "sql" in m.reason.lower()


def test_drop_database_lowercase_fires() -> None:
    # Case insensitivity is a hard requirement.
    assert detect_destructive("psql -c 'drop database users'") is not None


def test_drop_table_fires() -> None:
    assert detect_destructive("DROP TABLE users") is not None


def test_truncate_sql_fires() -> None:
    assert detect_destructive("TRUNCATE orders") is not None


def test_fork_bomb_fires() -> None:
    m = detect_destructive(":(){ :|:& };:")
    assert m is not None
    assert "fork" in m.reason.lower() or "bomb" in m.reason.lower()


# ─── Unit tests: negative cases — allowlisted "looks bad but isn't" commands ──


def test_git_rm_does_not_fire() -> None:
    # `git rm` is the git-tracked file-remove and is intentional.
    assert detect_destructive("git rm old_file.py") is None


def test_npm_uninstall_does_not_fire() -> None:
    assert detect_destructive("npm uninstall lodash") is None


def test_pip_uninstall_does_not_fire() -> None:
    # `uninstall` includes the string "install" — make sure no false-positive.
    assert detect_destructive("pip uninstall requests") is None


def test_rm_interactive_does_not_fire() -> None:
    # `rm -i` prompts before each deletion — not catastrophic.
    assert detect_destructive("rm -i file.txt") is None


def test_plain_rm_single_file_does_not_fire() -> None:
    # Removing one named file with no recursive flag is routine.
    assert detect_destructive("rm /tmp/foo.txt") is None


def test_trash_cli_does_not_fire() -> None:
    # trash-cli is the safe-delete alternative.
    assert detect_destructive("trash ~/Downloads/junk") is None


def test_rm_inside_rmdir_does_not_fire() -> None:
    # `rmdir` only removes empty dirs — low risk, and our rm-regex shouldn't fire on `rmdir`.
    assert detect_destructive("rmdir empty_dir") is None


def test_echo_append_does_not_fire() -> None:
    # `>>` is append, safe. Our > detection must not fire on >>.
    assert detect_destructive("echo hi >> log.txt") is None


def test_ls_does_not_fire() -> None:
    assert detect_destructive("ls -la") is None


def test_cat_file_does_not_fire() -> None:
    assert detect_destructive("cat file.txt") is None


def test_git_status_does_not_fire() -> None:
    assert detect_destructive("git status") is None


def test_git_commit_does_not_fire() -> None:
    assert detect_destructive("git commit -m 'done'") is None


def test_grep_does_not_fire() -> None:
    assert detect_destructive("grep -r 'pattern' src/") is None


def test_mv_within_cwd_does_not_fire() -> None:
    # Rename inside the working tree is fine.
    assert detect_destructive("mv old.py new.py") is None


def test_mv_to_subdir_does_not_fire() -> None:
    assert detect_destructive("mv data.csv ./archive/data.csv") is None


def test_dd_without_disk_target_does_not_fire() -> None:
    # dd to a regular file (image/backup) is routine; only /dev/* of= is risky.
    assert detect_destructive("dd if=source.img of=backup.img") is None


# ─── Metadata checks ──────────────────────────────────────────────────────────


def test_destructive_patterns_is_nonempty() -> None:
    assert len(DESTRUCTIVE_PATTERNS) > 0


def test_pattern_dataclass_shape() -> None:
    p = DESTRUCTIVE_PATTERNS[0]
    assert isinstance(p, DestructivePattern)
    assert p.pattern_id
    assert p.pattern
    assert p.reason


def test_detect_destructive_empty_input() -> None:
    assert detect_destructive("") is None


def test_detect_destructive_whitespace_only() -> None:
    assert detect_destructive("   \n\t  ") is None


# ─── Integration: plan-mode hook scans Bash command arg ──────────────────────


def _load_plan_block():
    """Load ``hooks/plan_block.py`` the same way the coding-harness tests do."""
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "extensions" / "coding-harness" / "hooks" / "plan_block.py"
    spec = importlib.util.spec_from_file_location("ch_test_plan_block_bash_safety", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ch_test_plan_block_bash_safety"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_plan_mode_hook_blocks_destructive_bash_with_specific_reason() -> None:
    mod = _load_plan_block()
    ctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=ToolCall(id="1", name="Bash", arguments={"command": "rm -rf /"}),
        runtime=RuntimeContext(plan_mode=True),
    )
    dec = asyncio.run(mod.plan_mode_block_hook(ctx))
    assert dec is not None
    assert dec.decision == "block"
    # Reason should mention the specific destructive pattern, not just "Bash refused".
    assert "destructive" in dec.reason.lower() or "rm" in dec.reason.lower()


def test_plan_mode_hook_blocks_sql_drop_in_bash() -> None:
    mod = _load_plan_block()
    ctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=ToolCall(
            id="1",
            name="Bash",
            arguments={"command": "psql -c 'DROP DATABASE prod'"},
        ),
        runtime=RuntimeContext(plan_mode=True),
    )
    dec = asyncio.run(mod.plan_mode_block_hook(ctx))
    assert dec is not None
    assert dec.decision == "block"


def test_plan_mode_hook_does_not_fire_outside_plan_mode() -> None:
    """MVP scope: II.4 only gates in plan mode. Outside, nothing fires."""
    mod = _load_plan_block()
    ctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=ToolCall(id="1", name="Bash", arguments={"command": "rm -rf /"}),
        runtime=RuntimeContext(plan_mode=False),
    )
    dec = asyncio.run(mod.plan_mode_block_hook(ctx))
    assert dec is None


def test_plan_mode_hook_benign_bash_still_blocked_by_name() -> None:
    """Bash is in DESTRUCTIVE_TOOLS by name — benign commands still refused in plan mode.

    The II.4 change layers a more specific reason on TOP of the existing name-based
    block; it does not weaken the existing contract.
    """
    mod = _load_plan_block()
    ctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=ToolCall(id="1", name="Bash", arguments={"command": "ls -la"}),
        runtime=RuntimeContext(plan_mode=True),
    )
    dec = asyncio.run(mod.plan_mode_block_hook(ctx))
    assert dec is not None
    assert dec.decision == "block"
    # Fallback reason (existing behavior) — Bash is destructive by name.
    assert "plan mode" in dec.reason.lower()


def test_plan_mode_hook_read_tools_still_pass_through() -> None:
    """Read/Grep/Glob are not in DESTRUCTIVE_TOOLS and don't get blocked in plan mode."""
    mod = _load_plan_block()
    ctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=ToolCall(id="1", name="Read", arguments={"file_path": "/etc/hosts"}),
        runtime=RuntimeContext(plan_mode=True),
    )
    dec = asyncio.run(mod.plan_mode_block_hook(ctx))
    assert dec is None


def test_plan_mode_hook_destructive_reason_preferred_over_generic() -> None:
    """When the Bash command itself matches a destructive pattern, the hook's reason
    string should mention the specific pattern — not just the generic "Bash refused".
    That gives the model (and the user) an actionable signal.
    """
    mod = _load_plan_block()
    ctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=ToolCall(
            id="1",
            name="Bash",
            arguments={"command": "git reset --hard HEAD~5"},
        ),
        runtime=RuntimeContext(plan_mode=True),
    )
    dec = asyncio.run(mod.plan_mode_block_hook(ctx))
    assert dec is not None
    assert dec.decision == "block"
    r = dec.reason.lower()
    # Must contain a specific hint, not just "Bash refused".
    assert "git" in r or "reset" in r or "destructive" in r
