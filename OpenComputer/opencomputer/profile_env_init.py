"""Interactive `.env` initializer for the active profile.

Phase 14.G T2 — D.4 follow-up (2026-05-05). Companion to
:mod:`opencomputer.profile_env_template`. Where ``env-template`` writes
a fillable file users edit by hand, ``env-init`` walks every declared
env var across enabled plugins and prompts (with masking) for each
missing value, then writes the result atomically to ``<profile>/.env``
with mode 0600.

Pure logic in this module is exposed for tests; the CLI wrapper lives
in :func:`opencomputer.cli_profile.env_init_cmd`.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ─── Hermes config v2 — secret-key heuristic + single-key writer ─────

#: Conservative case-insensitive pattern matching common secret-name
#: suffixes (api_key, token, secret, password, webhook_url). Tested at
#: word-boundary or after ``.``/``_`` so ``memory.provider`` is rejected.
_SECRET_PATTERN = re.compile(
    r"(?i)(^|[._-])(api[_-]?key|token|secret|password|webhook[_-]?url)$"
)


def is_secret_key(key: str) -> bool:
    """Return True iff ``key`` matches the Hermes-v2 secret-name heuristic.

    Matches: ``OPENAI_API_KEY``, ``GITHUB_TOKEN``, ``CLIENT_SECRET``,
    ``DB_PASSWORD``, ``SLACK_WEBHOOK_URL``, dotted ``custom.api_key``.
    Rejects: ``memory.provider``, ``max_iterations``, ``language``.
    """
    return bool(_SECRET_PATTERN.search(key))


def write_env_var(env_path: Path, key: str, value: str) -> None:
    """Write ``KEY=VALUE`` to ``env_path`` atomically.

    - If ``env_path`` exists, update existing line for ``key`` or append.
    - File created with mode 0600 (owner-only).
    - Comments and blank lines preserved.
    - Multi-line writes go through :func:`atomic_write` (tempfile + rename).
    """
    existing = parse_env_file(env_path) if env_path.exists() else {}
    # Preserve original ordering by reading raw lines and editing in place
    # when the key already exists, else appending.
    out_lines: list[str] = []
    found = False
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                out_lines.append(raw)
                continue
            if "=" not in stripped:
                out_lines.append(raw)
                continue
            line_key = stripped.partition("=")[0].strip()
            if line_key == key and not found:
                out_lines.append(f"{key}={_quote_env_value(value)}")
                found = True
                continue
            if line_key == key:
                # Drop further duplicates of the same key.
                continue
            out_lines.append(raw)
    if not found:
        out_lines.append(f"{key}={_quote_env_value(value)}")

    body = "\n".join(out_lines).rstrip() + "\n"
    atomic_write(env_path, body, mode=0o600)
    # ``existing`` retained in scope so test assertions on prior values
    # remain meaningful — but we don't merge through render_env_file
    # (which reorders + adds a header).
    _ = existing


def _quote_env_value(value: str) -> str:
    """Quote a value for .env iff it contains whitespace, quotes, or =."""
    if not value:
        return '""'
    if any(c in value for c in (" ", "\t", "\n", '"', "'", "=", "#", "$")):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


@dataclass(frozen=True)
class EnvVarSpec:
    """One declared env var attached to its origin (plugin + setup target)."""

    var_name: str
    plugin_id: str
    label: str
    signup_url: str
    description: str

    @property
    def display(self) -> str:
        """Single-line label for the prompt: ``OPENAI_API_KEY (openai-provider — OpenAI)``."""
        if self.label and self.label != self.plugin_id:
            return f"{self.var_name} ({self.plugin_id} — {self.label})"
        return f"{self.var_name} ({self.plugin_id})"


def collect_env_var_specs(
    plugins: Iterable[Any],
    *,
    enabled_ids: set[str] | None = None,
) -> list[EnvVarSpec]:
    """Walk plugin candidates and collect every declared env var.

    Deduplicates by ``var_name`` — first occurrence wins. Skips plugins
    not in ``enabled_ids`` when set. Returns specs in encounter order so
    the prompt sequence is stable.
    """
    seen: set[str] = set()
    specs: list[EnvVarSpec] = []

    for cand in plugins:
        manifest = getattr(cand, "manifest", None)
        if manifest is None:
            continue
        plugin_id = getattr(manifest, "id", "")
        if not plugin_id:
            continue
        if enabled_ids is not None and plugin_id not in enabled_ids:
            continue

        plugin_desc = getattr(manifest, "description", "")
        setup = getattr(manifest, "setup", None)
        if setup is None:
            continue

        for target_kind in ("providers", "channels"):
            for target in getattr(setup, target_kind, ()):
                env_vars = tuple(getattr(target, "env_vars", ()))
                label = getattr(target, "label", "") or plugin_id
                signup_url = getattr(target, "signup_url", "")
                for var in env_vars:
                    if var in seen:
                        continue
                    seen.add(var)
                    specs.append(
                        EnvVarSpec(
                            var_name=var,
                            plugin_id=plugin_id,
                            label=label,
                            signup_url=signup_url,
                            description=plugin_desc,
                        )
                    )

    return specs


def parse_env_file(path: Path) -> dict[str, str]:
    """Best-effort parse of a `.env` file → dict.

    Comments and blank lines skipped. Quoted values stripped. Bad lines
    silently skipped (not our job to validate the user's hand-edits).
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def render_env_file(values: dict[str, str], *, profile_name: str = "default") -> str:
    """Format the merged values as a clean `.env` file.

    Stable key order via sort. Comment header echoes the profile.
    """
    lines = [
        "# " + "=" * 64,
        f"# OpenComputer profile: {profile_name}",
        "# Auto-generated by `oc profile env-init` — edit by hand if needed.",
        "# " + "=" * 64,
        "",
    ]
    for key in sorted(values):
        val = values[key]
        # Quote if value contains whitespace or shell-special chars.
        if any(c in val for c in (" ", "\t", "#", "$", "\"", "'")):
            escaped = val.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{escaped}"')
        else:
            lines.append(f"{key}={val}")
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Write `content` to `path` atomically with the given file mode.

    Uses tempfile in the destination directory + os.replace so a partial
    write never leaves a half-written file behind. Sets mode 0600 by
    default (matches secrets-file convention).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class InitResult:
    """Summary of an env-init run for the CLI to print."""

    written: int
    skipped_existing: int
    skipped_empty: int
    target_path: Path


def run_init(
    specs: list[EnvVarSpec],
    *,
    target_path: Path,
    profile_name: str,
    prompter: Callable[[EnvVarSpec, str | None], str | None],
    overwrite: bool = False,
) -> InitResult:
    """Run the interactive flow.

    ``prompter(spec, current_value)`` returns the user's input, an empty
    string to skip, or ``None`` if the user aborted (Ctrl-C). On abort
    we raise ``KeyboardInterrupt`` so the caller can decide what to do.
    """
    existing = parse_env_file(target_path)
    merged = dict(existing)

    written = 0
    skipped_existing = 0
    skipped_empty = 0

    for spec in specs:
        current = existing.get(spec.var_name, "")
        if current and not overwrite:
            skipped_existing += 1
            continue

        try:
            entered = prompter(spec, current or None)
        except KeyboardInterrupt:
            raise

        if entered is None:
            raise KeyboardInterrupt
        entered = entered.strip()
        if not entered:
            skipped_empty += 1
            continue

        merged[spec.var_name] = entered
        written += 1

    rendered = render_env_file(merged, profile_name=profile_name)
    atomic_write(target_path, rendered)

    return InitResult(
        written=written,
        skipped_existing=skipped_existing,
        skipped_empty=skipped_empty,
        target_path=target_path,
    )


__all__ = [
    "EnvVarSpec",
    "InitResult",
    "atomic_write",
    "collect_env_var_specs",
    "is_secret_key",
    "parse_env_file",
    "render_env_file",
    "run_init",
    "write_env_var",
]
