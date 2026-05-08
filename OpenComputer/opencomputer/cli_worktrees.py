"""``oc worktrees`` Typer subapp — list, clean, include-preview.

These subcommands operate on the ``.opencomputer-worktrees/`` directory
under the cwd's git repo root. They never touch git's own worktree
machinery beyond invoking ``git worktree list/remove``.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.worktree import WORKTREES_DIR, repo_root
from opencomputer.worktree_include import apply_to_worktree

worktrees_app = typer.Typer(
    name="worktrees",
    help="Manage `.opencomputer-worktrees/` (the per-session git-worktree directory).",
    no_args_is_help=True,
)
console = Console()


def _worktrees_root(cwd: Path) -> Path | None:
    rr = repo_root(cwd)
    if rr is None:
        return None
    return rr / WORKTREES_DIR


def _list_git_worktrees(repo: Path) -> dict[str, dict[str, str]]:
    """Return ``{path: {branch, head, ...}}`` from ``git worktree list --porcelain``."""
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    result: dict[str, dict[str, str]] = {}
    if out.returncode != 0:
        return result
    cur: dict[str, str] = {}
    for line in out.stdout.splitlines():
        if not line:
            if cur.get("worktree"):
                result[cur["worktree"]] = cur
            cur = {}
            continue
        if " " in line:
            k, v = line.split(" ", 1)
        else:
            k, v = line, ""
        cur[k] = v
    if cur.get("worktree"):
        result[cur["worktree"]] = cur
    return result


@worktrees_app.command("list")
def worktrees_list_cmd() -> None:
    """List all ``.opencomputer-worktrees/<id>/`` entries for the cwd's repo."""
    cwd = Path.cwd()
    wts_root = _worktrees_root(cwd)
    if wts_root is None or not wts_root.exists():
        console.print("[dim]no oc worktrees in this repo (or not a git repo).[/dim]")
        return

    rr = repo_root(cwd)
    assert rr is not None
    git_wts = _list_git_worktrees(rr)

    rows = []
    for sub in sorted(wts_root.iterdir()):
        if not sub.is_dir():
            continue
        info = git_wts.get(str(sub.resolve()))
        branch = (info or {}).get("branch", "[unregistered]")
        rows.append((sub.name, branch, str(sub)))

    if not rows:
        console.print("[dim]no oc worktrees in this repo.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("session_id", style="cyan")
    table.add_column("branch")
    table.add_column("path", overflow="fold")
    for sid, branch, path in rows:
        table.add_row(sid, branch, path)
    console.print(table)


@worktrees_app.command("clean")
def worktrees_clean_cmd(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print what would be removed; do not delete."),
    ] = False,
    all_: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Remove ALL .opencomputer-worktrees/* (use with care).",
        ),
    ] = False,
) -> None:
    """Remove stale ``.opencomputer-worktrees/*`` entries.

    "Stale" = present on disk but not registered with ``git worktree
    list``. ``--all`` removes every entry regardless of registration.
    """
    cwd = Path.cwd()
    wts_root = _worktrees_root(cwd)
    if wts_root is None or not wts_root.exists():
        console.print("[dim]no oc worktrees in this repo.[/dim]")
        return

    rr = repo_root(cwd)
    assert rr is not None
    git_wts = _list_git_worktrees(rr)

    targets: list[Path] = []
    for sub in sorted(wts_root.iterdir()):
        if not sub.is_dir():
            continue
        if all_:
            targets.append(sub)
            continue
        registered = str(sub.resolve()) in git_wts
        if not registered:
            targets.append(sub)

    if not targets:
        console.print("[green]nothing to clean.[/green]")
        return

    for t in targets:
        prefix = "[would remove]" if dry_run else "[remove]"
        console.print(f"{prefix} {t}")
        if dry_run:
            continue
        rc = subprocess.run(
            ["git", "worktree", "remove", "--force", str(t)],
            cwd=str(rr),
            capture_output=True,
            text=True,
        )
        if rc.returncode != 0:
            shutil.rmtree(t, ignore_errors=True)


@worktrees_app.command("include-preview")
def worktrees_include_preview_cmd(
    dir_: Annotated[
        Path | None,
        typer.Option(
            "--dir",
            help="Override the repo root (defaults to cwd's repo root).",
        ),
    ] = None,
) -> None:
    """Print what ``.worktreeinclude`` would copy into a fresh worktree.

    Reads project + global include files, expands patterns, and prints
    a per-source line plus aggregate bytes. No I/O — strictly preview.
    """
    cwd = dir_ or Path.cwd()
    rr = repo_root(cwd)
    if rr is None:
        console.print("[dim]not in a git repo.[/dim]")
        raise typer.Exit(2)

    fake_wt = rr / ".__worktree_include_preview__"
    from opencomputer.agent.config import default_config

    cfg = default_config()
    wcfg = cfg.worktree

    global_path: Path | None = None
    if wcfg.include_global_fallback:
        try:
            from opencomputer.profiles import get_default_root, read_active_profile

            active = read_active_profile()
            if active in (None, "default"):
                root = get_default_root()
            else:
                root = get_default_root() / "profiles" / active
            global_path = root / "worktreeinclude"
        except Exception:  # noqa: BLE001
            global_path = None

    report = apply_to_worktree(
        rr,
        fake_wt,
        dry_run=True,
        max_total_mb=wcfg.include_max_total_mb,
        max_per_file_mb=wcfg.include_max_per_file_mb,
        follow_symlinks=wcfg.include_follow_symlinks,
        global_fallback_path=global_path,
    )

    if not report.copied and not report.skipped:
        console.print("[dim]no .worktreeinclude patterns matched.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("source", overflow="fold")
    table.add_column("bytes", justify="right")
    for e in report.copied:
        table.add_row(str(e.src), f"{e.bytes_copied:,}")
    for src, reason in report.skipped:
        table.add_row(f"[dim]{src} (skip: {reason})[/dim]", "[dim]—[/dim]")
    console.print(table)
    console.print(
        f"\n[bold]total:[/bold] {report.total_bytes:,} bytes "
        f"({report.total_bytes / (1024 * 1024):.1f} MB)"
    )


__all__ = ["worktrees_app"]
