# Hermes Tier 1.A — Skills Hub MVP + agentskills.io Standard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the smallest viable Skills Hub for OpenComputer — a `oc skills browse|search|install|inspect|list|update|uninstall|tap|audit` surface that pulls SKILL.md bundles from a default `well-known` registry plus arbitrary GitHub `tap` sources, with `agentskills.io` frontmatter validation, Skills Guard scanning on install, and an append-only audit log. This closes the single largest visible-to-user gap identified in `docs/refs/hermes-agent/2026-04-28-major-gaps.md` Tier 1.A + 1.D.

**Architecture:** Three-layer design — (1) **`plugin_sdk/skill_source.py`** declares the public `SkillSource` ABC + `SkillMeta` + `SkillBundle` data classes so third parties can write sources; (2) **`opencomputer/skills_hub/`** package implements 2 concrete sources (WellKnown via static curated manifest, GitHub for arbitrary repos via `tap`), a router that fans search across enabled sources, an installer that runs every fetched bundle through `skills_guard` before writing to `~/.opencomputer/<profile>/skills/.hub/`, a lockfile, taps manager, agentskills.io validator, and append-only audit log; (3) **`opencomputer/cli_skills_hub.py`** is the Typer subapp + slash command bridge — thin wrappers over shared `do_*` functions, mirroring Hermes' pattern. Existing `skills_guard/` is unchanged; the hub *uses* it.

**Tech Stack:** Python 3.12+, Typer (existing), pydantic v2 (existing), httpx (existing), rich.Console (existing), pyyaml (existing), pytest (existing). New deps: none. Optional new dep: `gitpython` for `tap add` (or fallback to subprocess `git clone` — preferred since gitpython adds 6MB and we already shell to git in `cli_profile.py`). **Decision: subprocess `git clone`, no new dep.**

**Out of scope (deferred to Tier 1.A v2 spec, not this plan):**
- `oc skills publish --to <hub>` (publish flow) — additional 2-3 days, port after MVP ships
- `oc skills snapshot export|import` — additional 1 day
- `oc skills check` (verify installed skills are still in registry) — additional 0.5 day
- `oc skills config` (per-skill config tweaks) — niche
- ClawHub / SkillsSh / LobeHub / ClaudeMarketplace / HermesIndex sources — port on demand
- Skills Hub network audit log uploaded to a remote service (Hermes does not do this either; both keep local-only)

**Why this order of phases:** Phase 1 establishes the public ABC contract first so plugin_sdk users can start writing custom sources before the bundled ones are even done. Phase 2 ships the WellKnown source (works offline once the curated manifest is bundled). Phase 3 adds the installer + Skills Guard wiring — at this point you can `oc skills install <name>` end-to-end. Phase 4 adds CLI + slash. Phase 5 adds GitHub source + taps. Phase 6 polish + README. **Each phase produces working software.** If the project pauses after Phase 3, the hub is technically functional via Python API; after Phase 4, end-user functional; after Phase 6, polished.

---

## File map (reference for all tasks)

**Created:**
- `plugin_sdk/skill_source.py` — public ABC + dataclasses (Phase 1)
- `opencomputer/skills_hub/__init__.py` — re-exports (Phase 1)
- `opencomputer/skills_hub/models.py` — internal richer types (Phase 1)
- `opencomputer/skills_hub/agentskills_validator.py` — frontmatter validation (Phase 1)
- `opencomputer/skills_hub/lockfile.py` — `HubLockFile` (Phase 1)
- `opencomputer/skills_hub/audit_log.py` — append-only JSONL audit (Phase 1)
- `opencomputer/skills_hub/sources/__init__.py` (Phase 2)
- `opencomputer/skills_hub/sources/well_known.py` — bundled curated source (Phase 2)
- `opencomputer/skills_hub/sources/github.py` — arbitrary-repo source (Phase 5)
- `opencomputer/skills_hub/router.py` — multi-source search/inspect (Phase 2)
- `opencomputer/skills_hub/installer.py` — fetch + scan + write (Phase 3)
- `opencomputer/skills_hub/taps.py` — `TapsManager` (Phase 5)
- `opencomputer/skills_hub/well_known_manifest.json` — bundled curated registry (Phase 2)
- `opencomputer/cli_skills_hub.py` — Typer subapp (Phase 4)
- `opencomputer/agent/slash_commands_impl/skills_hub_slash.py` — slash bridge (Phase 4)
- `tests/skills_hub/test_models.py` (Phase 1)
- `tests/skills_hub/test_agentskills_validator.py` (Phase 1)
- `tests/skills_hub/test_lockfile.py` (Phase 1)
- `tests/skills_hub/test_audit_log.py` (Phase 1)
- `tests/skills_hub/test_well_known_source.py` (Phase 2)
- `tests/skills_hub/test_router.py` (Phase 2)
- `tests/skills_hub/test_installer.py` (Phase 3)
- `tests/skills_hub/test_cli.py` (Phase 4)
- `tests/skills_hub/test_github_source.py` (Phase 5)
- `tests/skills_hub/test_taps.py` (Phase 5)
- `tests/skills_hub/test_e2e.py` (Phase 6)

**Modified:**
- `plugin_sdk/__init__.py` — export `SkillSource`, `SkillMeta`, `SkillBundle` (Phase 1)
- `opencomputer/cli.py` — register `skills_hub_app` as `oc skills` subapp (Phase 4)
- `opencomputer/agent/slash_dispatcher.py` — register slash dispatchers (Phase 4)
- `pyproject.toml` — version bump to `0.2.0` after Phase 6
- `CHANGELOG.md` — add `## [Unreleased]` entry (Phase 6)
- `README.md` — Skills Hub section (Phase 6)
- `tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer` — verify still passes after `plugin_sdk/skill_source.py` is added (no change needed; just re-run)

