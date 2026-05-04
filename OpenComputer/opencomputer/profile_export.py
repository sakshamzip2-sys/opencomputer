"""Profile export/import — Phase 14.H credential sharing.

Exports a profile directory to a portable tar.gz archive with default
redaction (`.env` values + likely-secret config.yaml fields). Import
extracts the archive into a target profile dir, refusing to overwrite
existing profiles unless ``force=True``.

Pure functions where possible — the actual filesystem walk lives behind
explicit ``Path`` parameters so tests use ``tmp_path``. The CLI wrapper
in :mod:`opencomputer.cli_profile` resolves the active profile + invokes
these functions.

Default redaction policy (when ``include_secrets=False``):
  - `.env` files: each ``KEY=value`` becomes ``KEY=<REDACTED:N>`` where N
    is the original length.
  - `config.yaml`: walks the loaded YAML; any value at a key whose name
    matches the secret-key heuristic (`*api_key*`, `*token*`, `*secret*`,
    `*password*`, case-insensitive) AND whose stringified value is
    longer than 8 chars is replaced with ``<REDACTED:N>``.
  - All other files (profile.yaml, MEMORY.md, USER.md, SOUL.md) are
    included verbatim.

Default exclusions (when ``include_sessions=False``):
  - ``sessions.db``, ``sessions.db-wal``, ``sessions.db-shm`` — large + private
  - ``logs/`` directory — runtime logs
  - ``llm_events.jsonl`` — telemetry
  - ``audit_log.jsonl`` — F1 audit trail (always excluded; never overridable)
  - ``mcp_oauth/`` — OAuth refresh/access tokens. Excluded by default
    because sharing them gives the receiver live API access without
    re-authenticating. Opt in via ``include_oauth_tokens=True`` (CLI
    ``--include-oauth-tokens``) when migrating a profile to a different
    machine YOU personally own. Bundled verbatim (no redaction) when
    enabled — redacting would defeat the purpose.

The exported archive contains a top-level ``manifest.json`` with the
profile name, OC version, export timestamp, and the redaction flags
applied. Import validates this manifest before extraction.
"""

from __future__ import annotations

import io
import json
import logging
import re
import tarfile
import time
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: Files always excluded from export (audit log never goes out).
_ALWAYS_EXCLUDED = frozenset(
    [
        "audit_log.jsonl",
    ]
)

#: Directories always excluded from export — never overridable.
_ALWAYS_EXCLUDED_DIRS = frozenset(
    [
        "logs",
        "__pycache__",
        ".cache",
    ]
)

#: Directory excluded by default but unlockable via the explicit
#: ``include_oauth_tokens=True`` opt-in. OAuth tokens (refresh + access
#: pairs from MCP servers) are too risky to ship out under normal
#: ``--include-secrets`` semantics — sharing them effectively gives the
#: receiver live API access without re-authenticating. Users who DO want
#: to migrate a profile to a new machine they own pass this second flag
#: explicitly.
_OAUTH_DIR = "mcp_oauth"

#: Files only included when ``include_sessions=True``.
_SESSION_FILES = frozenset(
    [
        "sessions.db",
        "sessions.db-wal",
        "sessions.db-shm",
        "llm_events.jsonl",
    ]
)

#: Heuristic: substrings (case-insensitive) of YAML key names whose values
#: should be redacted unless ``include_secrets=True``.
_SECRET_KEY_PATTERNS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
)

#: Minimum stringified-value length before redaction kicks in. Prevents
#: false positives on empty placeholders + very short flags.
_REDACT_MIN_LEN = 8


def _is_secret_key(key: str) -> bool:
    """True if the YAML key name suggests its value is a secret."""
    lower = key.lower()
    return any(p in lower for p in _SECRET_KEY_PATTERNS)


def _redact_env_text(text: str) -> str:
    """Redact every KEY=value line in a .env-style file.

    Preserves blank lines + ``#`` comments verbatim. Each non-empty
    KEY=value gets its value replaced with ``<REDACTED:N>``.
    """
    out_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            out_lines.append(raw)
            continue
        # Match "KEY=" optionally followed by anything
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if m is None:
            out_lines.append(raw)
            continue
        key, value = m.group(1), m.group(2)
        # Strip optional surrounding quotes for length count
        stripped = value.strip()
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ('"', "'"):
            stripped = stripped[1:-1]
        out_lines.append(f"{key}=<REDACTED:{len(stripped)}>")
    return "\n".join(out_lines) + ("\n" if text.endswith("\n") else "")


def _redact_yaml_data(data: Any) -> Any:
    """Walk a parsed YAML structure, redacting values at secret-key paths."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(k, str) and _is_secret_key(k) and isinstance(v, str):
                if len(v) >= _REDACT_MIN_LEN:
                    out[k] = f"<REDACTED:{len(v)}>"
                else:
                    out[k] = v  # empty placeholder, preserve
            else:
                out[k] = _redact_yaml_data(v)
        return out
    if isinstance(data, list):
        return [_redact_yaml_data(item) for item in data]
    return data


def _redact_yaml_text(text: str) -> str:
    """Round-trip parse → redact → dump a YAML document."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return text  # malformed — leave alone
    if data is None:
        return text
    redacted = _redact_yaml_data(data)
    return yaml.safe_dump(redacted, default_flow_style=False, sort_keys=False)


