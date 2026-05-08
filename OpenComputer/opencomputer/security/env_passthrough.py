"""Skill-scoped environment variable + credential file passthrough registry.

Hermes-parity for P3.4 (``required_environment_variables``) and P3.5
(``required_credential_files``) SKILL.md frontmatter. The registry is
the single source of truth that ExecuteCode + Docker sandbox + setup
wizard all consult.

Lifecycle:

1. Skill-loader (in :mod:`opencomputer.agent.memory`) calls
   :func:`register_skill_requirements` once per loaded skill with the
   skill id + parsed declarations.
2. Tools that spawn subprocesses (ExecuteCode, sandbox.docker) call
   :func:`get_passthrough_env_keys` to find which parent env vars to
   forward.
3. Setup wizard / ``oc skills env`` CLI calls
   :func:`get_missing_required_env_vars` to discover declared-but-unset
   vars and prompt the user.
4. Docker sandbox calls :func:`get_required_credential_files` to find
   file paths to bind-mount as ``-v host:container:ro``.

Thread-safety: a single global module-level registry guarded by a lock.
Skill-loading is single-threaded today; the lock is defensive against
future hot-reload paths.

Privacy: this module never reads or stores secret values. It only
tracks declarations + the file paths to mount.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger("opencomputer.security.env_passthrough")

if TYPE_CHECKING:
    from opencomputer.agent.memory import (
        RequiredCredentialFile,
        RequiredEnvVar,
    )


@dataclass(frozen=True, slots=True)
class _SkillRegistration:
    """Internal record — what a single skill declared."""

    env_vars: tuple[RequiredEnvVar, ...]
    credential_files: tuple[RequiredCredentialFile, ...]


_lock = threading.Lock()
_registry: dict[str, _SkillRegistration] = {}


def register_skill_requirements(
    skill_id: str,
    *,
    env_vars: tuple[RequiredEnvVar, ...],
    credential_files: tuple[RequiredCredentialFile, ...],
) -> None:
    """Register (or replace) a skill's declared env + credential needs.

    Called by :class:`opencomputer.agent.memory.MemoryManager.list_skills`
    once per skill on every scan. Re-registration is intentional — a
    skill that's been edited in-place should pick up new declarations
    without a daemon restart.

    Empty inputs are valid (skill declared neither). The skill is
    REMOVED from the registry in that case so it doesn't continue to
    contribute stale entries to ``get_passthrough_env_keys`` after the
    user removed all required_* keys.
    """
    with _lock:
        if not env_vars and not credential_files:
            _registry.pop(skill_id, None)
            return
        _registry[skill_id] = _SkillRegistration(
            env_vars=env_vars,
            credential_files=credential_files,
        )


def unregister_skill_requirements(skill_id: str) -> None:
    """Drop a skill from the registry (called on uninstall / shadow)."""
    with _lock:
        _registry.pop(skill_id, None)


def clear_registry_for_tests() -> None:
    """Test helper — wipe the registry."""
    with _lock:
        _registry.clear()


def get_passthrough_env_keys() -> tuple[str, ...]:
    """Return the union of all skill-declared env-var names.

    Output is sorted + de-duplicated. Empty when no skills have
    declared anything. ExecuteCode + sandbox merge this set with their
    own config-driven ``env_passthrough`` list.
    """
    with _lock:
        names: set[str] = set()
        for reg in _registry.values():
            for v in reg.env_vars:
                if v.name:
                    names.add(v.name)
    return tuple(sorted(names))


def get_required_env_var_declarations() -> tuple[RequiredEnvVar, ...]:
    """Return every declared :class:`RequiredEnvVar` across all skills.

    Used by the setup wizard / ``oc skills env`` CLI to render the
    list of vars the user might want to set, with their human-readable
    prompts + help URLs.
    """
    with _lock:
        out: list[RequiredEnvVar] = []
        seen: set[str] = set()
        for reg in _registry.values():
            for v in reg.env_vars:
                if v.name and v.name not in seen:
                    seen.add(v.name)
                    out.append(v)
    return tuple(out)


def get_missing_required_env_vars() -> tuple[RequiredEnvVar, ...]:
    """Return declared env vars that are NOT currently in :data:`os.environ`.

    Used by setup wizard and ``oc doctor`` to flag missing
    credentials. An env var that's set to an empty string is treated as
    missing (the typical user mistake — ``export FOO=`` instead of
    ``export FOO=value``).
    """
    declared = get_required_env_var_declarations()
    return tuple(v for v in declared if not os.environ.get(v.name, "").strip())


def get_required_credential_files() -> tuple[RequiredCredentialFile, ...]:
    """Return every declared :class:`RequiredCredentialFile`."""
    with _lock:
        out: list[RequiredCredentialFile] = []
        seen: set[str] = set()
        for reg in _registry.values():
            for f in reg.credential_files:
                if f.path and f.path not in seen:
                    seen.add(f.path)
                    out.append(f)
    return tuple(out)


def resolve_credential_file_paths(profile_home: Path) -> tuple[tuple[Path, str], ...]:
    """Return ``(host_path, container_path)`` pairs for Docker bind-mounts.

    Each declared credential file is resolved under
    ``profile_home / <path>``; pairs are returned only for files that
    EXIST on disk (a missing credential is a soft failure — the skill
    that needs it will surface its own error when the file isn't where
    expected). ``container_path`` is always
    ``/root/.opencomputer/<path>`` to mirror Hermes' convention.

    Ops can prevent surprising mounts by setting permissive 0600 on the
    declared files; the bind mount is read-only so the container can't
    overwrite them either way.
    """
    out: list[tuple[Path, str]] = []
    for f in get_required_credential_files():
        host = (profile_home / f.path).resolve()
        # Defensive: don't escape profile_home via "../" tricks.
        try:
            host.relative_to(profile_home.resolve())
        except ValueError:
            logger.warning(
                "credential file %r escapes profile home — skipping",
                f.path,
            )
            continue
        if not host.exists():
            logger.debug(
                "declared credential file %s missing at %s — skipping mount",
                f.path, host,
            )
            continue
        container = f"/root/.opencomputer/{f.path}"
        out.append((host, container))
    return tuple(out)


__all__ = [
    "clear_registry_for_tests",
    "get_missing_required_env_vars",
    "get_passthrough_env_keys",
    "get_required_credential_files",
    "get_required_env_var_declarations",
    "register_skill_requirements",
    "resolve_credential_file_paths",
    "unregister_skill_requirements",
]