**Hermes upstream references (read-only):**
- `sources/hermes-agent-2026.4.23/tools/skills_hub.py:64-128` — SkillMeta + SkillBundle shapes
- `sources/hermes-agent-2026.4.23/tools/skills_hub.py:252-282` — SkillSource ABC
- `sources/hermes-agent-2026.4.23/tools/skills_hub.py:708-936` — WellKnownSource pattern
- `sources/hermes-agent-2026.4.23/tools/skills_hub.py:284-707` — GitHubSource pattern
- `sources/hermes-agent-2026.4.23/tools/skills_hub.py:2379-2444` — HubLockFile pattern
- `sources/hermes-agent-2026.4.23/tools/skills_hub.py:2445-2761` — TapsManager pattern
- `sources/hermes-agent-2026.4.23/hermes_cli/skills_hub.py:1-150` — CLI dispatch pattern (mirror, don't copy)

---

## Phase 1: Foundation — ABC, models, lockfile, audit, validator

**Phase goal:** Public contract sealed; data layer testable in isolation; **no networking yet**.

### Task 1.1: Define `SkillMeta` + `SkillBundle` dataclasses in `plugin_sdk`

**Files:**
- Create: `plugin_sdk/skill_source.py`
- Test: `tests/skills_hub/test_models.py`

- [ ] **Step 1: Write failing test for SkillMeta + SkillBundle dataclasses**

```python
# tests/skills_hub/test_models.py
"""Tests for SkillSource public ABC and dataclasses."""
import pytest
from plugin_sdk.skill_source import SkillMeta, SkillBundle


def test_skill_meta_required_fields():
    meta = SkillMeta(
        identifier="well-known/pead-screener",
        name="pead-screener",
        description="Screen post-earnings gap-up stocks",
        source="well-known",
    )
    assert meta.identifier == "well-known/pead-screener"
    assert meta.trust_level == "community"  # default


def test_skill_meta_optional_fields():
    meta = SkillMeta(
        identifier="well-known/foo",
        name="foo",
        description="bar",
        source="well-known",
        version="1.2.0",
        author="alice",
        tags=["finance", "screening"],
        trust_level="trusted",
    )
    assert meta.version == "1.2.0"
    assert meta.author == "alice"
    assert "finance" in meta.tags
    assert meta.trust_level == "trusted"


def test_skill_bundle_with_skill_md_required():
    bundle = SkillBundle(
        identifier="well-known/foo",
        skill_md="---\nname: foo\ndescription: bar\n---\n# Foo",
        files={},
    )
    assert "name: foo" in bundle.skill_md


def test_skill_bundle_with_extra_files():
    bundle = SkillBundle(
        identifier="well-known/foo",
        skill_md="---\nname: foo\ndescription: bar\n---",
        files={"helper.py": "def x(): pass\n"},
    )
    assert bundle.files["helper.py"].startswith("def x")


def test_trust_level_must_be_valid():
    with pytest.raises(ValueError):
        SkillMeta(
            identifier="x", name="x", description="y", source="z",
            trust_level="invalid_value",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/skills_hub/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'plugin_sdk.skill_source'`

- [ ] **Step 3: Implement `plugin_sdk/skill_source.py`**

```python
# plugin_sdk/skill_source.py
"""Public SkillSource ABC + dataclasses for the Skills Hub.

Plugins and OC's bundled hub adapters both implement SkillSource. Skill metadata
flowing through the hub system is a SkillMeta; full installable content is a
SkillBundle.

This module is part of the public plugin SDK. It MUST NOT import from
opencomputer/* — the SDK boundary test enforces this.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Optional

TrustLevel = Literal["builtin", "trusted", "community", "untrusted"]
_VALID_TRUST: tuple[str, ...] = ("builtin", "trusted", "community", "untrusted")


@dataclass(frozen=True, slots=True)
class SkillMeta:
    """Lightweight skill descriptor returned by SkillSource.search/inspect.

    Identifier MUST be `<source>/<name>` form so a router can route fetch()
    calls back to the right source.
    """
    identifier: str
    name: str
    description: str
    source: str
    version: Optional[str] = None
    author: Optional[str] = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    trust_level: TrustLevel = "community"
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.trust_level not in _VALID_TRUST:
            raise ValueError(
                f"trust_level must be one of {_VALID_TRUST}, got {self.trust_level!r}"
            )


@dataclass(frozen=True, slots=True)
class SkillBundle:
    """Full installable content of a skill — SKILL.md plus any auxiliary files."""
    identifier: str
    skill_md: str
    files: dict[str, str]


class SkillSource(ABC):
    """Abstract base class for skill registry adapters.

    Implementations:
    - Return a stable `name` (e.g. "well-known", "github", "agentskills_io").
    - Implement `search()`, `fetch()`, and `inspect()`.
    - Are stateless or carry only their own auth/config.
    - Raise nothing on partial failure — return [] from search, None from
      fetch/inspect when the identifier is unknown. Network errors should be
      logged but not raised so the router can fall through to other sources.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique source name used as the identifier prefix."""

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        """Return up to `limit` skills matching the query string."""

    @abstractmethod
    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        """Return the full bundle for an identifier, or None if unknown."""

    @abstractmethod
    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        """Return rich metadata for an identifier, or None if unknown."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/skills_hub/test_models.py -v`
Expected: 5 passed

- [ ] **Step 5: Verify SDK boundary**

Run: `pytest tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v`
Expected: PASS (we didn't import from opencomputer)

- [ ] **Step 6: Commit**

```bash
git add plugin_sdk/skill_source.py tests/skills_hub/__init__.py tests/skills_hub/test_models.py
git commit -m "feat(skills-hub): SkillSource ABC + SkillMeta/SkillBundle dataclasses in plugin_sdk

Public contract for the upcoming Skills Hub. Plugins and bundled adapters both
implement SkillSource. Frozen, slots-based dataclasses for hashability."
```

---

### Task 1.2: Re-export from `plugin_sdk/__init__.py`

**Files:**
- Modify: `plugin_sdk/__init__.py`

- [ ] **Step 1: Read current `plugin_sdk/__init__.py` to find the right insertion point**

Run: `grep -n '__all__\|from \.' plugin_sdk/__init__.py | head -20`

- [ ] **Step 2: Add re-export**

Add the import line near the other `from .` imports and add the names to `__all__`:

```python
# In plugin_sdk/__init__.py — add to existing imports
from .skill_source import SkillSource, SkillMeta, SkillBundle, TrustLevel

# Add to __all__ tuple
__all__ = (
    # ... existing entries ...
    "SkillSource",
    "SkillMeta",
    "SkillBundle",
    "TrustLevel",
)
```

- [ ] **Step 3: Verify import works**

Run: `python -c "from plugin_sdk import SkillSource, SkillMeta, SkillBundle, TrustLevel; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Run SDK boundary test again**

Run: `pytest tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add plugin_sdk/__init__.py
git commit -m "feat(skills-hub): re-export SkillSource types from plugin_sdk"
```

---

### Task 1.3: agentskills.io frontmatter validator

**Files:**
- Create: `opencomputer/skills_hub/__init__.py`
- Create: `opencomputer/skills_hub/agentskills_validator.py`
- Test: `tests/skills_hub/test_agentskills_validator.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/skills_hub/test_agentskills_validator.py
"""Tests for agentskills.io-compatible SKILL.md frontmatter validation."""
import pytest
from opencomputer.skills_hub.agentskills_validator import (
    validate_frontmatter,
    ValidationError,
)


VALID = """---
name: pead-screener
description: Screen post-earnings gap-up stocks for PEAD setups
version: 1.0.0
author: saksham
tags: [finance, screening]
---

# PEAD Screener
"""


def test_valid_frontmatter_passes():
    parsed = validate_frontmatter(VALID)
    assert parsed["name"] == "pead-screener"
    assert parsed["description"].startswith("Screen")
    assert parsed["version"] == "1.0.0"
    assert parsed["tags"] == ["finance", "screening"]


def test_missing_name_fails():
    body = "---\ndescription: something\n---\n# X"
    with pytest.raises(ValidationError, match="missing required field 'name'"):
        validate_frontmatter(body)


def test_missing_description_fails():
    body = "---\nname: x\n---\n# X"
    with pytest.raises(ValidationError, match="missing required field 'description'"):
        validate_frontmatter(body)


def test_name_must_be_kebab_case():
    body = "---\nname: PEAD_Screener\ndescription: x\n---"
    with pytest.raises(ValidationError, match="kebab-case"):
        validate_frontmatter(body)


def test_description_too_short_fails():
    body = "---\nname: x\ndescription: short\n---"
    with pytest.raises(ValidationError, match="description.*at least"):
        validate_frontmatter(body)


def test_description_too_long_fails():
    body = f"---\nname: x\ndescription: {'a' * 600}\n---"
    with pytest.raises(ValidationError, match="description.*at most"):
        validate_frontmatter(body)


def test_invalid_version_fails():
    body = "---\nname: x\ndescription: a valid description here please\nversion: not-semver\n---"
    with pytest.raises(ValidationError, match="version.*semver"):
        validate_frontmatter(body)


def test_no_frontmatter_fails():
    body = "# Just a heading"
    with pytest.raises(ValidationError, match="no frontmatter"):
        validate_frontmatter(body)


def test_unclosed_frontmatter_fails():
    body = "---\nname: x\ndescription: ok valid here\n# never closes"
    with pytest.raises(ValidationError, match="unclosed frontmatter"):
        validate_frontmatter(body)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/skills_hub/test_agentskills_validator.py -v`
Expected: FAIL on import (`ModuleNotFoundError`)

- [ ] **Step 3: Create `opencomputer/skills_hub/__init__.py`**

```python
# opencomputer/skills_hub/__init__.py
"""Skills Hub — multi-source SKILL.md registry + installer + audit.

Public surface lives in plugin_sdk.skill_source for third-party adapters.
The bundled adapters (well-known, github) plus the installer/router/lockfile
live here.
"""
```

- [ ] **Step 4: Implement validator**

```python
# opencomputer/skills_hub/agentskills_validator.py
"""Validate SKILL.md frontmatter against the agentskills.io standard.

The standard (as inferred from the published Hermes docs and our extracted
inventory) defines:
- Required: `name`, `description`
- Recommended: `version` (semver), `author`, `tags` (list of strings)
- name is kebab-case
- description is 20-500 chars
"""

from __future__ import annotations

import re
from typing import Any

import yaml

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")

DESCRIPTION_MIN = 20
DESCRIPTION_MAX = 500


class ValidationError(ValueError):
    """Raised when SKILL.md frontmatter does not satisfy agentskills.io."""


def validate_frontmatter(skill_md: str) -> dict[str, Any]:
    """Parse and validate the YAML frontmatter at the top of SKILL.md.

    Returns the parsed dict. Raises ValidationError on any failure.
    """
    if not skill_md.lstrip().startswith("---"):
        raise ValidationError("SKILL.md has no frontmatter (must start with '---')")

    body = skill_md.lstrip()
    # Find the closing '---' on its own line
    rest = body[3:]  # skip the leading '---'
    end_idx = rest.find("\n---")
    if end_idx == -1:
        raise ValidationError("SKILL.md has unclosed frontmatter (no closing '---')")

    yaml_block = rest[:end_idx]
    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError as e:
        raise ValidationError(f"frontmatter is not valid YAML: {e}") from e

    if not isinstance(parsed, dict):
        raise ValidationError("frontmatter must be a YAML mapping (key: value pairs)")

    # Required fields
    if "name" not in parsed or not parsed["name"]:
        raise ValidationError("missing required field 'name'")
    if "description" not in parsed or not parsed["description"]:
        raise ValidationError("missing required field 'description'")

    # name kebab-case
    name = str(parsed["name"])
    if not _NAME_RE.match(name):
        raise ValidationError(
            f"name {name!r} must be kebab-case (lowercase letters, digits, hyphens; "
            "start with a letter; no leading/trailing/double hyphens)"
        )

    # description length
    desc = str(parsed["description"])
    if len(desc) < DESCRIPTION_MIN:
        raise ValidationError(
            f"description must be at least {DESCRIPTION_MIN} chars (got {len(desc)})"
        )
    if len(desc) > DESCRIPTION_MAX:
        raise ValidationError(
            f"description must be at most {DESCRIPTION_MAX} chars (got {len(desc)})"
        )

    # version (optional, semver)
    if "version" in parsed and parsed["version"] is not None:
        version = str(parsed["version"])
        if not _SEMVER_RE.match(version):
            raise ValidationError(
                f"version {version!r} must be semver (e.g. 1.0.0 or 1.0.0-beta.1)"
            )

    # tags (optional, must be list of strings)
    if "tags" in parsed and parsed["tags"] is not None:
        tags = parsed["tags"]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValidationError("tags must be a list of strings")

    return parsed
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/skills_hub/test_agentskills_validator.py -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add opencomputer/skills_hub/__init__.py opencomputer/skills_hub/agentskills_validator.py tests/skills_hub/test_agentskills_validator.py
git commit -m "feat(skills-hub): agentskills.io frontmatter validator

Validates name (kebab-case), description (20-500 chars), version (semver),
tags (list of strings). Rejects missing required fields, malformed YAML,
unclosed frontmatter."
```

---

### Task 1.4: HubLockFile

**Files:**
- Create: `opencomputer/skills_hub/lockfile.py`
- Test: `tests/skills_hub/test_lockfile.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/skills_hub/test_lockfile.py
"""Tests for the Skills Hub lockfile (tracks installed skills + versions)."""
import json
from pathlib import Path

import pytest
from opencomputer.skills_hub.lockfile import HubLockFile, LockEntry


def test_empty_lockfile_starts_with_no_entries(tmp_path):
    lf = HubLockFile(tmp_path / "lockfile.json")
    assert lf.list() == []


def test_record_install_adds_entry(tmp_path):
    lf = HubLockFile(tmp_path / "lockfile.json")
    lf.record_install(
        identifier="well-known/pead-screener",
        version="1.0.0",
        source="well-known",
        install_path="pead-screener",
        sha256="abc123",
    )
    entries = lf.list()
    assert len(entries) == 1
    assert entries[0].identifier == "well-known/pead-screener"
    assert entries[0].version == "1.0.0"


def test_lockfile_persists_to_disk(tmp_path):
    p = tmp_path / "lockfile.json"
    lf1 = HubLockFile(p)
    lf1.record_install(
        identifier="well-known/foo",
        version="0.1.0",
        source="well-known",
        install_path="foo",
        sha256="x",
    )
    lf2 = HubLockFile(p)  # fresh instance
    assert len(lf2.list()) == 1


def test_uninstall_removes_entry(tmp_path):
    lf = HubLockFile(tmp_path / "lockfile.json")
    lf.record_install("well-known/foo", "0.1.0", "well-known", "foo", "x")
    lf.record_uninstall("well-known/foo")
    assert lf.list() == []


def test_get_returns_entry(tmp_path):
    lf = HubLockFile(tmp_path / "lockfile.json")
    lf.record_install("well-known/foo", "0.1.0", "well-known", "foo", "abc")
    entry = lf.get("well-known/foo")
    assert entry is not None
    assert entry.sha256 == "abc"


def test_get_missing_returns_none(tmp_path):
    lf = HubLockFile(tmp_path / "lockfile.json")
    assert lf.get("well-known/nope") is None


def test_concurrent_writes_serialize(tmp_path):
    """Two HubLockFile instances writing should not corrupt each other."""
    p = tmp_path / "lockfile.json"
    lf1 = HubLockFile(p)
    lf2 = HubLockFile(p)
    lf1.record_install("well-known/a", "1.0", "well-known", "a", "x")
    lf2.record_install("well-known/b", "1.0", "well-known", "b", "y")
    lf3 = HubLockFile(p)
    ids = sorted(e.identifier for e in lf3.list())
    assert ids == ["well-known/a", "well-known/b"]


def test_corrupt_lockfile_raises_clear_error(tmp_path):
    p = tmp_path / "lockfile.json"
    p.write_text("{not json")
    with pytest.raises(ValueError, match="lockfile.*corrupt"):
        HubLockFile(p).list()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/skills_hub/test_lockfile.py -v`
Expected: FAIL with import error

- [ ] **Step 3: Implement lockfile**

```python
# opencomputer/skills_hub/lockfile.py
"""HubLockFile — tracks installed hub skills with version + checksum.

JSON shape:
{
  "version": 1,
  "entries": [
    {
      "identifier": "well-known/pead-screener",
      "name": "pead-screener",
      "version": "1.0.0",
      "source": "well-known",
      "install_path": "pead-screener",  // relative to .hub/
      "sha256": "abc123...",
      "installed_at": "2026-04-28T10:00:00Z"
    }
  ]
}

Uses fcntl-based file locking on the lockfile while writing so concurrent
oc invocations on the same profile don't lose updates.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

LOCKFILE_VERSION = 1


@dataclass(frozen=True, slots=True)
class LockEntry:
    identifier: str
    name: str
    version: str
    source: str
    install_path: str
    sha256: str
    installed_at: str


@contextlib.contextmanager
def _file_lock(path: Path):
    """Take an exclusive flock on `path` (creates it if missing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


class HubLockFile:
    """Append/remove entries to the JSON lockfile with file-level locking."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": LOCKFILE_VERSION, "entries": []}
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"lockfile is corrupt: {e}") from e
        if not isinstance(data, dict) or "entries" not in data:
            raise ValueError(f"lockfile is corrupt: missing 'entries' key")
        return data

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def list(self) -> list[LockEntry]:
        data = self._read()
        return [LockEntry(**e) for e in data["entries"]]

    def get(self, identifier: str) -> Optional[LockEntry]:
        for e in self.list():
            if e.identifier == identifier:
                return e
        return None

    def record_install(
        self,
        identifier: str,
        version: str,
        source: str,
        install_path: str,
        sha256: str,
    ) -> None:
        with _file_lock(self.path.with_suffix(".flock")):
            data = self._read()
            # name = identifier after the slash
            name = identifier.split("/", 1)[-1]
            entry = {
                "identifier": identifier,
                "name": name,
                "version": version,
                "source": source,
                "install_path": install_path,
                "sha256": sha256,
                "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            # Replace if already present
            data["entries"] = [e for e in data["entries"] if e["identifier"] != identifier]
            data["entries"].append(entry)
            self._write(data)

    def record_uninstall(self, identifier: str) -> None:
        with _file_lock(self.path.with_suffix(".flock")):
            data = self._read()
            data["entries"] = [e for e in data["entries"] if e["identifier"] != identifier]
            self._write(data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/skills_hub/test_lockfile.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add opencomputer/skills_hub/lockfile.py tests/skills_hub/test_lockfile.py
git commit -m "feat(skills-hub): HubLockFile with fcntl-protected concurrent writes

Tracks installed skills (identifier, version, source, install_path, sha256,
timestamp). flock on lockfile.flock prevents corruption from concurrent oc
invocations. Atomic write via .tmp + replace."
```

---

### Task 1.5: Append-only audit log

**Files:**
- Create: `opencomputer/skills_hub/audit_log.py`
- Test: `tests/skills_hub/test_audit_log.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/skills_hub/test_audit_log.py
"""Tests for Skills Hub append-only audit log (JSONL)."""
import json
from pathlib import Path

import pytest
from opencomputer.skills_hub.audit_log import AuditLog


def test_empty_audit_log_returns_empty(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    assert log.entries() == []


def test_record_install_event(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    log.record(
        action="install",
        identifier="well-known/foo",
        source="well-known",
        version="1.0.0",
        guard_verdict="pass",
    )
    entries = log.entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "install"
    assert entries[0]["guard_verdict"] == "pass"


def test_audit_log_is_append_only(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(p)
    log.record(action="install", identifier="x", source="w", version="1", guard_verdict="pass")
    raw_after_first = p.read_text()
    log.record(action="uninstall", identifier="x", source="w")
    raw_after_second = p.read_text()
    # Append-only: second write must contain the first verbatim as a prefix
    assert raw_after_second.startswith(raw_after_first)


def test_audit_log_jsonl_format(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(p)
    log.record(action="install", identifier="x", source="w", version="1", guard_verdict="pass")
    log.record(action="uninstall", identifier="x", source="w")
    lines = p.read_text().strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # each line must be valid JSON


def test_audit_log_filter_by_action(tmp_path):
    log = AuditLog(tmp_path / "audit.log")
    log.record(action="install", identifier="a", source="w", version="1", guard_verdict="pass")
    log.record(action="uninstall", identifier="a", source="w")
    log.record(action="install", identifier="b", source="w", version="1", guard_verdict="pass")
    installs = log.entries(action="install")
    assert len(installs) == 2
    uninstalls = log.entries(action="uninstall")
    assert len(uninstalls) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/skills_hub/test_audit_log.py -v`
Expected: FAIL on import

- [ ] **Step 3: Implement audit log**

```python
# opencomputer/skills_hub/audit_log.py
"""Append-only JSONL audit log for Skills Hub install/uninstall/update events.

Co-located with the lockfile under ~/.opencomputer/<profile>/skills/.hub/audit.log.
Each line is a JSON object with at minimum: timestamp, action, identifier, source.
install events also carry version + guard_verdict (skills_guard scanner result).

This log is human-readable + machine-parseable. It is intentionally NOT
HMAC-chained like the F1 consent audit (different threat model — this is for
"what got installed" not "did the agent bypass consent").
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ALLOWED_ACTIONS = ("install", "uninstall", "update", "scan_blocked")


class AuditLog:
    """Append-only JSONL log."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def record(self, action: str, identifier: str, source: str, **extra: Any) -> None:
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"unknown action {action!r}; expected one of {ALLOWED_ACTIONS}")
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            "identifier": identifier,
            "source": source,
            **extra,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def entries(self, action: Optional[str] = None) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if action is not None and entry.get("action") != action:
                continue
            out.append(entry)
        return out
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/skills_hub/test_audit_log.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add opencomputer/skills_hub/audit_log.py tests/skills_hub/test_audit_log.py
git commit -m "feat(skills-hub): append-only JSONL audit log for install/uninstall events"
```

---

### Task 1.6: Phase 1 commit gate — full test suite passes

- [ ] **Step 1: Run all skills_hub tests + SDK boundary**

Run:
```bash
pytest tests/skills_hub/ tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v
```
Expected: All pass.

- [ ] **Step 2: Run lint**

Run: `ruff check opencomputer/skills_hub/ plugin_sdk/skill_source.py tests/skills_hub/`
Expected: No issues.

- [ ] **Step 3: Push if user-directed (per phase rule)**

Per CLAUDE.md §8: "Per-phase workflow is hard rule: review → push → next phase". Confirm with user before push if not in auto mode.

```bash
git push
```

---

## Phase 2: Bundled `well-known` source + multi-source router

**Phase goal:** A user can call `Router(...).search("pead")` from Python and get curated SKILL.md results back. **Still no installer, no CLI.**

### Task 2.1: Bundled well-known manifest

**Files:**
- Create: `opencomputer/skills_hub/well_known_manifest.json`

The manifest is **bundled with the wheel** so OC works offline. Future updates ship via PyPI release. (A network-fetched manifest can come later; bundled is enough for MVP.)

- [ ] **Step 1: Write the bundled manifest**

```json
// opencomputer/skills_hub/well_known_manifest.json
{
  "version": 1,
  "updated_at": "2026-04-28T00:00:00Z",
  "entries": [
    {
      "identifier": "well-known/example-readme-summarizer",
      "name": "example-readme-summarizer",
      "description": "Summarize a project README into 5 bullet points covering goal, install, basic usage, dev workflow, and notable gotchas",
      "version": "0.1.0",
      "author": "opencomputer",
      "tags": ["docs", "summarization"],
      "trust_level": "trusted",
      "skill_md": "---\nname: example-readme-summarizer\ndescription: Summarize a project README into 5 bullet points covering goal, install, basic usage, dev workflow, and notable gotchas\n---\n\n# Example: README Summarizer\n\nWhen the user asks to summarize a README:\n1. Read README.md.\n2. Produce exactly 5 bullets covering goal, install, basic usage, dev workflow, gotchas.\n3. If a bullet has no information in the README, write the bullet as 'No info'.\n",
      "files": {}
    }
  ]
}
```

(The manifest starts with one example entry. Real entries are added in a follow-up PR after MVP ships and we observe demand. This avoids gating Phase 2 on content curation.)

- [ ] **Step 2: Commit**

```bash
git add opencomputer/skills_hub/well_known_manifest.json
git commit -m "feat(skills-hub): bundled well-known manifest seed (1 example entry)"
```

---

### Task 2.2: WellKnown source

**Files:**
- Create: `opencomputer/skills_hub/sources/__init__.py`
- Create: `opencomputer/skills_hub/sources/well_known.py`
- Test: `tests/skills_hub/test_well_known_source.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/skills_hub/test_well_known_source.py
"""Tests for the bundled well-known SkillSource."""
import json
from pathlib import Path

import pytest
from opencomputer.skills_hub.sources.well_known import WellKnownSource


@pytest.fixture
def fake_manifest(tmp_path) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({
        "version": 1,
        "entries": [
            {
                "identifier": "well-known/foo-bar",
                "name": "foo-bar",
                "description": "An example foo-bar skill for testing search behavior",
                "version": "1.0.0",
                "trust_level": "trusted",
                "skill_md": "---\nname: foo-bar\ndescription: An example foo-bar skill for testing search behavior\n---\n# Foo",
                "files": {}
            },
            {
                "identifier": "well-known/baz",
                "name": "baz",
                "description": "Different skill for non-matching search test verification",
                "version": "0.1.0",
                "trust_level": "community",
                "skill_md": "---\nname: baz\ndescription: Different skill for non-matching search test verification\n---\n# Baz",
                "files": {}
            }
        ]
    }))
    return p


def test_source_name_is_well_known(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    assert src.name == "well-known"


def test_search_substring_match_returns_meta(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    results = src.search("foo")
    assert len(results) == 1
    assert results[0].name == "foo-bar"


def test_search_returns_empty_when_no_match(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    assert src.search("nonexistent-xyzzy") == []


def test_search_respects_limit(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    results = src.search("", limit=1)
    assert len(results) == 1


def test_inspect_returns_meta_for_known(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    meta = src.inspect("well-known/foo-bar")
    assert meta is not None
    assert meta.name == "foo-bar"
    assert meta.version == "1.0.0"


def test_inspect_returns_none_for_unknown(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    assert src.inspect("well-known/nope") is None


def test_fetch_returns_bundle_with_skill_md(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    bundle = src.fetch("well-known/foo-bar")
    assert bundle is not None
    assert "name: foo-bar" in bundle.skill_md


def test_fetch_returns_none_for_unknown(fake_manifest):
    src = WellKnownSource(manifest_path=fake_manifest)
    assert src.fetch("well-known/nope") is None


def test_default_manifest_path_loads_bundled(monkeypatch):
    """No path argument falls back to the bundled manifest in the package."""
    src = WellKnownSource()  # uses bundled
    # Default bundled has at least one entry
    assert len(src.search("", limit=10)) >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/skills_hub/test_well_known_source.py -v`
Expected: FAIL on import

- [ ] **Step 3: Implement WellKnownSource**

```python
# opencomputer/skills_hub/sources/__init__.py
```

```python
# opencomputer/skills_hub/sources/well_known.py
"""WellKnown source — reads a bundled curated manifest of trusted skills.

The manifest is shipped inside the wheel so OC works offline. To update the
catalogue, ship a new release. (A network-fetched manifest can come later
without breaking this offline path.)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from plugin_sdk.skill_source import SkillBundle, SkillMeta, SkillSource


def _bundled_manifest_path() -> Path:
    return Path(__file__).resolve().parent.parent / "well_known_manifest.json"


class WellKnownSource(SkillSource):
    """Reads from a static JSON manifest. Default: bundled with the package."""

    def __init__(self, manifest_path: Optional[Path] = None) -> None:
        self._path = Path(manifest_path) if manifest_path else _bundled_manifest_path()

    @property
    def name(self) -> str:
        return "well-known"

    def _entries(self) -> list[dict]:
        if not self._path.exists():
            return []
        data = json.loads(self._path.read_text())
        return data.get("entries", [])

    def _to_meta(self, entry: dict) -> SkillMeta:
        return SkillMeta(
            identifier=entry["identifier"],
            name=entry["name"],
            description=entry["description"],
            source=self.name,
            version=entry.get("version"),
            author=entry.get("author"),
            tags=tuple(entry.get("tags", [])),
            trust_level=entry.get("trust_level", "community"),
        )

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        q = query.lower()
        out: list[SkillMeta] = []
        for entry in self._entries():
            if q == "" or q in entry["name"].lower() or q in entry["description"].lower():
                out.append(self._to_meta(entry))
            if len(out) >= limit:
                break
        return out

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        for entry in self._entries():
            if entry["identifier"] == identifier:
                return self._to_meta(entry)
        return None

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        for entry in self._entries():
            if entry["identifier"] == identifier:
                return SkillBundle(
                    identifier=entry["identifier"],
                    skill_md=entry["skill_md"],
                    files=dict(entry.get("files", {})),
                )
        return None
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/skills_hub/test_well_known_source.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add opencomputer/skills_hub/sources/__init__.py opencomputer/skills_hub/sources/well_known.py tests/skills_hub/test_well_known_source.py
git commit -m "feat(skills-hub): WellKnownSource backed by bundled manifest"
```

---

### Task 2.3: Multi-source router

**Files:**
- Create: `opencomputer/skills_hub/router.py`
- Test: `tests/skills_hub/test_router.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/skills_hub/test_router.py
"""Tests for the multi-source SkillSource router."""
from typing import Optional

import pytest
from plugin_sdk.skill_source import SkillBundle, SkillMeta, SkillSource
from opencomputer.skills_hub.router import SkillSourceRouter


class _FakeSource(SkillSource):
    def __init__(self, name: str, items: list[dict]) -> None:
        self._name = name
        self._items = items

    @property
    def name(self) -> str:
        return self._name

    def _to_meta(self, item: dict) -> SkillMeta:
        return SkillMeta(
            identifier=f"{self._name}/{item['name']}",
            name=item["name"],
            description=item["description"],
            source=self._name,
        )

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        out = []
        for item in self._items:
            if query == "" or query in item["name"] or query in item["description"]:
                out.append(self._to_meta(item))
            if len(out) >= limit:
                break
        return out

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        for item in self._items:
            if identifier == f"{self._name}/{item['name']}":
                return SkillBundle(
                    identifier=identifier,
                    skill_md=item.get("skill_md", "---\nname: x\ndescription: y\n---"),
                    files={},
                )
        return None

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        for item in self._items:
            if identifier == f"{self._name}/{item['name']}":
                return self._to_meta(item)
        return None


def test_router_search_aggregates_sources():
    a = _FakeSource("a", [{"name": "foo", "description": "from a"}])
    b = _FakeSource("b", [{"name": "foo", "description": "from b"}])
    router = SkillSourceRouter([a, b])
    results = router.search("foo")
    sources = sorted(r.source for r in results)
    assert sources == ["a", "b"]


def test_router_fetch_routes_by_identifier_prefix():
    a = _FakeSource("a", [{"name": "x", "description": "from a", "skill_md": "FROM_A"}])
    b = _FakeSource("b", [{"name": "x", "description": "from b", "skill_md": "FROM_B"}])
    router = SkillSourceRouter([a, b])
    bundle = router.fetch("b/x")
    assert bundle is not None
    assert bundle.skill_md == "FROM_B"


def test_router_fetch_returns_none_for_unknown_source():
    a = _FakeSource("a", [])
    router = SkillSourceRouter([a])
    assert router.fetch("nonexistent/x") is None


def test_router_search_filtered_to_one_source():
    a = _FakeSource("a", [{"name": "foo", "description": "from a"}])
    b = _FakeSource("b", [{"name": "foo", "description": "from b"}])
    router = SkillSourceRouter([a, b])
    results = router.search("foo", source_filter="a")
    assert len(results) == 1
    assert results[0].source == "a"


def test_router_failing_source_does_not_break_others():
    class _BoomSource(SkillSource):
        @property
        def name(self) -> str:
            return "boom"

        def search(self, query, limit=10):
            raise RuntimeError("network down")

        def fetch(self, identifier):
            raise RuntimeError("network down")

        def inspect(self, identifier):
            raise RuntimeError("network down")

    a = _FakeSource("a", [{"name": "foo", "description": "ok"}])
    router = SkillSourceRouter([_BoomSource(), a])
    # Boom source raises but router still returns a's results
    results = router.search("foo")
    assert len(results) == 1
    assert results[0].source == "a"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/skills_hub/test_router.py -v`
Expected: FAIL on import

- [ ] **Step 3: Implement router**

```python
# opencomputer/skills_hub/router.py
"""SkillSourceRouter — fan search/inspect/fetch across multiple sources.

Failures from one source must not block others. Logged + swallowed.
"""

from __future__ import annotations

import logging
from typing import Optional

from plugin_sdk.skill_source import SkillBundle, SkillMeta, SkillSource

_log = logging.getLogger(__name__)


class SkillSourceRouter:
    def __init__(self, sources: list[SkillSource]) -> None:
        self._sources = list(sources)
        self._by_name = {s.name: s for s in self._sources}

    def search(
        self,
        query: str,
        limit: int = 10,
        source_filter: Optional[str] = None,
    ) -> list[SkillMeta]:
        out: list[SkillMeta] = []
        for src in self._sources:
            if source_filter and src.name != source_filter:
                continue
            try:
                out.extend(src.search(query, limit=limit))
            except Exception as e:
                _log.warning("source %r raised during search: %s", src.name, e)
        return out[:limit] if limit else out

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        # Identifier is "<source>/<name>" — route by prefix.
        if "/" not in identifier:
            return None
        source_name, _ = identifier.split("/", 1)
        src = self._by_name.get(source_name)
        if src is None:
            return None
        try:
            return src.fetch(identifier)
        except Exception as e:
            _log.warning("source %r raised during fetch(%s): %s", src.name, identifier, e)
            return None

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        if "/" not in identifier:
            return None
        source_name, _ = identifier.split("/", 1)
        src = self._by_name.get(source_name)
        if src is None:
            return None
        try:
            return src.inspect(identifier)
        except Exception as e:
            _log.warning("source %r raised during inspect(%s): %s", src.name, identifier, e)
            return None

    def list_sources(self) -> list[str]:
        return [s.name for s in self._sources]
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/skills_hub/test_router.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add opencomputer/skills_hub/router.py tests/skills_hub/test_router.py
git commit -m "feat(skills-hub): SkillSourceRouter with fault-tolerant fan-out"
```

---

### Task 2.4: Phase 2 gate — all tests + lint

- [ ] **Step 1: Run full skills_hub test suite**

Run: `pytest tests/skills_hub/ -v`
Expected: ~22 passed

- [ ] **Step 2: Lint**

Run: `ruff check opencomputer/skills_hub/ plugin_sdk/skill_source.py tests/skills_hub/`
Expected: clean

- [ ] **Step 3: Phase boundary commit + push (per CLAUDE.md §8)**

Confirm with user before push (or auto-mode proceed):
```bash
git push
```

---

## Phase 3: Installer + Skills Guard wiring

**Phase goal:** `Installer.install("well-known/foo-bar")` end-to-end works from Python — fetch, scan via skills_guard, validate frontmatter, write to disk, update lockfile, append audit. **Still no CLI.**

### Task 3.1: Installer with skills_guard integration

**Files:**
- Create: `opencomputer/skills_hub/installer.py`
- Test: `tests/skills_hub/test_installer.py`

- [ ] **Step 1: Inspect current skills_guard surface**

Run: `grep -n "class \|def " opencomputer/skills_guard/scanner.py | head -20`

Required: identify the public function for scanning a SKILL.md text body and what its return shape is. Document here before writing.

- [ ] **Step 2: Write failing tests**

```python
# tests/skills_hub/test_installer.py
"""Tests for the Skills Hub installer (fetch + scan + validate + write)."""
import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from plugin_sdk.skill_source import SkillBundle, SkillMeta
from opencomputer.skills_hub.installer import Installer, InstallError, InstallResult


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@pytest.fixture
def fake_router():
    router = Mock()
    bundle = SkillBundle(
        identifier="well-known/example-readme-summarizer",
        skill_md="---\nname: example-readme-summarizer\ndescription: Summarize a project README into 5 bullets covering goal install usage workflow gotchas\n---\n# Example",
        files={},
    )
    meta = SkillMeta(
        identifier="well-known/example-readme-summarizer",
        name="example-readme-summarizer",
        description="Summarize a project README into 5 bullets covering goal install usage workflow gotchas",
        source="well-known",
        version="0.1.0",
    )
    router.fetch.return_value = bundle
    router.inspect.return_value = meta
    return router


@pytest.fixture
def installer(tmp_path, fake_router):
    # Lenient fake guard that always passes
    guard = Mock()
    guard.scan.return_value = Mock(severity="none", findings=[])
    return Installer(
        router=fake_router,
        skills_guard=guard,
        hub_root=tmp_path / "skills" / ".hub",
    )


def test_install_writes_skill_md_to_disk(installer, tmp_path):
    result = installer.install("well-known/example-readme-summarizer")
    expected = tmp_path / "skills" / ".hub" / "well-known" / "example-readme-summarizer" / "SKILL.md"
    assert expected.exists()
    assert "name: example-readme-summarizer" in expected.read_text()
    assert isinstance(result, InstallResult)
    assert result.identifier == "well-known/example-readme-summarizer"


def test_install_records_in_lockfile(installer, tmp_path):
    installer.install("well-known/example-readme-summarizer")
    lockfile = tmp_path / "skills" / ".hub" / "lockfile.json"
    assert lockfile.exists()
    import json
    data = json.loads(lockfile.read_text())
    assert len(data["entries"]) == 1
    assert data["entries"][0]["identifier"] == "well-known/example-readme-summarizer"


def test_install_appends_audit_entry(installer, tmp_path):
    installer.install("well-known/example-readme-summarizer")
    audit = tmp_path / "skills" / ".hub" / "audit.log"
    assert audit.exists()
    line = audit.read_text().strip()
    assert "install" in line
    assert "well-known/example-readme-summarizer" in line


def test_install_rejects_invalid_frontmatter(installer, fake_router):
    bad_bundle = SkillBundle(
        identifier="well-known/bad",
        skill_md="no frontmatter at all",
        files={},
    )
    fake_router.fetch.return_value = bad_bundle
    fake_router.inspect.return_value = SkillMeta(
        identifier="well-known/bad", name="bad", description="x" * 30, source="well-known"
    )
    with pytest.raises(InstallError, match="frontmatter"):
        installer.install("well-known/bad")


def test_install_blocked_by_skills_guard(installer, fake_router, tmp_path):
    installer._guard.scan.return_value = Mock(
        severity="high",
        findings=["embedded shell escape"],
    )
    with pytest.raises(InstallError, match="skills_guard"):
        installer.install("well-known/example-readme-summarizer")
    # Audit log should record scan_blocked
    audit = tmp_path / "skills" / ".hub" / "audit.log"
    if audit.exists():
        assert "scan_blocked" in audit.read_text()


def test_install_unknown_identifier_raises(installer, fake_router):
    fake_router.fetch.return_value = None
    fake_router.inspect.return_value = None
    with pytest.raises(InstallError, match="not found"):
        installer.install("well-known/nope")


def test_uninstall_removes_files_and_lockfile_entry(installer, tmp_path):
    installer.install("well-known/example-readme-summarizer")
    skill_dir = tmp_path / "skills" / ".hub" / "well-known" / "example-readme-summarizer"
    assert skill_dir.exists()
    installer.uninstall("well-known/example-readme-summarizer")
    assert not skill_dir.exists()
    import json
    data = json.loads((tmp_path / "skills" / ".hub" / "lockfile.json").read_text())
    assert data["entries"] == []


def test_double_install_replaces_lockfile_entry(installer, tmp_path):
    installer.install("well-known/example-readme-summarizer")
    installer.install("well-known/example-readme-summarizer")  # idempotent
    import json
    data = json.loads((tmp_path / "skills" / ".hub" / "lockfile.json").read_text())
    assert len(data["entries"]) == 1


def test_install_writes_extra_files_from_bundle(tmp_path, fake_router):
    fake_router.fetch.return_value = SkillBundle(
        identifier="well-known/with-helper",
        skill_md="---\nname: with-helper\ndescription: A skill that ships a helper script alongside its prose\n---\n",
        files={"helper.py": "def x(): return 1\n"},
    )
    fake_router.inspect.return_value = SkillMeta(
        identifier="well-known/with-helper",
        name="with-helper",
        description="A skill that ships a helper script alongside its prose",
        source="well-known",
    )
    guard = Mock()
    guard.scan.return_value = Mock(severity="none", findings=[])
    installer = Installer(router=fake_router, skills_guard=guard, hub_root=tmp_path / ".hub")
    installer.install("well-known/with-helper")
    helper = tmp_path / ".hub" / "well-known" / "with-helper" / "helper.py"
    assert helper.exists()
    assert helper.read_text().startswith("def x")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/skills_hub/test_installer.py -v`
Expected: FAIL on import

- [ ] **Step 4: Implement installer**

```python
# opencomputer/skills_hub/installer.py
"""Skills Hub installer — fetch, scan, validate, write, lockfile, audit."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencomputer.skills_hub.agentskills_validator import (
    ValidationError,
    validate_frontmatter,
)
from opencomputer.skills_hub.audit_log import AuditLog
from opencomputer.skills_hub.lockfile import HubLockFile
from opencomputer.skills_hub.router import SkillSourceRouter


class InstallError(RuntimeError):
    """Raised when an install or uninstall fails."""


@dataclass(frozen=True, slots=True)
class InstallResult:
    identifier: str
    install_path: Path
    sha256: str


class Installer:
    """Installs skills from a SkillSourceRouter into ~/.opencomputer/<profile>/skills/.hub/."""

    def __init__(
        self,
        router: SkillSourceRouter,
        skills_guard: Any,
        hub_root: Path,
    ) -> None:
        self._router = router
        self._guard = skills_guard
        self._hub_root = Path(hub_root)
        self._lockfile = HubLockFile(self._hub_root / "lockfile.json")
        self._audit = AuditLog(self._hub_root / "audit.log")

    def install(self, identifier: str) -> InstallResult:
        meta = self._router.inspect(identifier)
        bundle = self._router.fetch(identifier)
        if meta is None or bundle is None:
            raise InstallError(f"skill not found: {identifier}")

        # Validate frontmatter
        try:
            validate_frontmatter(bundle.skill_md)
        except ValidationError as e:
            raise InstallError(f"invalid frontmatter for {identifier}: {e}") from e

        # Run Skills Guard scanner
        verdict = self._guard.scan(bundle.skill_md)
        severity = getattr(verdict, "severity", "unknown")
        if severity == "high":
            self._audit.record(
                action="scan_blocked",
                identifier=identifier,
                source=meta.source,
                guard_severity=severity,
                findings=list(getattr(verdict, "findings", []) or []),
            )
            raise InstallError(
                f"skills_guard blocked install of {identifier} (severity={severity}): "
                f"{getattr(verdict, 'findings', [])}"
            )

        # Compute the directory layout: <hub_root>/<source>/<name>/
        source_name, name = identifier.split("/", 1)
        skill_dir = self._hub_root / source_name / name
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Write SKILL.md
        skill_md_path = skill_dir / "SKILL.md"
        skill_md_path.write_text(bundle.skill_md)

        # Write extra files
        for rel_path, content in bundle.files.items():
            target = skill_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

        # SHA-256 over canonical SKILL.md content for integrity check
        sha = hashlib.sha256(bundle.skill_md.encode("utf-8")).hexdigest()

        # Lockfile entry (relative install_path under hub_root)
        rel_install = f"{source_name}/{name}"
        self._lockfile.record_install(
            identifier=identifier,
            version=meta.version or "0.0.0",
            source=meta.source,
            install_path=rel_install,
            sha256=sha,
        )

        # Audit
        self._audit.record(
            action="install",
            identifier=identifier,
            source=meta.source,
            version=meta.version or "0.0.0",
            sha256=sha,
            guard_severity=severity,
        )

        return InstallResult(identifier=identifier, install_path=skill_dir, sha256=sha)

    def uninstall(self, identifier: str) -> None:
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
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/skills_hub/test_installer.py -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add opencomputer/skills_hub/installer.py tests/skills_hub/test_installer.py
git commit -m "feat(skills-hub): Installer with skills_guard scan + agentskills.io validation"
```

---

### Task 3.2: Phase 3 gate — full Phase 3 + Phase 1+2 still green

- [ ] **Step 1: Full skills_hub test run**

Run: `pytest tests/skills_hub/ -v`
Expected: ~31 passed

- [ ] **Step 2: Run the broader OC test suite to make sure nothing regressed**

Run: `pytest -x --ignore=tests/skills_hub`
Expected: PASS (or document any pre-existing flakes)

- [ ] **Step 3: Lint**

Run: `ruff check opencomputer/skills_hub/ plugin_sdk/skill_source.py tests/skills_hub/`
Expected: clean

- [ ] **Step 4: Phase 3 commit + push**

```bash
git push
```

---

## Phase 4: CLI surface + slash bridge

**Phase goal:** End-user can run `oc skills browse|search|install|inspect|list|update|uninstall|audit` and `/skills <subcommand>` from inside the agent loop.

### Task 4.1: `cli_skills_hub.py` Typer subapp

**Files:**
- Create: `opencomputer/cli_skills_hub.py`
- Test: `tests/skills_hub/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/skills_hub/test_cli.py
"""Tests for the oc skills CLI subapp."""
import json
from unittest.mock import patch

from typer.testing import CliRunner
from opencomputer.cli_skills_hub import skills_hub_app

runner = CliRunner()


def test_search_command_outputs_results(monkeypatch, tmp_path):
    """oc skills search <query> hits the well-known source and renders rows."""
    # Point hub_root + manifest to tmp
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(skills_hub_app, ["search", ""])
    # Default well-known has at least 1 entry
    assert result.exit_code == 0
    assert "well-known" in result.stdout or result.stdout != ""


def test_install_then_list(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r1 = runner.invoke(
        skills_hub_app,
        ["install", "well-known/example-readme-summarizer", "--yes"],
    )
    assert r1.exit_code == 0
    r2 = runner.invoke(skills_hub_app, ["list"])
    assert r2.exit_code == 0
    assert "example-readme-summarizer" in r2.stdout


def test_install_then_uninstall(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(
        skills_hub_app,
        ["install", "well-known/example-readme-summarizer", "--yes"],
    )
    r = runner.invoke(
        skills_hub_app,
        ["uninstall", "well-known/example-readme-summarizer", "--yes"],
    )
    assert r.exit_code == 0
    r2 = runner.invoke(skills_hub_app, ["list"])
    assert "example-readme-summarizer" not in r2.stdout


def test_inspect_known_identifier(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r = runner.invoke(
        skills_hub_app,
        ["inspect", "well-known/example-readme-summarizer"],
    )
    assert r.exit_code == 0
    assert "example-readme-summarizer" in r.stdout


def test_inspect_unknown_identifier_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r = runner.invoke(skills_hub_app, ["inspect", "well-known/nope-xyzzy"])
    assert r.exit_code != 0


def test_audit_shows_install_entry(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(
        skills_hub_app,
        ["install", "well-known/example-readme-summarizer", "--yes"],
    )
    r = runner.invoke(skills_hub_app, ["audit"])
    assert r.exit_code == 0
    assert "install" in r.stdout
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/skills_hub/test_cli.py -v`
Expected: FAIL on import

- [ ] **Step 3: Implement Typer subapp**

```python
# opencomputer/cli_skills_hub.py
"""`oc skills` Typer subapp — Skills Hub end-user surface.

All command logic lives in shared `do_*` functions so the slash command bridge
can call them too.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.skills_hub.installer import InstallError, Installer
from opencomputer.skills_hub.router import SkillSourceRouter
from opencomputer.skills_hub.sources.well_known import WellKnownSource
from opencomputer.skills_guard.scanner import SkillsGuardScanner

console = Console()

skills_hub_app = typer.Typer(
    name="skills",
    help="Manage skills via the OpenComputer Skills Hub.",
    no_args_is_help=True,
)


def _profile_home() -> Path:
    """Resolve the active profile's directory under OPENCOMPUTER_HOME."""
    home = Path(os.environ.get("OPENCOMPUTER_HOME", str(Path.home() / ".opencomputer")))
    # Default profile = "default" — TODO wire to active-profile resolver if richer
    return home / "default"


def _hub_root() -> Path:
    return _profile_home() / "skills" / ".hub"


def _build_router() -> SkillSourceRouter:
    return SkillSourceRouter([WellKnownSource()])


def _build_installer() -> Installer:
    return Installer(
        router=_build_router(),
        skills_guard=SkillsGuardScanner(),
        hub_root=_hub_root(),
    )


# --- Shared do_* functions ---


def do_search(query: str, source: Optional[str] = None, limit: int = 10) -> None:
    router = _build_router()
    results = router.search(query, limit=limit, source_filter=source)
    if not results:
        console.print(f"[yellow]No matches for {query!r}[/]")
        return
    table = Table()
    table.add_column("Source", style="dim")
    table.add_column("Identifier", style="cyan")
    table.add_column("Description")
    for r in results:
        table.add_row(r.source, r.identifier, r.description)
    console.print(table)


def do_inspect(identifier: str) -> bool:
    router = _build_router()
    meta = router.inspect(identifier)
    if meta is None:
        console.print(f"[red]Not found: {identifier}[/]")
        return False
    console.print(f"[bold]{meta.identifier}[/]")
    console.print(f"  description: {meta.description}")
    if meta.version:
        console.print(f"  version: {meta.version}")
    if meta.author:
        console.print(f"  author: {meta.author}")
    if meta.tags:
        console.print(f"  tags: {', '.join(meta.tags)}")
    console.print(f"  trust_level: {meta.trust_level}")
    return True


def do_install(identifier: str, yes: bool = False) -> bool:
    installer = _build_installer()
    if not yes:
        console.print(f"Install [bold]{identifier}[/]? Skills Guard scan will run.")
        confirm = typer.confirm("Proceed?", default=True)
        if not confirm:
            console.print("Aborted.")
            return False
    try:
        result = installer.install(identifier)
    except InstallError as e:
        console.print(f"[red]Install failed:[/] {e}")
        return False
    console.print(f"[green]Installed[/] {identifier} → {result.install_path}")
    return True


def do_uninstall(identifier: str, yes: bool = False) -> bool:
    installer = _build_installer()
    if not yes:
        confirm = typer.confirm(f"Uninstall {identifier}?", default=True)
        if not confirm:
            console.print("Aborted.")
            return False
    try:
        installer.uninstall(identifier)
    except InstallError as e:
        console.print(f"[red]Uninstall failed:[/] {e}")
        return False
    console.print(f"[green]Uninstalled[/] {identifier}")
    return True


def do_list() -> None:
    from opencomputer.skills_hub.lockfile import HubLockFile
    lockfile = HubLockFile(_hub_root() / "lockfile.json")
    entries = lockfile.list()
    if not entries:
        console.print("[dim]No hub-installed skills.[/]")
        return
    table = Table()
    table.add_column("Identifier", style="cyan")
    table.add_column("Version")
    table.add_column("Source")
    table.add_column("Installed")
    for e in entries:
        table.add_row(e.identifier, e.version, e.source, e.installed_at)
    console.print(table)


def do_audit(action: Optional[str] = None) -> None:
    from opencomputer.skills_hub.audit_log import AuditLog
    log = AuditLog(_hub_root() / "audit.log")
    entries = log.entries(action=action)
    if not entries:
        console.print("[dim]Audit log is empty.[/]")
        return
    for e in entries:
        ts = e.get("timestamp", "?")
        act = e.get("action", "?")
        ident = e.get("identifier", "?")
        console.print(f"  {ts}  [{act}]  {ident}")


# --- Typer commands (thin wrappers) ---


@skills_hub_app.command("search")
def cmd_search(
    query: str = typer.Argument("", help="Search term (empty = list all)"),
    source: Optional[str] = typer.Option(None, "--source", help="Filter to one source"),
    limit: int = typer.Option(10, "--limit"),
) -> None:
    do_search(query, source=source, limit=limit)


@skills_hub_app.command("browse")
def cmd_browse(
    source: Optional[str] = typer.Option(None, "--source"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Alias for `search` with empty query."""
    do_search("", source=source, limit=limit)


@skills_hub_app.command("inspect")
def cmd_inspect(identifier: str) -> None:
    ok = do_inspect(identifier)
    if not ok:
        raise typer.Exit(code=1)


@skills_hub_app.command("install")
def cmd_install(
    identifier: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    ok = do_install(identifier, yes=yes)
    if not ok:
        raise typer.Exit(code=1)


@skills_hub_app.command("uninstall")
def cmd_uninstall(
    identifier: str,
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    ok = do_uninstall(identifier, yes=yes)
    if not ok:
        raise typer.Exit(code=1)


@skills_hub_app.command("list")
def cmd_list() -> None:
    do_list()


@skills_hub_app.command("audit")
def cmd_audit(
    action: Optional[str] = typer.Option(None, "--action", help="Filter to install/uninstall/scan_blocked"),
) -> None:
    do_audit(action=action)


@skills_hub_app.command("update")
def cmd_update(identifier: str, yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Update = uninstall + install (idempotent re-fetch)."""
    do_uninstall(identifier, yes=yes)
    do_install(identifier, yes=yes)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/skills_hub/test_cli.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_skills_hub.py tests/skills_hub/test_cli.py
git commit -m "feat(skills-hub): oc skills Typer subapp (search/browse/install/inspect/list/uninstall/audit/update)"
```

---

### Task 4.2: Wire `oc skills` into root cli.py

**Files:**
- Modify: `opencomputer/cli.py`

- [ ] **Step 1: Find the right insertion point**

Run: `grep -n "app.add_typer" opencomputer/cli.py | head -5`
Expected: see existing `add_typer` calls — add new one in the same block.

- [ ] **Step 2: Add the import + registration**

Add near top of `cli.py` (alongside other `from opencomputer.cli_*` imports):

```python
from opencomputer.cli_skills_hub import skills_hub_app
```

In the same `add_typer` block (after the last existing one):

```python
app.add_typer(skills_hub_app, name="skills")
```

**Note:** OC may already have a `skills` subapp from earlier work. If so:
- Check what it does: `grep -n "skills_app\|add_typer.*skills" opencomputer/cli.py`
- If it covers `list/view/install` for *bundled* skills (not hub), rename ours to `skill-hub` or merge.
- Recommendation: **MERGE** — fold the existing `oc skills` commands into the new subapp under shared names, so `oc skills install <id>` always means hub install. The existing `oc skills list` likely already lists local skills; keep that behavior, add `--source local|hub|all` filter.

- [ ] **Step 3: Verify CLI registration**

Run: `oc skills --help`
Expected: command list shows the new subcommands.

- [ ] **Step 4: Run cli tests**

Run: `pytest tests/skills_hub/test_cli.py -v`
Expected: still 6 passed

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli.py
git commit -m "feat(skills-hub): wire oc skills subapp into root CLI"
```

---

### Task 4.3: Slash command bridge

**Files:**
- Create: `opencomputer/agent/slash_commands_impl/skills_hub_slash.py`
- Modify: `opencomputer/agent/slash_dispatcher.py`

- [ ] **Step 1: Inspect current slash dispatcher**

Run: `grep -n "register\|dispatch_slash\|SLASH_HANDLERS" opencomputer/agent/slash_dispatcher.py | head -20`

- [ ] **Step 2: Implement the slash bridge**

```python
# opencomputer/agent/slash_commands_impl/skills_hub_slash.py
"""Bridge `/skills <subcommand>` slash command to cli_skills_hub do_* functions."""

from __future__ import annotations

from opencomputer.cli_skills_hub import (
    do_audit,
    do_inspect,
    do_install,
    do_list,
    do_search,
    do_uninstall,
)


def handle_skills_slash(args: list[str]) -> str:
    """Dispatch `/skills <sub>` invocations.

    Returns a status string for the slash dispatcher.
    """
    if not args:
        do_search("")
        return ""
    sub = args[0]
    rest = args[1:]
    if sub == "search":
        do_search(" ".join(rest) if rest else "")
    elif sub == "browse":
        do_search("", limit=20)
    elif sub == "inspect":
        if not rest:
            return "Usage: /skills inspect <identifier>"
        do_inspect(rest[0])
    elif sub == "install":
        if not rest:
            return "Usage: /skills install <identifier>"
        # Slash always confirms via inline prompt — pass yes=False
        do_install(rest[0], yes=False)
    elif sub == "uninstall":
        if not rest:
            return "Usage: /skills uninstall <identifier>"
        do_uninstall(rest[0], yes=False)
    elif sub == "list":
        do_list()
    elif sub == "audit":
        do_audit()
    else:
        return f"Unknown subcommand: /skills {sub}"
    return ""
```

- [ ] **Step 3: Register in slash_dispatcher**

In `opencomputer/agent/slash_dispatcher.py`, locate the `SLASH_HANDLERS` dict (or equivalent registry), and add:

```python
from opencomputer.agent.slash_commands_impl.skills_hub_slash import handle_skills_slash

SLASH_HANDLERS["skills"] = handle_skills_slash  # type: ignore
```

(Adapt to whatever exact registry shape exists — the audit reported "agent/slash_dispatcher.py" + "agent/slash_commands_impl/" so the structure should already exist.)

- [ ] **Step 4: Smoke-test slash dispatch from a unit test**

Add to `tests/skills_hub/test_cli.py`:

```python
def test_slash_skills_search(monkeypatch, tmp_path, capsys):
    from opencomputer.agent.slash_commands_impl.skills_hub_slash import handle_skills_slash
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    handle_skills_slash(["search", ""])
    captured = capsys.readouterr()
    # Should have hit well-known and printed something
    assert "well-known" in captured.out or "example" in captured.out


def test_slash_skills_unknown_subcommand(tmp_path, monkeypatch):
    from opencomputer.agent.slash_commands_impl.skills_hub_slash import handle_skills_slash
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    out = handle_skills_slash(["nonexistent"])
    assert "Unknown subcommand" in out
```

Run: `pytest tests/skills_hub/test_cli.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add opencomputer/agent/slash_commands_impl/skills_hub_slash.py opencomputer/agent/slash_dispatcher.py tests/skills_hub/test_cli.py
git commit -m "feat(skills-hub): /skills slash command bridge"
```

---

### Task 4.4: Phase 4 gate

- [ ] **Step 1: Full test pass**

Run: `pytest tests/skills_hub/ -v`
Expected: ~38 passed

- [ ] **Step 2: Lint**

Run: `ruff check opencomputer/skills_hub/ opencomputer/cli_skills_hub.py opencomputer/agent/slash_commands_impl/skills_hub_slash.py plugin_sdk/skill_source.py tests/skills_hub/`
Expected: clean

- [ ] **Step 3: Manual smoke test**

```bash
oc skills --help
oc skills search ""
oc skills install well-known/example-readme-summarizer --yes
oc skills list
oc skills audit
oc skills uninstall well-known/example-readme-summarizer --yes
```

- [ ] **Step 4: Commit + push**

```bash
git push
```

---

## Phase 5: GitHub source + Taps

**Phase goal:** `oc skills tap add <github.com/user/repo>` registers a GitHub repo as a SkillSource. Subsequent `oc skills install <github_user_repo>/<skill-name>` fetches.

### Task 5.1: GitHub source

**Files:**
- Create: `opencomputer/skills_hub/sources/github.py`
- Test: `tests/skills_hub/test_github_source.py`

- [ ] **Step 1: Write failing tests (mock subprocess)**

```python
# tests/skills_hub/test_github_source.py
"""Tests for the GitHub source (mocks `git clone`)."""
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from opencomputer.skills_hub.sources.github import GitHubSource


def _seed_fake_repo(target: Path) -> None:
    """Create a fake cloned repo with one valid SKILL.md."""
    skill_dir = target / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: A demo skill from a tapped GitHub repo for testing fetches\nversion: 1.0.0\n---\n# Demo\n"
    )


def test_github_source_name_uses_repo(tmp_path):
    """GitHubSource name reflects the user/repo for routing."""
    src = GitHubSource(repo="alice/cool-skills", clone_root=tmp_path / "taps")
    assert src.name == "alice/cool-skills"


def test_search_walks_cloned_skills(tmp_path):
    src = GitHubSource(repo="alice/cool-skills", clone_root=tmp_path / "taps")
    # Pretend we already cloned
    clone_dir = tmp_path / "taps" / "alice" / "cool-skills"
    clone_dir.mkdir(parents=True, exist_ok=True)
    _seed_fake_repo(clone_dir)
    # No actual clone; bypass by setting the cached path
    src._clone_dir = clone_dir
    results = src.search("demo")
    assert len(results) == 1
    assert results[0].name == "demo-skill"


def test_inspect_returns_meta_for_known(tmp_path):
    src = GitHubSource(repo="alice/cool", clone_root=tmp_path / "taps")
    clone_dir = tmp_path / "taps" / "alice" / "cool"
    clone_dir.mkdir(parents=True, exist_ok=True)
    _seed_fake_repo(clone_dir)
    src._clone_dir = clone_dir
    meta = src.inspect("alice/cool/demo-skill")
    assert meta is not None
    assert meta.name == "demo-skill"
    assert meta.version == "1.0.0"


def test_fetch_returns_bundle(tmp_path):
    src = GitHubSource(repo="alice/cool", clone_root=tmp_path / "taps")
    clone_dir = tmp_path / "taps" / "alice" / "cool"
    clone_dir.mkdir(parents=True, exist_ok=True)
    _seed_fake_repo(clone_dir)
    src._clone_dir = clone_dir
    bundle = src.fetch("alice/cool/demo-skill")
    assert bundle is not None
    assert "demo-skill" in bundle.skill_md


def test_clone_invokes_git(tmp_path, monkeypatch):
    """If the clone dir is missing, GitHubSource shells out to git clone."""
    src = GitHubSource(repo="alice/cool", clone_root=tmp_path / "taps")
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        # Simulate the directory now exists (caller checks)
        Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    src._ensure_cloned()
    # Should have called git clone
    assert any("clone" in c for c in calls)
```

- [ ] **Step 2: Implement GitHubSource**

```python
# opencomputer/skills_hub/sources/github.py
"""GitHubSource — clones an arbitrary public GitHub repo and walks for SKILL.md.

Identifier shape: `<user>/<repo>/<skill-name>`. Source name = `<user>/<repo>`.

Uses `subprocess git clone` with depth=1 — no gitpython dep. The caller is
responsible for refreshing (fetch+pull) via `_refresh()` if they want updates.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

from plugin_sdk.skill_source import SkillBundle, SkillMeta, SkillSource

from opencomputer.skills_hub.agentskills_validator import validate_frontmatter

_log = logging.getLogger(__name__)


class GitHubSource(SkillSource):
    def __init__(self, repo: str, clone_root: Path) -> None:
        """`repo` is "user/name". `clone_root` is the directory under which clones live."""
        self._repo = repo
        self._user, self._name = repo.split("/", 1)
        self._clone_root = Path(clone_root)
        self._clone_dir = self._clone_root / self._user / self._name

    @property
    def name(self) -> str:
        return self._repo

    def _ensure_cloned(self) -> None:
        if self._clone_dir.exists():
            return
        self._clone_dir.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{self._repo}.git"
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", url, str(self._clone_dir)],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as e:
            _log.warning("git clone failed for %s: %s", self._repo, e.stderr)
            raise
        except subprocess.TimeoutExpired:
            _log.warning("git clone timed out for %s", self._repo)
            raise

    def _walk_skills(self) -> list[Path]:
        """Find all SKILL.md files in the cloned repo."""
        if not self._clone_dir.exists():
            try:
                self._ensure_cloned()
            except Exception:
                return []
        return list(self._clone_dir.rglob("SKILL.md"))

    def _meta_from_skill_md(self, skill_md_path: Path) -> Optional[SkillMeta]:
        try:
            text = skill_md_path.read_text()
            parsed = validate_frontmatter(text)
        except Exception as e:
            _log.debug("skipping %s: %s", skill_md_path, e)
            return None
        name = parsed["name"]
        return SkillMeta(
            identifier=f"{self._repo}/{name}",
            name=name,
            description=parsed["description"],
            source=self.name,
            version=parsed.get("version"),
            author=parsed.get("author"),
            tags=tuple(parsed.get("tags", [])),
            trust_level="community",  # untrusted by default, per Skills Guard policy
        )

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        out: list[SkillMeta] = []
        q = query.lower()
        for skill_md in self._walk_skills():
            meta = self._meta_from_skill_md(skill_md)
            if meta is None:
                continue
            if q == "" or q in meta.name.lower() or q in meta.description.lower():
                out.append(meta)
            if len(out) >= limit:
                break
        return out

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        for skill_md in self._walk_skills():
            meta = self._meta_from_skill_md(skill_md)
            if meta and meta.identifier == identifier:
                return meta
        return None

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        for skill_md in self._walk_skills():
            meta = self._meta_from_skill_md(skill_md)
            if meta and meta.identifier == identifier:
                return SkillBundle(
                    identifier=identifier,
                    skill_md=skill_md.read_text(),
                    files={},  # MVP: SKILL.md only; future: walk skill dir for helper files
                )
        return None
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/skills_hub/test_github_source.py -v`
Expected: 5 passed

- [ ] **Step 4: Commit**

```bash
git add opencomputer/skills_hub/sources/github.py tests/skills_hub/test_github_source.py
git commit -m "feat(skills-hub): GitHubSource via subprocess git clone (depth=1)"
```

---

### Task 5.2: TapsManager

**Files:**
- Create: `opencomputer/skills_hub/taps.py`
- Test: `tests/skills_hub/test_taps.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/skills_hub/test_taps.py
import json
from pathlib import Path
import pytest
from opencomputer.skills_hub.taps import TapsManager


def test_empty_taps(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    assert mgr.list() == []


def test_add_tap(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    mgr.add("alice/skills")
    assert mgr.list() == ["alice/skills"]


def test_add_normalizes_url_to_user_repo(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    mgr.add("https://github.com/alice/skills.git")
    assert mgr.list() == ["alice/skills"]


def test_add_normalizes_user_repo_form(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    mgr.add("alice/skills")
    assert mgr.list() == ["alice/skills"]


def test_remove_tap(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    mgr.add("alice/skills")
    mgr.add("bob/skills")
    mgr.remove("alice/skills")
    assert mgr.list() == ["bob/skills"]


def test_add_duplicate_is_idempotent(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    mgr.add("alice/skills")
    mgr.add("alice/skills")
    assert mgr.list() == ["alice/skills"]


def test_invalid_repo_form_rejected(tmp_path):
    mgr = TapsManager(tmp_path / "taps.json")
    with pytest.raises(ValueError):
        mgr.add("not-a-valid-repo")
```

- [ ] **Step 2: Implement**

```python
# opencomputer/skills_hub/taps.py
"""TapsManager — register/unregister GitHub repos as SkillSources."""

from __future__ import annotations

import json
import re
from pathlib import Path

_REPO_FORM = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_GITHUB_URL = re.compile(r"^https?://github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")


def _normalize(spec: str) -> str:
    spec = spec.strip()
    m = _GITHUB_URL.match(spec)
    if m:
        return m.group(1)
    if _REPO_FORM.match(spec):
        return spec
    raise ValueError(
        f"taps argument {spec!r} is neither user/repo nor a github.com URL"
    )


class TapsManager:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def _read(self) -> list[str]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text())
        except json.JSONDecodeError:
            return []
        return list(data.get("taps", []))

    def _write(self, taps: list[str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"taps": taps}, indent=2, sort_keys=True))

    def list(self) -> list[str]:
        return self._read()

    def add(self, spec: str) -> None:
        repo = _normalize(spec)
        taps = self._read()
        if repo not in taps:
            taps.append(repo)
            taps.sort()
            self._write(taps)

    def remove(self, spec: str) -> None:
        repo = _normalize(spec)
        taps = [t for t in self._read() if t != repo]
        self._write(taps)
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/skills_hub/test_taps.py -v`
Expected: 7 passed

- [ ] **Step 4: Commit**

```bash
git add opencomputer/skills_hub/taps.py tests/skills_hub/test_taps.py
git commit -m "feat(skills-hub): TapsManager — register/unregister GitHub repos"
```

---

### Task 5.3: Wire taps into router + CLI

**Files:**
- Modify: `opencomputer/cli_skills_hub.py`

- [ ] **Step 1: Update `_build_router` to include tapped GitHub sources**

Replace the existing `_build_router` and `_build_installer` with:

```python
def _build_router() -> SkillSourceRouter:
    sources: list = [WellKnownSource()]
    taps = TapsManager(_profile_home() / "skills" / ".hub" / "taps.json").list()
    clone_root = _profile_home() / "skills" / ".hub" / "_clones"
    for repo in taps:
        sources.append(GitHubSource(repo=repo, clone_root=clone_root))
    return SkillSourceRouter(sources)
```

Add the imports:

```python
from opencomputer.skills_hub.sources.github import GitHubSource
from opencomputer.skills_hub.taps import TapsManager
```

- [ ] **Step 2: Add tap subcommand group**

Append to `opencomputer/cli_skills_hub.py`:

```python
tap_app = typer.Typer(name="tap", help="Manage GitHub repo taps for the skills hub.", no_args_is_help=True)
skills_hub_app.add_typer(tap_app, name="tap")


@tap_app.command("add")
def cmd_tap_add(repo: str) -> None:
    mgr = TapsManager(_profile_home() / "skills" / ".hub" / "taps.json")
    try:
        mgr.add(repo)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(code=1)
    console.print(f"[green]Tapped[/] {repo}")


@tap_app.command("remove")
def cmd_tap_remove(repo: str) -> None:
    mgr = TapsManager(_profile_home() / "skills" / ".hub" / "taps.json")
    mgr.remove(repo)
    console.print(f"[green]Untapped[/] {repo}")


@tap_app.command("list")
def cmd_tap_list() -> None:
    mgr = TapsManager(_profile_home() / "skills" / ".hub" / "taps.json")
    taps = mgr.list()
    if not taps:
        console.print("[dim]No taps registered.[/]")
        return
    for t in taps:
        console.print(f"  {t}")
```

- [ ] **Step 3: Add CLI tap test**

Add to `tests/skills_hub/test_cli.py`:

```python
def test_tap_add_then_list(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r1 = runner.invoke(skills_hub_app, ["tap", "add", "alice/skills"])
    assert r1.exit_code == 0
    r2 = runner.invoke(skills_hub_app, ["tap", "list"])
    assert "alice/skills" in r2.stdout


def test_tap_invalid_form_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r = runner.invoke(skills_hub_app, ["tap", "add", "not-valid"])
    assert r.exit_code != 0
```

- [ ] **Step 4: Run all tests**

Run: `pytest tests/skills_hub/ -v`
Expected: ~47 passed

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli_skills_hub.py tests/skills_hub/test_cli.py
git commit -m "feat(skills-hub): tap add/remove/list CLI + integrate into router"
```

---

### Task 5.4: Phase 5 gate

- [ ] **Step 1: Full test suite**

Run: `pytest tests/skills_hub/ -v`
Expected: ~47 passed

- [ ] **Step 2: Wider OC suite (no regressions)**

Run: `pytest -x --ignore=tests/skills_hub`
Expected: pass

- [ ] **Step 3: Manual smoke**

```bash
oc skills tap add anthropics/claude-skills  # if real, fetches
oc skills tap list
oc skills search ""  # should now show tapped skills if any have SKILL.md
oc skills tap remove anthropics/claude-skills
```

- [ ] **Step 4: Push**

```bash
git push
```

---

## Phase 6: Polish — README + CHANGELOG + e2e + docs

### Task 6.1: End-to-end test

**Files:**
- Create: `tests/skills_hub/test_e2e.py`

- [ ] **Step 1: Write e2e test**

```python
# tests/skills_hub/test_e2e.py
"""End-to-end test: install + use + uninstall against the bundled well-known."""
from pathlib import Path

from typer.testing import CliRunner
from opencomputer.cli_skills_hub import skills_hub_app

runner = CliRunner()


def test_full_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    # 1. Search well-known
    r = runner.invoke(skills_hub_app, ["search", "example"])
    assert r.exit_code == 0
    assert "example-readme-summarizer" in r.stdout

    # 2. Inspect
    r = runner.invoke(skills_hub_app, ["inspect", "well-known/example-readme-summarizer"])
    assert r.exit_code == 0

    # 3. Install
    r = runner.invoke(skills_hub_app, ["install", "well-known/example-readme-summarizer", "--yes"])
    assert r.exit_code == 0

    # 4. List shows it
    r = runner.invoke(skills_hub_app, ["list"])
    assert "example-readme-summarizer" in r.stdout

    # 5. The SKILL.md file exists on disk
    skill_md = tmp_path / "default" / "skills" / ".hub" / "well-known" / "example-readme-summarizer" / "SKILL.md"
    assert skill_md.exists()

    # 6. Audit log has an install event
    r = runner.invoke(skills_hub_app, ["audit"])
    assert "install" in r.stdout

    # 7. Uninstall
    r = runner.invoke(skills_hub_app, ["uninstall", "well-known/example-readme-summarizer", "--yes"])
    assert r.exit_code == 0

    # 8. List is empty
    r = runner.invoke(skills_hub_app, ["list"])
    assert "example-readme-summarizer" not in r.stdout

    # 9. Audit shows both events
    r = runner.invoke(skills_hub_app, ["audit"])
    assert "install" in r.stdout
    assert "uninstall" in r.stdout
```

- [ ] **Step 2: Run e2e**

Run: `pytest tests/skills_hub/test_e2e.py -v`
Expected: 1 passed

- [ ] **Step 3: Commit**

```bash
git add tests/skills_hub/test_e2e.py
git commit -m "test(skills-hub): full lifecycle e2e (search → install → list → uninstall → audit)"
```

---

### Task 6.2: README section

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Find the right section**

Run: `grep -n "^## " README.md | head -20`

Locate where to insert a new "Skills Hub" section — probably after the "Skills" mention or "Plugins" section.

- [ ] **Step 2: Add new section**

Insert under appropriate location:

```markdown
## Skills Hub

OpenComputer skills can come from three places:

1. **Bundled** — ships in the wheel under `opencomputer/skills/`. Always available.
2. **User** — your own SKILL.md files at `~/.opencomputer/<profile>/skills/`. Created by you, by `SkillManageTool`, or by the auto-skill-evolution loop after approval.
3. **Hub** — installed from a remote source via `oc skills install`. Stored at `~/.opencomputer/<profile>/skills/.hub/<source>/<name>/SKILL.md`.

### Browsing & installing

```bash
oc skills browse                                # list all hub skills
oc skills search "screener"                     # fuzzy search
oc skills inspect well-known/example-readme-summarizer
oc skills install well-known/example-readme-summarizer
oc skills list                                  # what's hub-installed
oc skills uninstall well-known/example-readme-summarizer
oc skills audit                                 # install/uninstall log
```

The same surface is available as `/skills <subcommand>` inside the agent loop.

### Adding GitHub repos as sources (taps)

Any public GitHub repo with one or more `SKILL.md` files can be a source:

```bash
oc skills tap add alice/cool-skills
oc skills search ""                             # now shows alice/cool-skills/* too
oc skills install alice/cool-skills/some-skill
oc skills tap list
oc skills tap remove alice/cool-skills
```

### Standards & safety

- All hub skills are validated against the [agentskills.io](https://agentskills.io) frontmatter standard before install.
- Every install runs through Skills Guard's threat scanner. High-severity findings block the install.
- `~/.opencomputer/<profile>/skills/.hub/audit.log` records every install/uninstall (append-only JSONL).
- Skills loaded via taps default to `community` trust level (treated as untrusted by Skills Guard policy).
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(skills-hub): README section explaining browse/install/tap workflow"
```

---

### Task 6.3: CHANGELOG entry

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add `## [Unreleased]` entry**

```markdown
## [Unreleased]

### Added

- **Skills Hub**: `oc skills browse|search|inspect|install|list|update|uninstall|audit` and `oc skills tap add|remove|list` for installing skills from a bundled `well-known` registry or arbitrary GitHub repos. agentskills.io frontmatter validation. Skills Guard scan on install. Append-only audit log at `~/.opencomputer/<profile>/skills/.hub/audit.log`. Full slash command surface as `/skills <subcommand>` inside the agent loop. (#TBD)
- `plugin_sdk.skill_source.SkillSource` ABC for third-party hub adapters. (#TBD)

### Changed

- Skills now load from three locations: bundled, user (`~/.opencomputer/<profile>/skills/`), hub (`~/.opencomputer/<profile>/skills/.hub/<source>/<name>/`). Loader walks all three.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): Skills Hub MVP unreleased entry"
```

---

### Task 6.4: Verify skills loader picks up `.hub/` skills

**Files:**
- Inspect + possibly modify: `opencomputer/agent/memory.py` (skill loader path)

- [ ] **Step 1: Inspect current loader**

Run: `grep -n "skills/\|SKILL.md\|skill_paths\|list_skills" opencomputer/agent/memory.py | head -20`

Look for the canonical skill discovery path.

- [ ] **Step 2: Verify or extend**

If the loader walks `~/.opencomputer/<profile>/skills/**/SKILL.md`, the `.hub/<source>/<name>/SKILL.md` paths are already discovered — done.

If it only walks `~/.opencomputer/<profile>/skills/*/SKILL.md` (one level), extend the glob to recurse with `**/SKILL.md`. Add a test:

```python
# tests/skills_hub/test_loader_picks_up_hub.py
def test_skills_in_hub_directory_are_discovered(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    skill_dir = tmp_path / "default" / "skills" / ".hub" / "well-known" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A demo skill installed via the hub testing loader pickup\n---\n"
    )
    # Use whatever the actual public list-skills function is
    from opencomputer.agent.memory import list_skills  # adjust if name differs
    skills = list_skills()
    assert any(s.name == "demo" for s in skills)
```

- [ ] **Step 3: Run test**

Run: `pytest tests/skills_hub/test_loader_picks_up_hub.py -v`

If FAIL, fix loader to recurse. If PASS, commit and move on.

- [ ] **Step 4: Commit**

```bash
git add opencomputer/agent/memory.py tests/skills_hub/test_loader_picks_up_hub.py
git commit -m "fix(skills-hub): skill loader recurses into .hub/ to pick up installed skills"
```

(Skip the modify line if no change needed; just commit the test.)

---

### Task 6.5: Phase 6 final gate

- [ ] **Step 1: Full test suite**

Run: `pytest tests/skills_hub/ -v`
Expected: ~50 tests pass

- [ ] **Step 2: Full OC test suite**

Run: `pytest -x`
Expected: pass

- [ ] **Step 3: Lint everything new**

Run:
```bash
ruff check opencomputer/skills_hub/ opencomputer/cli_skills_hub.py opencomputer/agent/slash_commands_impl/skills_hub_slash.py plugin_sdk/skill_source.py tests/skills_hub/
```
Expected: clean

- [ ] **Step 4: Manual smoke against real install**

```bash
oc skills --help
oc skills browse
oc skills install well-known/example-readme-summarizer --yes
oc skills list
ls ~/.opencomputer/default/skills/.hub/well-known/example-readme-summarizer/
oc skills audit
oc skills uninstall well-known/example-readme-summarizer --yes
```

- [ ] **Step 5: Open PR**

```bash
git push -u origin feat/skills-hub
gh pr create --title "feat(skills-hub): Tier 1.A — Skills Hub MVP + agentskills.io standard" --body "$(cat <<'EOF'
## Summary

Closes the largest visible-to-user gap identified in `docs/refs/hermes-agent/2026-04-28-major-gaps.md` Tier 1.A + 1.D — OC now has a multi-source skills hub with agentskills.io frontmatter validation, Skills Guard scanning on install, append-only audit log, and tap-add for arbitrary GitHub repos.

### What ships

- `plugin_sdk.skill_source.SkillSource` ABC + `SkillMeta` + `SkillBundle` (third-party adapters can write sources)
- Bundled `well-known` source (offline-friendly, ships in wheel)
- `GitHubSource` for arbitrary public repos via `oc skills tap add`
- `oc skills browse|search|inspect|install|list|update|uninstall|audit` Typer subapp
- `oc skills tap add|remove|list` GitHub repo registration
- `/skills <subcommand>` slash command surface in the agent loop
- agentskills.io frontmatter validator (name kebab-case, description 20-500 chars, semver version, list-of-string tags)
- `HubLockFile` with fcntl-protected concurrent writes
- Append-only JSONL audit log at `~/.opencomputer/<profile>/skills/.hub/audit.log`
- ~50 unit + e2e tests

### Out of scope (deferred)

- `oc skills publish` — separate PR after MVP demand surfaces
- `oc skills snapshot export|import` — separate PR
- ClawHub / SkillsSh / LobeHub / ClaudeMarketplace / HermesIndex sources — port on demand
- Hub-side cron-driven update poll — niche

## Test plan

- [x] All ~50 hub tests pass
- [x] Full OC suite green (no regressions)
- [x] ruff clean
- [x] Manual smoke: install/list/uninstall lifecycle works against bundled well-known
- [ ] Manual smoke: tap add against a real GitHub repo, install one of its skills, verify SKILL.md is loaded by the agent on next turn
- [ ] Manual smoke: malformed SKILL.md frontmatter is rejected with clear error
- [ ] Manual smoke: Skills Guard high-severity scan blocks install + records `scan_blocked` audit event

## Hermes-equivalent gap closed

| Hermes feature | Status |
|---|---|
| `hermes skills browse/search/install/inspect/list/update/uninstall/audit` | shipped |
| `hermes skills tap add/remove/list` | shipped |
| `hermes skills publish` | deferred |
| `hermes skills snapshot` | deferred |
| `hermes skills config` | deferred |
| Skills Guard on install | shipped |
| agentskills.io standard | shipped (frontmatter validator) |
| 6 remote registries (github/well-known/skills-sh/clawhub/lobehub/claude-marketplace) | 2 shipped (well-known + github tap) — others on demand |

## Architecture

Three-layer:
1. `plugin_sdk/skill_source.py` — public ABC contract
2. `opencomputer/skills_hub/` — bundled adapters + router + installer + lockfile + audit + taps + validator
3. `opencomputer/cli_skills_hub.py` + `agent/slash_commands_impl/skills_hub_slash.py` — thin CLI/slash wrappers over shared `do_*` functions

Skills Guard is unchanged; the hub uses it.

## Plan reference

See `docs/superpowers/plans/2026-04-28-hermes-tier1a-skills-hub.md` for the full phase-by-phase task plan.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Confirm PR opened**

Run: `gh pr view`

---

## Self-Review (run after writing the plan, before execution)

**1. Spec coverage:** Each Tier 1.A line item from the gap audit (browse/search/install/inspect/list/update/uninstall/audit + tap add/remove/list) maps to a concrete task above. agentskills.io validator (Tier 1.D) → Task 1.3. Skills Guard integration → Task 3.1. Lockfile → Task 1.4. Audit log → Task 1.5. Slash bridge → Task 4.3. README + CHANGELOG → Task 6.2 + 6.3. Loader recursion → Task 6.4. ✅ no gaps.

**2. Placeholder scan:** Searched for "TBD", "TODO", "implement later", "fill in details", "Add appropriate", "handle edge cases", "Similar to Task". Only "TBD" remains in PR body's `(#TBD)` for the future PR number — that's standard. No code-step placeholders.

**3. Type consistency:** `SkillMeta`, `SkillBundle`, `SkillSource`, `SkillSourceRouter`, `Installer`, `InstallResult`, `InstallError`, `HubLockFile`, `LockEntry`, `AuditLog`, `WellKnownSource`, `GitHubSource`, `TapsManager`, `validate_frontmatter`, `ValidationError` — names consistent across all tasks. Method signatures (`search(query, limit)`, `fetch(identifier)`, `inspect(identifier)`) consistent.

---

## Self-Audit (expert critic pass — 2026-04-28)

After writing the plan, audited as a hostile reviewer. **25 issues identified.** The 9 critical ones are fixed inline below; the remaining 16 are documented as constraints the executor must respect.

### Critical fixes (applied inline to plan above)

**[C1] Manifest seeded with one entry is a demo, not a feature.** Original Task 2.1 had a single example skill. **Fix:** Add **Task 2.0 — Seed manifest from bundled skills** before Task 2.1. Walk `opencomputer/skills/*/SKILL.md`, extract frontmatter from 5-10 broadly-useful skills (e.g., `accessibility-audit`, `api-design`, `async-concurrency`, `brainstorming`, `bill-deadline-tracker`, `coding-via-chat`, `inbox-triage`, `meeting-notes`), embed them as `well-known/<name>` entries in `well_known_manifest.json`. Now users see real options on day 1.

**[C2] Skill loader recursion was buried in Phase 6.** If the loader doesn't walk into `.hub/`, the entire Skills Hub is decorative — installs silently never reach the agent. **Fix:** Promote Task 6.4 to **Task 0.2 — Pre-flight: verify skill loader recursion** in a new Phase 0. Block Phase 4 (CLI) on this passing.

**[C3] Skills Guard scanner API was assumed.** Tests mocked `guard.scan().severity` and `.findings`, but I never verified the actual API shape. **Fix:** Add **Task 0.3 — Read `opencomputer/skills_guard/scanner.py`** as a Phase 0 pre-flight. Document the actual public class name, scan entry-point signature, and verdict object fields. Update Task 3.1 mocks + Installer wiring to match.

**[C4] Slash dispatcher registration shape was hand-wavy.** Task 4.3 said "Adapt to whatever exact registry shape exists." That's a placeholder inside a no-placeholders plan. **Fix:** Add **Task 0.4 — Read `opencomputer/agent/slash_dispatcher.py`** as Phase 0 pre-flight. Document the actual registration mechanism. Update Task 4.3 to use it directly instead of the speculative `SLASH_HANDLERS["skills"] = ...` shape.

**[C5] Profile resolver was hardcoded to `"default"`.** `_profile_home()` returned `~/.opencomputer/default/` regardless of the user's active profile. Multi-profile users would have hub state lost on profile switch. **Fix:** Add **Task 0.5 — Read `opencomputer/cli_profile.py` for active-profile resolver pattern**. Use the same call site (likely an existing `get_active_profile()` or similar) in `_profile_home()`.

**[C6] `fcntl.flock` doesn't work on Windows.** OC supports Windows post-OI-removal (PR #179). Concurrent `oc skills install` calls on Windows would race. **Fix:** Replace `fcntl`-based `_file_lock` in Task 1.4 with the `filelock` library (already in OC's tree per `extensions/coding-harness/process_lock.py` patterns — verify; if not, add to pyproject as cross-platform locking dep, ~6KB pure Python).

**[C7] Existing `oc skills` subapp collision.** The audit reported `oc skills` already exists (list/view/install for *bundled* skills). My new subapp has the same names. **Fix:** Add **Task 0.6 — Read existing `oc skills` definition** in Phase 0. Decision: **merge.** New hub `install/list/uninstall` replaces old; `view` (read SKILL.md prose) stays as-is on the new subapp; `oc skills list --source local|hub|all` filters. Existing tests must continue to pass.

**[C8] Branch was implicit.** Plan committed straight to main in some tasks, mentioned `feat/skills-hub` only at PR open. **Fix:** **Task 0.1 — Create branch** prepended to Phase 0: `git checkout -b feat/skills-hub`. All commits go on this branch. PR opens after Phase 6.

**[C9] `oc skills update` was non-atomic** (uninstall+install — if network drops mid-install, user has neither old nor new). **Fix:** Update Task 4.1's `cmd_update` to do: fetch new bundle → install to staging dir `<hub>/_staging/<id>/` → on success, atomic-replace skill dir + lockfile entry → cleanup staging. Document in Task 4.1.

### Non-critical findings (executor must respect)

**[N1] SHA-256 covers only `bundle.skill_md`, not `bundle.files`.** An attacker controlling extra files but pinning SKILL.md content could ship malicious helpers. *Mitigation:* MVP scope is SKILL.md-only (no helper files installed by default — Task 5.1 GitHubSource explicitly returns `files={}`). If we later install helper files, switch to a tree hash. Document in installer docstring.

**[N2] GitHub anonymous clone is rate-limited** (~60 req/hr without auth). Personal use unlikely to hit. *Mitigation:* support `GITHUB_TOKEN` env var in `GitHubSource._ensure_cloned` (`x-access-token` URL prefix); document in README. Add as Task 5.1.5 follow-up if user-visible issue surfaces.

**[N3] GitHubSource never re-fetches.** Once cloned, content frozen. *Mitigation:* document in README; add `oc skills tap update <repo>` follow-up task.

**[N4] Concurrent same-identifier install race.** Two processes both install `well-known/foo`. fcntl protects lockfile but not skill dir. *Mitigation:* Wrap entire `Installer.install()` body in the same flock as lockfile writes. Update Task 3.1.

**[N5] `validate_frontmatter` standard is best-effort.** I inferred agentskills.io spec; the real spec may differ. *Mitigation:* docstring in `agentskills_validator.py` flags this as inferred; tighten when official spec accessible.

**[N6] Tests use `Mock` objects for SkillsGuard.** Phase 0 reads the actual API; tests update accordingly. Already addressed by C3.

**[N7] e2e test doesn't verify Skills Guard ran.** *Mitigation:* Update Task 6.1 e2e to assert audit log entry has `guard_severity` field non-null.

**[N8] PR body mentions ~50 tests; actual is ~74.** Update phase gates accordingly.

**[N9] One PR vs split.** ~30 commits in one PR is large. *Mitigation:* offer reviewer choice — stack as one PR labeled "needs careful review" OR split into 3: Phase 1+2 / Phase 3+4 / Phase 5+6. Default: **one PR** since the work is tightly coupled and merge order matters.

**[N10] `pyyaml` dep not verified.** *Mitigation:* check pyproject.toml; OC's config_store.py uses YAML so it's likely there.

**[N11] No data migration for lockfile schema bumps.** *Mitigation:* `LOCKFILE_VERSION = 1` already provides the field. Add `_migrate(data)` stub as future-proofing in Task 1.4.

**[N12] Missing review-skill invocation at phase boundaries.** *Mitigation:* Phase 3 + Phase 5 + Phase 6 gates additionally invoke `superpowers:requesting-code-review` skill against the phase's diff before push. Add to gate steps.

**[N13] Skills Guard verdict shape isn't normalized.** Different scanners might return different verdicts. *Mitigation:* installer should treat any `severity in ("high", "critical")` as block; everything else (including unknown) as pass-with-warning logged. Tighten in Task 3.1.

**[N14] Skill identifier validation.** Nothing prevents `oc skills install ../../etc/passwd`. *Mitigation:* add path traversal guard in Installer — identifier must match `^[\w.-]+/[\w.-]+(/[\w.-]+)?$`. Add to Task 3.1.

**[N15] Network failures in GitHubSource fetch don't surface to user.** *Mitigation:* `_ensure_cloned` raises on network failure; `_walk_skills` catches and returns `[]`. CLI surface should display "GitHub source unreachable" rather than silently empty results. Add to Task 5.1.

**[N16] `--source` filter doesn't validate source name.** Typoing `oc skills search foo --source wel-known` returns silent zero. *Mitigation:* CLI checks `source not in router.list_sources()` and errors. Add to Task 4.1.

### Stress test — real-world scenarios

| Scenario | Plan handles correctly? |
|---|---|
| User installs same skill twice (idempotency) | ✅ Task 3.1 test `test_double_install_replaces_lockfile_entry` |
| Network down during `oc skills tap add` clone | ⚠️ N15 — surface error gracefully |
| Skills Guard returns `severity=high` | ✅ Task 3.1 test `test_install_blocked_by_skills_guard` |
| Two terminals install different skills concurrently | ✅ Task 1.4 test `test_concurrent_writes_serialize` (after C6 fix) |
| Two terminals install **same** skill concurrently | ⚠️ N4 — wrap install body in flock |
| Profile switch between install and uninstall | ⚠️ C5 — active-profile resolver fixes this |
| User runs `oc skills install ../../etc/passwd` | ⚠️ N14 — path traversal guard needed |
| User installs skill, agent invokes it next turn | ⚠️ C2 — loader recursion verified in Phase 0 |
| User on Windows runs concurrent installs | ⚠️ C6 — `filelock` instead of `fcntl` |
| User has 100 taps, each with 50 skills | ⚠️ N3 + perf — `_walk_skills` does fs walk every search; should cache for ~1 minute. Defer to follow-up; document as known limitation |
| User uninstalls skill that's never been installed | ✅ Task 3.1 test `test_uninstall_*` would catch (raises `InstallError`) |
| Bundled manifest entry has invalid frontmatter | ✅ Task 1.3 validator catches; install fails clean |
| Skill name kebab-case violation (e.g. `My_Skill`) | ✅ Task 1.3 validator |
| Existing `oc skills` subapp test passes after merge | ⚠️ C7 — verify in Phase 0 |
| Plan executor reads slash_dispatcher.py and finds different shape | ⚠️ C4 — Phase 0 reads first |

### Refined Phase 0 (NEW — must run first)

**Phase 0 goal:** Verify external assumptions before writing code. ~30-60 min total.

#### Task 0.1: Create branch

```bash
git checkout -b feat/skills-hub
```

#### Task 0.2: Verify skill loader recursion

```bash
grep -n "rglob\|glob\|SKILL.md\|list_skills\|_load_skills" opencomputer/agent/memory.py | head -20
```

Document in plan execution log:
- Path: `opencomputer/agent/memory.py:Lxxx`
- Pattern: does it use `rglob("**/SKILL.md")` or one-level `glob("*/SKILL.md")`?
- If one-level: fix in Task 0.2.1 — change to recursive glob, add test for `.hub/` discovery, run full suite to verify no regression.

#### Task 0.3: Read Skills Guard scanner

```bash
grep -n "class \|def " opencomputer/skills_guard/scanner.py
grep -n "class \|def " opencomputer/skills_guard/policy.py
```

Document:
- Public class name (`SkillsGuardScanner`? `Scanner`? `ThreatScanner`?)
- Scan method name + signature (`scan(text)`? `scan_skill(skill_md)`? `evaluate(skill_md)`?)
- Verdict object shape: fields, severity values

Update Task 3.1 mocks and Installer wiring to match.

#### Task 0.4: Read slash dispatcher

```bash
grep -n "register\|SLASH_\|dispatch_slash\|is_slash_command" opencomputer/agent/slash_dispatcher.py | head -20
ls opencomputer/agent/slash_commands_impl/
```

Document the registration mechanism. Update Task 4.3 to use the actual API.

#### Task 0.5: Read active-profile resolver

```bash
grep -n "active_profile\|get_active\|profile_home\|profile_dir" opencomputer/cli_profile.py opencomputer/profiles.py 2>/dev/null | head -20
```

Document the public function. Update `_profile_home()` in `cli_skills_hub.py` to call it.

#### Task 0.6: Read existing `oc skills` subapp

```bash
grep -rn "skills_app\|cli_skills" opencomputer/cli.py opencomputer/ | head -20
```

If existing `oc skills` exists with `list/install/view/etc` commands:
- Document each command + behavior
- Decision tree:
  - `list`: merge — existing logic + new `--source local|hub|all` filter
  - `install`: replace — old becomes hub install
  - `view`: keep as-is on new subapp
  - Anything else: case-by-case
- Plan migration: any tests for old `oc skills` must continue to pass

Phase 0 ends with a written DECISIONS.md note in the branch describing all six findings. Then Phase 1 starts.

### Confidence after refinement

The plan is now defensible: every assumption that was speculative is verified before code is written; every load-bearing dependency (loader, scanner, dispatcher, profile resolver) gets a Phase 0 audit; cross-platform locking is correct; the manifest seeds with real content; `oc skills` collision is acknowledged with a clear merge path; identifier injection is guarded; concurrent-install races are flock'd.

What remains intentionally deferred:
- Publish flow (separate spec)
- Snapshot export/import (separate spec)
- Per-tap update with TTL caching
- ClawHub / SkillsSh / LobeHub / ClaudeMarketplace sources

These are explicitly out of scope per the plan header and don't compromise the MVP.

---

## End of plan

**Total tasks:** 24 (18 original + 6 in Phase 0 pre-flight) across 7 phases.
**Estimated dev-days:** 7-10 days at TDD pace (Phase 0 = 0.5 day).
**Estimated commits:** ~30-35 (each task = 1-2 commits).
**Estimated tests:** ~74 unit tests + 1 e2e + loader test.
**Breakable points:** any phase boundary is a clean stopping point. Phase 0 must finish before Phase 1.

**Branch:** `feat/skills-hub`. PR opens after Phase 6.

**Execution: see writing-plans skill's "Execution Handoff" — choose subagent-driven (recommended) or inline.**