def _should_include_file(
    name: str,
    *,
    include_sessions: bool,
    include_oauth_tokens: bool = False,
) -> bool:
    """Filter for entries at the profile-dir top level.

    Returns False for always-excluded names (regardless of flags),
    session files when sessions aren't requested, and the OAuth-tokens
    directory unless ``include_oauth_tokens=True`` is passed.
    """
    if name in _ALWAYS_EXCLUDED_DIRS:
        return False
    if name == _OAUTH_DIR and not include_oauth_tokens:
        return False
    if not include_sessions and name in _SESSION_FILES:
        return False
    return name not in _ALWAYS_EXCLUDED


def _make_manifest(
    *,
    profile_name: str,
    oc_version: str,
    include_secrets: bool,
    include_sessions: bool,
    include_oauth_tokens: bool = False,
) -> dict[str, Any]:
    """Build the archive manifest.json content.

    ``include_oauth_tokens`` defaults to False — manifests written
    before the flag landed never had this key, so we keep the default
    matching the historical behavior to keep older readers happy.
    """
    return {
        "format_version": "1",
        "profile_name": profile_name,
        "oc_version": oc_version,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "include_secrets": include_secrets,
        "include_sessions": include_sessions,
        "include_oauth_tokens": include_oauth_tokens,
    }


def export_profile(
    profile_dir: Path,
    output_path: Path,
    *,
    profile_name: str = "default",
    oc_version: str = "0.0.0",
    include_secrets: bool = False,
    include_sessions: bool = False,
    include_oauth_tokens: bool = False,
) -> Path:
    """Export ``profile_dir`` to a tar.gz at ``output_path``.

    ``include_oauth_tokens=True`` opts in to bundling the ``mcp_oauth/``
    directory. OAuth tokens (refresh + access pairs from MCP servers)
    are excluded by default because sharing them gives the receiver
    live API access without re-authenticating. Users migrating a
    profile to a different machine they personally own may want them.

    When ``include_oauth_tokens=True``, OAuth files are bundled
    **verbatim** — the redaction heuristics that handle ``.env`` and
    ``config.yaml`` are NOT applied to the OAuth JSON. Redacting them
    would defeat the purpose of the flag (you cannot reuse a redacted
    refresh token), so this is by design. The ``include_secrets`` knob
    still governs ``.env`` / ``config.yaml`` redaction independently.

    Returns the output path on success.
    """
    if not profile_dir.exists() or not profile_dir.is_dir():
        raise FileNotFoundError(f"profile dir does not exist: {profile_dir}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = _make_manifest(
        profile_name=profile_name,
        oc_version=oc_version,
        include_secrets=include_secrets,
        include_sessions=include_sessions,
        include_oauth_tokens=include_oauth_tokens,
    )
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")

    with tarfile.open(output_path, "w:gz") as tar:
        # Add manifest first
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        info.mode = 0o644
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(manifest_bytes))

        for entry in sorted(profile_dir.iterdir()):
            if not _should_include_file(
                entry.name,
                include_sessions=include_sessions,
                include_oauth_tokens=include_oauth_tokens,
            ):
                continue
            arcname = f"profile/{entry.name}"
            if entry.is_dir():
                _add_dir_to_tar(
                    tar,
                    entry,
                    arcname,
                    include_secrets=include_secrets,
                    is_oauth_dir=(entry.name == _OAUTH_DIR),
                )
            else:
                _add_file_to_tar(
                    tar,
                    entry,
                    arcname,
                    include_secrets=include_secrets,
                )

    return output_path


def _add_file_to_tar(
    tar: tarfile.TarFile,
    src: Path,
    arcname: str,
    *,
    include_secrets: bool,
    is_oauth_file: bool = False,
) -> None:
    """Add a single file to the archive, applying redaction if needed.

    OAuth-token files (``is_oauth_file=True``) are bundled verbatim and
    given 0o600 mode regardless of the ``include_secrets`` setting —
    redacting them would make the export useless for its intended
    purpose (migrating to a new machine), and we always want
    owner-only perms on tokens.
    """
    raw = src.read_bytes()

    # OAuth files: never apply redaction heuristics (would defeat the
    # purpose of include_oauth_tokens). Always 0o600 perm.
    if is_oauth_file:
        info = tarfile.TarInfo(name=arcname)
        info.size = len(raw)
        info.mode = 0o600
        info.mtime = int(src.stat().st_mtime)
        tar.addfile(info, io.BytesIO(raw))
        return

    if not include_secrets:
        if src.name == ".env":
            redacted_text = _redact_env_text(raw.decode("utf-8", errors="replace"))
            raw = redacted_text.encode("utf-8")
        elif src.name == "config.yaml" or src.suffix in (".yaml", ".yml"):
            redacted_text = _redact_yaml_text(raw.decode("utf-8", errors="replace"))
            raw = redacted_text.encode("utf-8")

    info = tarfile.TarInfo(name=arcname)
    info.size = len(raw)
    info.mode = 0o600 if src.name in (".env", "secrets") else 0o644
    info.mtime = int(src.stat().st_mtime)
    tar.addfile(info, io.BytesIO(raw))


