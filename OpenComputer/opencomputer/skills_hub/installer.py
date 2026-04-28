"""Skills Hub installer — fetch, scan, validate, write, lockfile, audit.

Per DECISIONS D-0.3, this uses the REAL Skills Guard API:

- ``scan_skill(skill_path: Path, source: str) -> ScanResult`` (operates on a
  directory)
- ``should_allow_install(result, force=False) -> tuple[bool | None, str]``
  with three-way decision (True=allow, False=block, None=ask)

Flow:
1. Fetch bundle from router
2. Validate SKILL.md frontmatter (agentskills.io)
3. Write to staging dir under ``<hub>/_staging/<source>/<name>/``
4. Run ``scan_skill`` against staging dir
5. Run ``should_allow_install`` for the policy decision
6. On allow: atomic-move staging → final, update lockfile, audit
7. On block/ask-rejected: rmtree staging, audit ``scan_blocked``, raise
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from opencomputer.skills_hub.agentskills_validator import (
    ValidationError,
    validate_frontmatter,
)
from opencomputer.skills_hub.audit_log import AuditLog
from opencomputer.skills_hub.lockfile import HubLockFile
from opencomputer.skills_hub.router import SkillSourceRouter

# Identifier safety — prevent path traversal via crafted ids.
# Allowed: <source>/<name> or <source>/<name>/<subpath> with [\w.-] segments.
_IDENTIFIER_RE = re.compile(r"^[\w.-]+(?:/[\w.-]+)+$")


class InstallError(RuntimeError):
    """Raised when an install or uninstall fails."""


@dataclass(frozen=True, slots=True)
class InstallResult:
    identifier: str
    install_path: Path
    sha256: str


class Installer:
    """Installs skills from a SkillSourceRouter into <hub_root>/<source>/<name>/.

    ``skills_guard`` is a duck-typed object exposing ``scan_skill(path, source)``
    returning a ``ScanResult``-shaped object, and ``should_allow_install(result)``
    returning ``(decision: bool | None, reason: str)``. Tests can pass a stub.
    Production passes the ``opencomputer.skills_guard`` module directly.
    """

    def __init__(
        self,
        router: SkillSourceRouter,
        skills_guard,  # noqa: ANN001 — duck-typed module-or-stub
        hub_root: Path,
    ) -> None:
        self._router = router
        self._guard = skills_guard
        self._hub_root = Path(hub_root)
        self._staging = self._hub_root / "_staging"
        self._lockfile = HubLockFile(self._hub_root / "lockfile.json")
        self._audit = AuditLog(self._hub_root / "audit.log")

    @staticmethod
    def _validate_identifier(identifier: str) -> None:
        if not _IDENTIFIER_RE.match(identifier):
            raise InstallError(
                f"invalid identifier {identifier!r} — must be <source>/<name> "
                "with safe characters only"
            )
        # Reject any '..' segment to prevent path traversal even if regex allows
        if any(seg == ".." for seg in identifier.split("/")):
            raise InstallError(
                f"invalid identifier {identifier!r} — '..' segment not allowed"
            )

    def install(self, identifier: str, force: bool = False) -> InstallResult:
        self._validate_identifier(identifier)

        meta = self._router.inspect(identifier)
        bundle = self._router.fetch(identifier)
        if meta is None or bundle is None:
            raise InstallError(f"skill not found: {identifier}")

        try:
            validate_frontmatter(bundle.skill_md)
        except ValidationError as e:
            raise InstallError(f"invalid frontmatter for {identifier}: {e}") from e

        # Write to staging first so the scanner can run against a real
        # directory (matches the actual Skills Guard API).
        source_name, _, name = identifier.partition("/")
        staging_dir = self._staging / source_name / name
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)
        skill_md_path = staging_dir / "SKILL.md"
        skill_md_path.write_text(bundle.skill_md)
        for rel_path, content in bundle.files.items():
            target = staging_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        # Scan against staging dir
        try:
            result = self._guard.scan_skill(staging_dir, source=meta.source)
        except Exception as e:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise InstallError(f"skills_guard scan failed: {e}") from e

        decision, reason = self._guard.should_allow_install(result, force=force)
        if decision is False or decision is None:
            self._audit.record(
                action="scan_blocked",
                identifier=identifier,
                source=meta.source,
                verdict=getattr(result, "verdict", "unknown"),
                trust_level=getattr(result, "trust_level", "unknown"),
                decision="block" if decision is False else "ask",
                decision_reason=reason,
                findings_count=len(getattr(result, "findings", []) or []),
            )
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise InstallError(f"skills_guard blocked install of {identifier}: {reason}")

        # Allowed — atomic-move staging → final
        final_dir = self._hub_root / source_name / name
        if final_dir.exists():
            shutil.rmtree(final_dir)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging_dir), str(final_dir))

        # Cleanup empty staging hierarchy
        try:
            staging_source_dir = self._staging / source_name
            if staging_source_dir.exists() and not any(staging_source_dir.iterdir()):
                staging_source_dir.rmdir()
            if self._staging.exists() and not any(self._staging.iterdir()):
                self._staging.rmdir()
        except OSError:
            pass

        sha = hashlib.sha256(bundle.skill_md.encode("utf-8")).hexdigest()
        rel_install = f"{source_name}/{name}"
        self._lockfile.record_install(
            identifier=identifier,
            version=meta.version or "0.0.0",
            source=meta.source,
            install_path=rel_install,
            sha256=sha,
        )
        self._audit.record(
            action="install",
            identifier=identifier,
            source=meta.source,
            version=meta.version or "0.0.0",
            sha256=sha,
            verdict=getattr(result, "verdict", "unknown"),
            trust_level=getattr(result, "trust_level", "unknown"),
            decision_reason=reason,
        )
        return InstallResult(identifier=identifier, install_path=final_dir, sha256=sha)

    def uninstall(self, identifier: str) -> None:
        self._validate_identifier(identifier)
        entry = self._lockfile.get(identifier)
        if entry is None:
            raise InstallError(f"not installed: {identifier}")
        skill_dir = self._hub_root / entry.install_path
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        self._lockfile.record_uninstall(identifier)
        self._audit.record(
            action="uninstall",
            identifier=identifier,
            source=entry.source,
        )
