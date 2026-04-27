"""Static-analysis scanner for SKILL.md and skill directories.

Three checks per scan:

1. **Pattern matching** — every line of every text file run through
   :data:`opencomputer.skills_guard.threat_patterns.THREAT_PATTERNS`.
2. **Invisible-unicode detection** — bidi / zero-width chars used to hide
   instructions inside otherwise-innocent prose.
3. **Structural sanity** — file count, total size, suspicious binary
   extensions, symlinks pointing outside the skill dir, executable bit
   on non-script files.

Findings are returned as a list of :class:`Finding` records (severity +
category + file + line + match excerpt). The overall ``verdict`` rolls
up to ``safe`` / ``caution`` / ``dangerous`` based on the worst severity
found:

- any ``critical`` → ``dangerous``
- any ``high`` (no ``critical``) → ``caution``
- ``medium`` / ``low`` only → ``caution``  (still flagged so users see
  the report, but with the lower-severity tier)
- empty findings → ``safe``
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .threat_patterns import (
    INVISIBLE_CHARS,
    MAX_FILE_COUNT,
    MAX_SINGLE_FILE_KB,
    MAX_TOTAL_SIZE_KB,
    SCANNABLE_EXTENSIONS,
    SUSPICIOUS_BINARY_EXTENSIONS,
    THREAT_PATTERNS,
)

# Pre-compile every pattern once at module import. Matching ~120 patterns
# per line × every line of every file × N files makes per-call compile
# noticeable on real corpora.
_COMPILED_PATTERNS: list[tuple[re.Pattern[str], str, str, str, str]] = [
    (re.compile(p, re.IGNORECASE), pid, sev, cat, desc)
    for (p, pid, sev, cat, desc) in THREAT_PATTERNS
]


@dataclass
class Finding:
    """One match — a single pattern hit on a single line, or a structural anomaly."""

    pattern_id: str
    severity: str  # "critical" | "high" | "medium" | "low"
    category: str
    file: str
    line: int
    match: str
    description: str


@dataclass
class ScanResult:
    """Aggregate scan output for one skill (file or directory)."""

    skill_name: str
    source: str
    trust_level: str
    verdict: str  # "safe" | "caution" | "dangerous"
    findings: list[Finding] = field(default_factory=list)
    scanned_at: str = ""
    summary: str = ""


# ──────────────────────────────────────────────────────────────────────
# Per-file scan
# ──────────────────────────────────────────────────────────────────────


def scan_file(file_path: Path, rel_path: str = "") -> list[Finding]:
    """Scan one file for threat patterns + invisible-unicode characters.

    Args:
        file_path: Absolute path to the file.
        rel_path: Display-only relative path (defaults to ``file_path.name``).

    Returns:
        List of :class:`Finding` records, one per ``(pattern_id, line)`` hit.
        Same pattern matching the same line twice is reported once.
    """
    if not rel_path:
        rel_path = file_path.name

    # Always scan SKILL.md regardless of suffix.
    if (
        file_path.suffix.lower() not in SCANNABLE_EXTENSIONS
        and file_path.name != "SKILL.md"
    ):
        return []

    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    findings: list[Finding] = []
    lines = content.split("\n")
    seen: set[tuple[str, int]] = set()

    for pat, pid, severity, category, description in _COMPILED_PATTERNS:
        for i, line in enumerate(lines, start=1):
            if (pid, i) in seen:
                continue
            if pat.search(line):
                seen.add((pid, i))
                excerpt = line.strip()
                if len(excerpt) > 120:
                    excerpt = excerpt[:117] + "..."
                findings.append(
                    Finding(
                        pattern_id=pid,
                        severity=severity,
                        category=category,
                        file=rel_path,
                        line=i,
                        match=excerpt,
                        description=description,
                    )
                )

    # Invisible-unicode pass — one finding per line max (avoid spamming
    # every char on a long bidi-attack line).
    for i, line in enumerate(lines, start=1):
        for char, name in INVISIBLE_CHARS.items():
            if char in line:
                findings.append(
                    Finding(
                        pattern_id="invisible_unicode",
                        severity="high",
                        category="injection",
                        file=rel_path,
                        line=i,
                        match=f"U+{ord(char):04X} ({name})",
                        description=(
                            f"invisible unicode character {name} "
                            "(possible text hiding/injection)"
                        ),
                    )
                )
                break

    return findings


# ──────────────────────────────────────────────────────────────────────
# Per-skill scan
# ──────────────────────────────────────────────────────────────────────


def scan_skill(skill_path: Path, source: str = "community") -> ScanResult:
    """Scan a skill (file or directory) and produce a verdict.

    Args:
        skill_path: Either a SKILL.md file or the directory containing one.
        source: Origin marker; resolved through
            :func:`opencomputer.skills_guard.policy.resolve_trust_level`
            to determine which install policy applies.

    Returns:
        :class:`ScanResult` with verdict, findings, and metadata.
    """
    # Local import to keep policy ↔ scanner decoupled at module level
    # (policy depends on scanner.Finding/ScanResult; scanner only needs
    # one helper from policy).
    from .policy import resolve_trust_level

    skill_name = skill_path.name
    trust_level = resolve_trust_level(source)
    findings: list[Finding] = []

    if skill_path.is_dir():
        findings.extend(_check_structure(skill_path))
        for f in skill_path.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(skill_path))
                findings.extend(scan_file(f, rel))
    elif skill_path.is_file():
        findings.extend(scan_file(skill_path, skill_path.name))

    verdict = _determine_verdict(findings)
    summary = _build_summary(skill_name, verdict, findings)

    return ScanResult(
        skill_name=skill_name,
        source=source,
        trust_level=trust_level,
        verdict=verdict,
        findings=findings,
        scanned_at=datetime.now(UTC).isoformat(),
        summary=summary,
    )


# ──────────────────────────────────────────────────────────────────────
# Structural checks
# ──────────────────────────────────────────────────────────────────────


def _check_structure(skill_dir: Path) -> list[Finding]:
    """File-count / total-size / binary / symlink-escape checks."""
    findings: list[Finding] = []
    file_count = 0
    total_size = 0

    for f in skill_dir.rglob("*"):
        if not f.is_file() and not f.is_symlink():
            continue

        rel = str(f.relative_to(skill_dir))
        file_count += 1

        if f.is_symlink():
            try:
                resolved = f.resolve()
                if not resolved.is_relative_to(skill_dir.resolve()):
                    findings.append(
                        Finding(
                            pattern_id="symlink_escape",
                            severity="critical",
                            category="traversal",
                            file=rel,
                            line=0,
                            match=f"symlink -> {resolved}",
                            description="symlink points outside the skill directory",
                        )
                    )
            except OSError:
                findings.append(
                    Finding(
                        pattern_id="broken_symlink",
                        severity="medium",
                        category="traversal",
                        file=rel,
                        line=0,
                        match="broken symlink",
                        description="broken or circular symlink",
                    )
                )
            continue

        try:
            size = f.stat().st_size
            total_size += size
        except OSError:
            continue

        if size > MAX_SINGLE_FILE_KB * 1024:
            findings.append(
                Finding(
                    pattern_id="oversized_file",
                    severity="medium",
                    category="structural",
                    file=rel,
                    line=0,
                    match=f"{size // 1024}KB",
                    description=(
                        f"file is {size // 1024}KB "
                        f"(limit: {MAX_SINGLE_FILE_KB}KB)"
                    ),
                )
            )

        ext = f.suffix.lower()
        if ext in SUSPICIOUS_BINARY_EXTENSIONS:
            findings.append(
                Finding(
                    pattern_id="binary_file",
                    severity="critical",
                    category="structural",
                    file=rel,
                    line=0,
                    match=f"binary: {ext}",
                    description=(
                        f"binary/executable file ({ext}) should not be "
                        "in a skill"
                    ),
                )
            )

        # Executable bit on non-script files: catches "secretly an ELF"
        # tricks and accidentally chmod +x'd README files.
        if (
            ext not in (".sh", ".bash", ".py", ".rb", ".pl")
            and (f.stat().st_mode & 0o111)
        ):
            findings.append(
                Finding(
                    pattern_id="unexpected_executable",
                    severity="medium",
                    category="structural",
                    file=rel,
                    line=0,
                    match="executable bit set",
                    description=(
                        "file has executable permission but is not a "
                        "recognized script type"
                    ),
                )
            )

    if file_count > MAX_FILE_COUNT:
        findings.append(
            Finding(
                pattern_id="too_many_files",
                severity="medium",
                category="structural",
                file="(directory)",
                line=0,
                match=f"{file_count} files",
                description=(
                    f"skill has {file_count} files (limit: {MAX_FILE_COUNT})"
                ),
            )
        )

    if total_size > MAX_TOTAL_SIZE_KB * 1024:
        findings.append(
            Finding(
                pattern_id="oversized_skill",
                severity="high",
                category="structural",
                file="(directory)",
                line=0,
                match=f"{total_size // 1024}KB total",
                description=(
                    f"skill is {total_size // 1024}KB total "
                    f"(limit: {MAX_TOTAL_SIZE_KB}KB)"
                ),
            )
        )

    return findings


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _determine_verdict(findings: list[Finding]) -> str:
    if not findings:
        return "safe"
    if any(f.severity == "critical" for f in findings):
        return "dangerous"
    return "caution"


def _build_summary(name: str, verdict: str, findings: list[Finding]) -> str:
    if not findings:
        return f"{name}: clean scan, no threats detected"
    cats = sorted({f.category for f in findings})
    return f"{name}: {verdict} — {len(findings)} finding(s) in {', '.join(cats)}"


def content_hash(skill_path: Path) -> str:
    """SHA-256 over all file contents (sorted by path) — stable id for caching."""
    h = hashlib.sha256()
    if skill_path.is_dir():
        for f in sorted(skill_path.rglob("*")):
            if f.is_file():
                try:
                    h.update(f.read_bytes())
                except OSError:
                    continue
    elif skill_path.is_file():
        h.update(skill_path.read_bytes())
    return f"sha256:{h.hexdigest()[:16]}"


__all__ = [
    "Finding",
    "ScanResult",
    "content_hash",
    "scan_file",
    "scan_skill",
]