def _add_dir_to_tar(
    tar: tarfile.TarFile,
    src_dir: Path,
    arcname: str,
    *,
    include_secrets: bool,
    is_oauth_dir: bool = False,
) -> None:
    """Recursively add a directory to the archive.

    ``is_oauth_dir=True`` propagates verbatim-bundle semantics to every
    file underneath (OAuth dirs may contain nested per-server token
    files; redacting any of them defeats the export purpose).
    """
    for sub in sorted(src_dir.iterdir()):
        if sub.name in _ALWAYS_EXCLUDED_DIRS:
            continue
        sub_arc = f"{arcname}/{sub.name}"
        if sub.is_dir():
            _add_dir_to_tar(
                tar,
                sub,
                sub_arc,
                include_secrets=include_secrets,
                is_oauth_dir=is_oauth_dir,
            )
        else:
            _add_file_to_tar(
                tar,
                sub,
                sub_arc,
                include_secrets=include_secrets,
                is_oauth_file=is_oauth_dir,
            )


def list_archive_files(archive_path: Path) -> list[str]:
    """Return the relative profile-internal paths the archive would write.

    Used by the CLI's ``--dry-run`` mode to preview what an import would
    do without touching the target directory. Strips the ``profile/``
    prefix and skips ``manifest.json`` (an envelope file, not a profile
    artifact). Path-traversal entries are filtered out — they would be
    rejected during a real import anyway.
    """
    if not archive_path.exists():
        raise FileNotFoundError(f"archive does not exist: {archive_path}")
    out: list[str] = []
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == "manifest.json":
                continue
            if not member.name.startswith("profile/"):
                continue
            rel = member.name[len("profile/"):]
            if not rel:
                continue
            # Skip clearly-traversal entries; real import would raise on these.
            if ".." in Path(rel).parts:
                continue
            if member.isdir():
                continue
            out.append(rel)
    return sorted(out)


def import_profile(
    archive_path: Path,
    target_profile_dir: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Extract ``archive_path`` into ``target_profile_dir``.

    Refuses to overwrite an existing non-empty target unless ``force=True``.
    Validates the archive contains a recognizable ``manifest.json``.

    With ``dry_run=True``, validates the archive and existence checks but
    writes nothing to disk. Always returns the manifest dict — the CLI
    pairs this with :func:`list_archive_files` for the preview output.
    """
    if not archive_path.exists():
        raise FileNotFoundError(f"archive does not exist: {archive_path}")

    if (
        target_profile_dir.exists()
        and target_profile_dir.is_dir()
        and any(target_profile_dir.iterdir())
        and not force
    ):
        raise FileExistsError(
            f"target profile dir is non-empty: {target_profile_dir}. "
            "Pass force=True to overwrite."
        )

    if not dry_run:
        target_profile_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as tar:
        # Validate manifest first (in both modes — dry-run still rejects
        # malformed archives so the preview never lies about a doomed import).
        try:
            manifest_member = tar.getmember("manifest.json")
        except KeyError as exc:
            raise ValueError(
                f"archive missing manifest.json: {archive_path}"
            ) from exc

        manifest_f = tar.extractfile(manifest_member)
        if manifest_f is None:
            raise ValueError(f"could not read manifest.json from {archive_path}")
        manifest = json.loads(manifest_f.read())
        if manifest.get("format_version") != "1":
            raise ValueError(
                f"unsupported archive format_version: "
                f"{manifest.get('format_version')!r}"
            )

        if dry_run:
            # Validate path-traversal safety in dry-run too — we want the
            # preview to surface the same error a real import would.
            for member in tar.getmembers():
                if member.name == "manifest.json":
                    continue
                if not member.name.startswith("profile/"):
                    continue
                rel = member.name[len("profile/"):]
                if not rel:
                    continue
                target = (target_profile_dir / rel).resolve()
                try:
                    target.relative_to(target_profile_dir.resolve())
                except ValueError as exc:
                    raise ValueError(
                        f"archive contains unsafe path: {member.name}"
                    ) from exc
            return manifest

        # Extract every member under profile/ into target_profile_dir
        for member in tar.getmembers():
            if member.name == "manifest.json":
                continue
            if not member.name.startswith("profile/"):
                continue
            # Resolve target path
            rel = member.name[len("profile/"):]
            if not rel:
                continue
            # Path-traversal safety
            target = (target_profile_dir / rel).resolve()
            try:
                target.relative_to(target_profile_dir.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"archive contains unsafe path: {member.name}"
                ) from exc
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                f = tar.extractfile(member)
                if f is None:
                    continue
                target.write_bytes(f.read())
                try:
                    target.chmod(member.mode & 0o777)
                except OSError:
                    pass

    return manifest


__all__ = [
    "export_profile",
    "import_profile",
    "list_archive_files",
    "_redact_env_text",
    "_redact_yaml_data",
]
