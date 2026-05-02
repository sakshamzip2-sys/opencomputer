"""Prior-install detection + import section (M1).

Modeled after Hermes's claw.py migration logic — independently
re-implemented (no code copied).

Detects two prior-install sources:
  - ~/.openclaw/  → OpenClaw (the project Hermes forked from)
  - ~/.hermes/    → Hermes Agent

If found, offers a non-destructive import:
  - MEMORY.md, USER.md, SOUL.md → copied to ~/.opencomputer/<filename>.
    If the destination already exists, the import lands at
    <filename>.imported instead so the user's existing data is never
    overwritten.
  - skills/ → merged into ~/.opencomputer/skills/ (per-skill, no
    overwrite). Existing skill names with the same path are kept.

Records each successful migration in config.migrations.prior_install
so re-runs see the work as done.

Deeper migration of sessions.db, plugin configs, etc. is deferred to
M1.b — schemas differ between projects and merging requires care.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from opencomputer.cli_setup.sections import SectionResult, WizardCtx
from opencomputer.cli_ui.menu import Choice, radiolist

_FILES_TO_IMPORT = ("MEMORY.md", "USER.md", "SOUL.md")
_DIR_TO_MERGE = "skills"


def _oc_home() -> Path:
    """Return the active profile / OC home directory."""
    home = os.environ.get("OPENCOMPUTER_HOME")
    if home:
        return Path(home)
    return Path.home() / ".opencomputer"


def _detect_prior_installs() -> list[dict]:
    """Walk known prior-install paths under ``~`` and return entries
    with at least one importable file or skills/ subtree."""
    candidates = [
        ("OpenClaw", Path.home() / ".openclaw"),
        ("Hermes", Path.home() / ".hermes"),
    ]
    found: list[dict] = []
    for name, path in candidates:
        if not path.exists() or not path.is_dir():
            continue
        # Treat as a "real" prior install if any importable artifact is present.
        has_file = any((path / f).exists() for f in _FILES_TO_IMPORT)
        has_skills = (path / _DIR_TO_MERGE).exists()
        if has_file or has_skills:
            found.append({"name": name, "path": path})
    return found


def _print_import_preview(sources: list[dict], dest: Path) -> None:
    """Polish: dry-run preview of what would be imported. No file writes.

    For each source: count importable files (MEMORY/USER/SOUL.md) and
    skills, flag any that would land at <name>.imported because the
    destination already exists.
    """
    print("\n  Preview — what would be imported:")
    for src in sources:
        src_path: Path = src["path"]
        files_fresh: list[str] = []
        files_renamed: list[str] = []
        for fname in _FILES_TO_IMPORT:
            if not (src_path / fname).exists():
                continue
            if (dest / fname).exists():
                files_renamed.append(fname)
            else:
                files_fresh.append(fname)
        skills_count = 0
        skills_skipped = 0
        src_skills = src_path / _DIR_TO_MERGE
        if src_skills.exists():
            for skill_md in src_skills.rglob("SKILL.md"):
                rel = skill_md.parent.relative_to(src_skills)
                if (dest / _DIR_TO_MERGE / rel).exists():
                    skills_skipped += 1
                else:
                    skills_count += 1
        print(f"    {src['name']} ({src_path}):")
        if files_fresh:
            print(f"      → fresh: {', '.join(files_fresh)}")
        if files_renamed:
            print(f"      → existing kept; new lands at: "
                  f"{', '.join(f'{f}.imported' for f in files_renamed)}")
        if skills_count:
            print(f"      → {skills_count} new skill(s) merged")
        if skills_skipped:
            print(f"      → {skills_skipped} skill(s) skipped (path already exists)")
        if not (files_fresh or files_renamed or skills_count or skills_skipped):
            print("      → nothing to import")
    print()


def _import_one(source: dict, dest: Path) -> dict:
    """Copy MEMORY/USER/SOUL files (non-destructively) and merge skills/.
    Returns a dict suitable for config.migrations.prior_install."""
    src_path: Path = source["path"]
    dest.mkdir(parents=True, exist_ok=True)

    files_imported: list[str] = []
    files_renamed: list[str] = []
    for fname in _FILES_TO_IMPORT:
        src_file = src_path / fname
        if not src_file.exists():
            continue
        dest_file = dest / fname
        if dest_file.exists():
            # Non-destructive: write to <fname>.imported
            target = dest / f"{fname}.imported"
            shutil.copy2(src_file, target)
            files_renamed.append(fname)
        else:
            shutil.copy2(src_file, dest_file)
            files_imported.append(fname)

    skills_imported: list[str] = []
    src_skills = src_path / _DIR_TO_MERGE
    if src_skills.exists():
        dest_skills = dest / _DIR_TO_MERGE
        for skill_md in src_skills.rglob("SKILL.md"):
            rel = skill_md.parent.relative_to(src_skills)
            target_dir = dest_skills / rel
            if target_dir.exists():
                continue  # don't overwrite existing skills
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill_md.parent, target_dir)
            skills_imported.append(str(rel))

    return {
        "source": source["name"],
        "source_path": str(src_path),
        "files_imported": files_imported,
        "files_renamed_imported": files_renamed,
        "skills_imported": skills_imported,
    }


def run_prior_install_section(ctx: WizardCtx) -> SectionResult:
    found = _detect_prior_installs()
    if not found:
        return SectionResult.SKIPPED_FRESH

    names = ", ".join(f["name"] for f in found)
    paths = "\n    ".join(str(f["path"]) for f in found)
    print(f"  Found prior install(s): {names}")
    print(f"    {paths}")

    # Polish: dry-run preview phase. Show what WOULD be imported
    # before any file is touched (matches Hermes's
    # _offer_openclaw_migration preview pattern).
    _print_import_preview(found, _oc_home())

    choices = [
        Choice(
            f"Import from {names} (non-destructive)",
            "import",
            description="Copies MEMORY.md / USER.md / SOUL.md and merges "
                        "skills/ into ~/.opencomputer. Existing files preserved "
                        "as <name>.imported.",
        ),
        Choice("Skip — leave prior install untouched", "skip"),
    ]
    idx = radiolist(
        "Import data from your prior install?", choices, default=0,
    )
    if idx == 1:
        return SectionResult.SKIPPED_FRESH

    dest = _oc_home()
    migrations: list[dict] = []
    for source in found:
        report = _import_one(source, dest)
        migrations.append(report)
        n_f = len(report["files_imported"])
        n_r = len(report["files_renamed_imported"])
        n_s = len(report["skills_imported"])
        print(
            f"  ✓ {source['name']}: {n_f} files imported, "
            f"{n_r} renamed (.imported), {n_s} skills merged"
        )

    ctx.config.setdefault("migrations", {}).setdefault(
        "prior_install", []
    ).extend(migrations)

    return SectionResult.CONFIGURED
