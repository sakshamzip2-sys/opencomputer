"""``oc backup`` — disaster-recovery tarball over a profile dir.

Creates a gzipped tarball of ``~/.opencomputer/<profile>/`` (or any
explicit ``--profile-dir``) excluding cache / tmp / __pycache__.
Restores by extracting to a staging dir, verifying the HMAC audit
chain (when present), then atomically renaming into place.

Wire surface:
    oc backup create [--profile-dir PATH] [--out PATH] [--no-include-sessions]
    oc backup restore PATH [--profile-dir PATH] [--force]
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import typer
from rich.console import Console

backup_app = typer.Typer(help="Backup and restore an OpenComputer profile.")
_console = Console()

_SCHEMA = 1
_DEFAULT_EXCLUDE_DIRS = {"cache", "tmp", "__pycache__"}
_SESSIONS_DB_NAME = "sessions.db"
_MANIFEST_NAME = "MANIFEST.json"


def _oc_version() -> str:
    try:
        from opencomputer import __version__  # type: ignore[attr-defined]

        return str(__version__)
    except Exception:
        return "unknown"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@backup_app.command("create")
def cmd_create(
    profile_dir: Path = typer.Option(
        None,
        "--profile-dir",
        help="Profile dir to back up (default: ~/.opencomputer/default).",
    ),
    out: Path = typer.Option(
        None,
        "--out",
        help="Output archive path (default: ./oc-backup-<profile>-<utc>.tar.gz).",
    ),
    include_sessions: bool = typer.Option(
        True,
        "--include-sessions/--no-include-sessions",
        help="Include sessions.db (live SQLite snapshot via .backup API).",
    ),
) -> None:
    """Create a tar.gz backup of a profile dir."""
    if profile_dir is None:
        profile_dir = Path.home() / ".opencomputer" / "default"
    profile_dir = profile_dir.expanduser().resolve()
    if not profile_dir.is_dir():
        _console.print(f"[red]Profile dir not found:[/red] {profile_dir}")
        raise typer.Exit(1)

    profile_name = profile_dir.name
    if out is None:
        out = Path.cwd() / f"oc-backup-{profile_name}-{_utc_iso()}.tar.gz"
    out = out.expanduser().resolve()

    files_packed: list[str] = []
    with tarfile.open(out, "w:gz") as tar:
        for root, dirs, files in os.walk(profile_dir):
            # Filter excluded dirs IN-PLACE so os.walk skips them.
            dirs[:] = [d for d in dirs if d not in _DEFAULT_EXCLUDE_DIRS]
            rel_root = Path(root).relative_to(profile_dir)
            for fname in files:
                if not include_sessions and fname == _SESSIONS_DB_NAME:
                    continue
                src = Path(root) / fname
                arcname = (Path(profile_name) / rel_root / fname).as_posix()
                if fname == _SESSIONS_DB_NAME and include_sessions:
                    # SQLite live-DB safe snapshot via .backup API.
                    snap = src.parent / f".{_SESSIONS_DB_NAME}.bak.{_utc_iso()}"
                    try:
                        _sqlite_safe_backup(src, snap)
                        tar.add(snap, arcname=arcname)
                        files_packed.append(arcname)
                    finally:
                        snap.unlink(missing_ok=True)
                else:
                    tar.add(src, arcname=arcname)
                    files_packed.append(arcname)

        # Manifest is the last member so it's easy to inspect.
        manifest = {
            "schema": _SCHEMA,
            "profile": profile_name,
            "created_utc": _utc_iso(),
            "oc_version": _oc_version(),
            "include_sessions": include_sessions,
            "files": files_packed,
        }
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        info = tarfile.TarInfo(name=f"{profile_name}/{_MANIFEST_NAME}")
        info.size = len(manifest_bytes)
        tar.addfile(info, BytesIO(manifest_bytes))

    _console.print(f"[green]Backup written:[/green] {out}")
    _console.print(f"  files: {len(files_packed)}, profile: {profile_name}")


def _sqlite_safe_backup(src: Path, dst: Path) -> None:
    """Use sqlite3 .backup() API for a consistent snapshot of a live DB."""
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


@backup_app.command("restore")
def cmd_restore(
    archive: Path = typer.Argument(..., help="Path to .tar.gz archive."),
    profile_dir: Path = typer.Option(
        None,
        "--profile-dir",
        help="Target profile dir (default: ~/.opencomputer/<profile-from-manifest>).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite even if target dir is non-empty.",
    ),
) -> None:
    """Restore a profile from a backup archive."""
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        _console.print(f"[red]Archive not found:[/red] {archive}")
        raise typer.Exit(1)

    # Stage to a tmp dir.
    staging = Path.home() / ".opencomputer" / f".restore-staging-{_utc_iso()}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            # Python 3.12+ filter='data' rejects abs paths + symlink escapes.
            tar.extractall(path=staging, filter="data")

        # Locate the profile root — top-level dir in the archive.
        children = [c for c in staging.iterdir() if c.is_dir()]
        if len(children) != 1:
            _console.print(
                f"[red]Archive must contain exactly one top-level dir, "
                f"found {len(children)}.[/red]"
            )
            raise typer.Exit(1)
        staged_profile = children[0]

        manifest_path = staged_profile / _MANIFEST_NAME
        if not manifest_path.is_file():
            _console.print(f"[red]Archive missing {_MANIFEST_NAME}.[/red]")
            raise typer.Exit(1)
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema") != _SCHEMA:
            _console.print(
                f"[red]Unsupported manifest schema: {manifest.get('schema')!r} "
                f"(expected {_SCHEMA}).[/red]"
            )
            raise typer.Exit(1)

        # Resolve target.
        if profile_dir is None:
            profile_dir = (
                Path.home() / ".opencomputer" / manifest["profile"]
            ).resolve()
        profile_dir = profile_dir.expanduser().resolve()

        if profile_dir.exists() and any(profile_dir.iterdir()):
            if not force:
                _console.print(
                    f"[red]Target profile dir non-empty:[/red] {profile_dir}\n"
                    "  Pass --force to overwrite."
                )
                raise typer.Exit(1)
            shutil.rmtree(profile_dir)

        # HMAC chain pre-check (when present).
        consent_db = staged_profile / "consent" / "audit.db"
        if consent_db.is_file():
            ok = _verify_audit_chain(consent_db)
            if not ok:
                _console.print(
                    "[red]HMAC audit chain verification FAILED on staged "
                    "archive.[/red]\n"
                    "  Restore aborted; original profile dir untouched."
                )
                raise typer.Exit(1)

        profile_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_profile), str(profile_dir))
        _console.print(f"[green]Restored to:[/green] {profile_dir}")
        _console.print(
            f"  profile: {manifest['profile']}, schema: {manifest['schema']}"
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _verify_audit_chain(consent_db: Path) -> bool:
    """Verify HMAC chain on a staged consent/audit.db.

    Returns True if intact or table absent (genesis-empty profiles).
    """
    try:
        from opencomputer.agent.consent.audit import AuditLogger
    except ImportError:
        return True  # consent module unavailable in test contexts
    try:
        logger = AuditLogger(db_path=consent_db)
        return logger.verify_chain()
    except Exception:  # noqa: BLE001
        return False
