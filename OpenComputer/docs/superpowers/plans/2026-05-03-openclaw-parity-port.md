# OpenClaw-Parity Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port nine load-bearing pieces of openclaw's plugin/wire contract that OpenComputer's reference-import phase missed — `min_host_version` pinning, extension boundary test (frozen-inventory), `activation` block + planner, `SecretRef` wire primitive, `plugins inspect` shape classifier, typed `ErrorCode` enum, JSON5 manifest tolerance, 256KB manifest size cap, and `providerAuthChoices` rich auth UI metadata.

**Architecture:** Single PR, 9 ordered commits, schema bump `plugin.json` v3 → v4 (every new field optional, so v3 manifests still parse). Pure additive on `plugin_sdk` (3 dataclasses + new `wire_primitives` module), surgical additions to `opencomputer/plugins/` (3 new modules) and `opencomputer/gateway/` (1 new module + extend existing schemas). Boundary test ships in advisory mode with frozen inventory of the 27 existing violators.

**Tech Stack:** Python 3.12+, pydantic v2, typer, pytest, ruff, json5 (new dep), packaging.

---

## Files: created + modified

### Created
- `plugin_sdk/wire_primitives.py` — `SecretRef`, `SecretResolver`
- `opencomputer/plugins/activation_planner.py` — manifest-driven activation
- `opencomputer/plugins/inspect_shape.py` — shape classifier
- `opencomputer/gateway/error_codes.py` — typed `ErrorCode` enum
- `tests/test_min_host_version.py`
- `tests/test_secret_ref.py`
- `tests/test_activation_planner.py`
- `tests/test_inspect_shape.py`
- `tests/test_error_codes.py`
- `tests/test_json5_tolerance.py`
- `tests/test_manifest_size_cap.py`
- `tests/test_auth_choices.py`
- `tests/test_plugin_extension_boundary.py`
- `tests/fixtures/plugin_extension_import_boundary_inventory.json`
- `scripts/refresh_extension_boundary_inventory.py`

### Modified
- `plugin_sdk/core.py` — add `min_host_version`, `PluginActivation`, `AuthChoice` dataclass + fields
- `plugin_sdk/__init__.py` — export new types
- `opencomputer/plugins/manifest_validator.py` — schema mirrors of new fields
- `opencomputer/plugins/discovery.py` — JSON5 parse + size cap + parse new fields
- `opencomputer/plugins/loader.py` — `min_host_version` enforcement at load
- `opencomputer/plugins/__init__.py` — export `inspect_shape`, `plan_activations`
- `opencomputer/gateway/protocol.py` — extend `WireResponse` with `code` field
- `opencomputer/gateway/protocol_v2.py` — re-export `ErrorCode`
- `opencomputer/cli_plugin.py` — `plugin_inspect` Typer subcommand
- `opencomputer/__init__.py` — only if exposing `__version__` for compat checks
- `pyproject.toml` — add `json5>=0.9` dep
- `extensions/anthropic-provider/plugin.json` — set `min_host_version: "1.0.0"` as a smoke-test plugin demonstrating the new field
- `OpenComputer/CLAUDE.md` — manifest field changelog
- `OpenComputer/CHANGELOG.md` — v1.1 unreleased section

---

## Task 1: Add `min_host_version` field to `PluginManifest`

**Files:**
- Modify: `plugin_sdk/core.py`
- Modify: `opencomputer/plugins/manifest_validator.py`
- Modify: `opencomputer/plugins/discovery.py`
- Test: `tests/test_min_host_version.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_min_host_version.py
"""Manifest min_host_version field validation + parse."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.plugins.discovery import discover
from opencomputer.plugins.manifest_validator import validate_manifest


def _base_manifest(**overrides: object) -> dict[str, object]:
    base = {
        "id": "test-plug",
        "name": "Test",
        "version": "0.1.0",
        "entry": "plugin",
        "kind": "tool",
    }
    base.update(overrides)
    return base


class TestMinHostVersionValidation:
    def test_field_optional_default_empty(self) -> None:
        schema, err = validate_manifest(_base_manifest())
        assert err == ""
        assert schema is not None
        assert schema.min_host_version == ""

    def test_explicit_semver_value_accepted(self) -> None:
        schema, err = validate_manifest(
            _base_manifest(min_host_version="1.2.3")
        )
        assert err == ""
        assert schema is not None
        assert schema.min_host_version == "1.2.3"

    def test_pre_release_accepted(self) -> None:
        schema, err = validate_manifest(
            _base_manifest(min_host_version="1.2.3-beta")
        )
        assert err == ""
        assert schema is not None

    def test_malformed_version_rejected(self) -> None:
        schema, err = validate_manifest(
            _base_manifest(min_host_version="not-a-version")
        )
        assert schema is None
        assert "min_host_version" in err
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_min_host_version.py::TestMinHostVersionValidation -v
```

Expected: FAIL — `AttributeError: 'PluginManifestSchema' object has no attribute 'min_host_version'`

- [ ] **Step 3: Add field to `PluginManifest` dataclass**

In `plugin_sdk/core.py`, find the `PluginManifest` dataclass and add:

```python
@dataclass(frozen=True, slots=True)
class PluginManifest:
    # ... existing fields ...
    schema_version: int | None = None
    # NEW (Task 1) — minimum opencomputer version this plugin requires.
    # Empty string = no check. Compared via packaging.version.Version
    # at load time (loader.py).
    min_host_version: str = ""
```

Update `__all__` if it lists fields explicitly (it does not — leave alone).

- [ ] **Step 4: Add field to `PluginManifestSchema`**

In `opencomputer/plugins/manifest_validator.py`:

```python
# Add after the existing schema_version field, before any @field_validator decorators.

    # Task 1 — pinned minimum opencomputer.__version__ that this plugin
    # was built against. Empty string = no check. Validated via
    # packaging.version.parse() — malformed strings rejected at scan.
    min_host_version: str = Field(default="", max_length=32)
```

Then add a validator:

```python
    @field_validator("min_host_version")
    @classmethod
    def _min_host_version_format(cls, v: str) -> str:
        if v == "":
            return v
        from packaging.version import InvalidVersion, Version
        try:
            Version(v)
        except InvalidVersion as e:
            raise ValueError(
                f"min_host_version {v!r} is not a valid PEP 440 / semver string ({e})"
            ) from e
        return v
```

- [ ] **Step 5: Wire it through `discovery._parse_manifest`**

In `opencomputer/plugins/discovery.py`, inside the `PluginManifest(...)` construction at the end of `_parse_manifest`, add:

```python
        # Task 1 — minimum host version pin (default empty = no check).
        min_host_version=schema.min_host_version,
```

- [ ] **Step 6: Run tests to verify they pass**

```
pytest tests/test_min_host_version.py::TestMinHostVersionValidation -v
```

Expected: PASS — all 4 tests green.

- [ ] **Step 7: Run the full suite to verify no regressions**

```
pytest tests/ -x -q 2>&1 | tail -20
```

Expected: existing 885 tests + 4 new = 889 PASS, no failures.

- [ ] **Step 8: Commit**

```bash
git add plugin_sdk/core.py opencomputer/plugins/manifest_validator.py opencomputer/plugins/discovery.py tests/test_min_host_version.py
git commit -m "feat(plugin-sdk): add min_host_version field to PluginManifest"
```

---

## Task 2: Add `PluginActivation` dataclass + manifest field

**Files:**
- Modify: `plugin_sdk/core.py`
- Modify: `plugin_sdk/__init__.py`
- Modify: `opencomputer/plugins/manifest_validator.py`
- Modify: `opencomputer/plugins/discovery.py`
- Test: extend `tests/test_min_host_version.py` with a new `TestActivationField` class (cohesive — both are PluginManifest schema additions)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_min_host_version.py`:

```python
class TestActivationField:
    def test_default_is_none(self) -> None:
        schema, err = validate_manifest(_base_manifest())
        assert err == ""
        assert schema is not None
        assert schema.activation is None

    def test_explicit_activation_block_parses(self) -> None:
        schema, err = validate_manifest(
            _base_manifest(
                activation={
                    "on_providers": ["anthropic"],
                    "on_channels": ["telegram"],
                    "on_commands": ["/foo"],
                    "on_tools": ["X"],
                    "on_models": ["claude-"],
                }
            )
        )
        assert err == ""
        assert schema is not None
        assert schema.activation is not None
        assert schema.activation.on_providers == ["anthropic"]
        assert schema.activation.on_channels == ["telegram"]
        assert schema.activation.on_commands == ["/foo"]
        assert schema.activation.on_tools == ["X"]
        assert schema.activation.on_models == ["claude-"]

    def test_partial_activation_other_fields_default_empty(self) -> None:
        schema, err = validate_manifest(
            _base_manifest(activation={"on_providers": ["openai"]})
        )
        assert err == ""
        assert schema is not None
        assert schema.activation is not None
        assert schema.activation.on_providers == ["openai"]
        assert schema.activation.on_channels == []

    def test_unknown_activation_key_rejected(self) -> None:
        schema, err = validate_manifest(
            _base_manifest(activation={"on_unknown": ["x"]})
        )
        assert schema is None
        assert "activation" in err
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_min_host_version.py::TestActivationField -v
```

Expected: FAIL — `activation` field unknown.

- [ ] **Step 3: Add `PluginActivation` dataclass to `plugin_sdk/core.py`**

In `plugin_sdk/core.py`, just above `PluginManifest`:

```python
@dataclass(frozen=True, slots=True)
class PluginActivation:
    """Manifest-declared triggers for demand-driven plugin activation.

    Sub-project G (openclaw-parity) Task 2. Lets the activation planner
    decide when a plugin loads from manifest alone, without inferring
    from tool_names. Empty tuples = no trigger of that kind. When the
    whole block is None on PluginManifest, planner falls back to legacy
    tool_names inference (Sub-project E behavior).

    Mirrors openclaw ``activation`` shape from
    ``sources/openclaw-2026.4.23/src/plugins/manifest.ts``.
    """

    on_providers: tuple[str, ...] = ()
    on_channels: tuple[str, ...] = ()
    on_commands: tuple[str, ...] = ()
    on_tools: tuple[str, ...] = ()
    on_models: tuple[str, ...] = ()
```

Then add the field to `PluginManifest`:

```python
    # NEW (Task 2) — manifest-declared activation triggers. None = use
    # legacy tool_names inference. See activation_planner.py.
    activation: PluginActivation | None = None
```

- [ ] **Step 4: Export `PluginActivation` from the SDK**

In `plugin_sdk/__init__.py`, add it to the existing imports + `__all__`:

```python
from plugin_sdk.core import (
    # ... existing ...
    PluginActivation,
)
```

And in `__all__`:

```python
    "PluginActivation",
```

- [ ] **Step 5: Add validator schema**

In `opencomputer/plugins/manifest_validator.py`, before `PluginManifestSchema`:

```python
class PluginActivationSchema(BaseModel):
    """Mirror of ``plugin_sdk.core.PluginActivation`` for validation only."""

    model_config = ConfigDict(extra="forbid")

    on_providers: list[str] = Field(default_factory=list)
    on_channels: list[str] = Field(default_factory=list)
    on_commands: list[str] = Field(default_factory=list)
    on_tools: list[str] = Field(default_factory=list)
    on_models: list[str] = Field(default_factory=list)

    @field_validator(
        "on_providers", "on_channels", "on_commands", "on_tools", "on_models",
        mode="before",
    )
    @classmethod
    def _drop_empty_strings(cls, v: object) -> object:
        if isinstance(v, list):
            return [s for s in v if isinstance(s, str) and s.strip()]
        return v
```

Then add the field to `PluginManifestSchema`:

```python
    # Task 2 — manifest-declared activation triggers; None = legacy
    # tool_names inference path.
    activation: PluginActivationSchema | None = Field(default=None)
```

Add `PluginActivationSchema` to `__all__`.

- [ ] **Step 6: Wire it through `discovery._parse_manifest`**

In `opencomputer/plugins/discovery.py`, after the existing `setup = ...` block and before `return PluginManifest(...)`:

```python
    # Task 2 (openclaw-parity) — flatten activation block.
    from plugin_sdk.core import PluginActivation

    activation = (
        PluginActivation(
            on_providers=tuple(schema.activation.on_providers),
            on_channels=tuple(schema.activation.on_channels),
            on_commands=tuple(schema.activation.on_commands),
            on_tools=tuple(schema.activation.on_tools),
            on_models=tuple(schema.activation.on_models),
        )
        if schema.activation is not None
        else None
    )
```

Then in the `PluginManifest(...)` constructor, add:

```python
        # Task 2 — activation triggers from manifest.
        activation=activation,
```

- [ ] **Step 7: Run tests**

```
pytest tests/test_min_host_version.py::TestActivationField -v
```

Expected: PASS — all 4 new tests green.

- [ ] **Step 8: Run full suite**

```
pytest tests/ -x -q 2>&1 | tail -10
```

Expected: 893 PASS.

- [ ] **Step 9: Commit**

```bash
git add plugin_sdk/core.py plugin_sdk/__init__.py opencomputer/plugins/manifest_validator.py opencomputer/plugins/discovery.py tests/test_min_host_version.py
git commit -m "feat(plugin-sdk): add PluginActivation block to manifest"
```

---

## Task 3: Add `AuthChoice` dataclass + `SetupProvider.auth_choices` field

**Files:**
- Modify: `plugin_sdk/core.py`
- Modify: `plugin_sdk/__init__.py`
- Modify: `opencomputer/plugins/manifest_validator.py`
- Modify: `opencomputer/plugins/discovery.py`
- Test: `tests/test_auth_choices.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_choices.py
"""SetupProvider.auth_choices — manifest-declared rich auth UI metadata."""

from __future__ import annotations

from opencomputer.plugins.manifest_validator import validate_manifest


def _manifest_with_provider(**provider_overrides: object) -> dict[str, object]:
    provider = {
        "id": "anthropic",
        "auth_methods": ["api_key"],
        "env_vars": ["ANTHROPIC_API_KEY"],
    }
    provider.update(provider_overrides)
    return {
        "id": "test",
        "name": "Test",
        "version": "0.1.0",
        "entry": "plugin",
        "kind": "provider",
        "setup": {"providers": [provider]},
    }


class TestAuthChoices:
    def test_default_empty_tuple(self) -> None:
        schema, err = validate_manifest(_manifest_with_provider())
        assert err == ""
        assert schema is not None
        assert schema.setup is not None
        assert schema.setup.providers[0].auth_choices == []

    def test_full_auth_choice_parses(self) -> None:
        schema, err = validate_manifest(
            _manifest_with_provider(
                auth_choices=[
                    {
                        "method": "api_key",
                        "label": "Anthropic API key",
                        "cli_flag": "--anthropic-key",
                        "option_key": "anthropic.api_key",
                        "group": "anthropic-auth",
                        "onboarding_priority": 100,
                    }
                ]
            )
        )
        assert err == ""
        assert schema is not None
        assert len(schema.setup.providers[0].auth_choices) == 1
        ac = schema.setup.providers[0].auth_choices[0]
        assert ac.method == "api_key"
        assert ac.label == "Anthropic API key"
        assert ac.cli_flag == "--anthropic-key"
        assert ac.option_key == "anthropic.api_key"
        assert ac.group == "anthropic-auth"
        assert ac.onboarding_priority == 100

    def test_method_required(self) -> None:
        schema, err = validate_manifest(
            _manifest_with_provider(
                auth_choices=[{"label": "X"}]
            )
        )
        assert schema is None
        assert "method" in err

    def test_unknown_field_rejected(self) -> None:
        schema, err = validate_manifest(
            _manifest_with_provider(
                auth_choices=[
                    {"method": "api_key", "label": "X", "garbage": "yes"}
                ]
            )
        )
        assert schema is None
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_auth_choices.py -v
```

Expected: FAIL — `auth_choices` field unknown.

- [ ] **Step 3: Add `AuthChoice` dataclass**

In `plugin_sdk/core.py`, just above `SetupProvider`:

```python
@dataclass(frozen=True, slots=True)
class AuthChoice:
    """Rich UI / CLI metadata for one auth method.

    Task 3 (openclaw-parity). Mirrors openclaw ``providerAuthChoices``
    shape so the setup wizard + CLI flags can be derived from manifest
    rather than hand-wired per provider.

    Empty strings on optional fields = "absent" (not nil) to keep the
    wire shape uniform.
    """

    method: str                  # required: matches one of setup.providers.auth_methods
    label: str = ""              # human-readable choice label in wizard
    cli_flag: str = ""           # e.g. "--anthropic-key"
    option_key: str = ""         # internal config key
    group: str = ""              # group hint for clustering related auth in wizard
    onboarding_priority: int = 0 # higher = shown first in wizard
```

Add the field to `SetupProvider`:

```python
@dataclass(frozen=True, slots=True)
class SetupProvider:
    # ... existing fields ...
    signup_url: str = ""
    # NEW (Task 3) — rich auth UI metadata; empty tuple = wizard falls
    # back to auth_methods: list[str] interpretation.
    auth_choices: tuple[AuthChoice, ...] = ()
```

- [ ] **Step 4: Export `AuthChoice` from the SDK**

In `plugin_sdk/__init__.py`:

```python
from plugin_sdk.core import (
    # ... existing ...
    AuthChoice,
)
```

And `__all__`:

```python
    "AuthChoice",
```

- [ ] **Step 5: Add validator schema**

In `opencomputer/plugins/manifest_validator.py`, before `SetupProviderSchema`:

```python
class AuthChoiceSchema(BaseModel):
    """Validator mirror of ``plugin_sdk.core.AuthChoice``."""

    model_config = ConfigDict(extra="forbid")

    method: str = Field(min_length=1, max_length=32)
    label: str = Field(default="", max_length=128)
    cli_flag: str = Field(default="", max_length=64)
    option_key: str = Field(default="", max_length=128)
    group: str = Field(default="", max_length=64)
    onboarding_priority: int = Field(default=0)
```

Then add the field to `SetupProviderSchema`:

```python
    # Task 3 — rich auth UI metadata; default empty list = legacy
    # auth_methods-only behavior.
    auth_choices: list[AuthChoiceSchema] = Field(default_factory=list)
```

Add `AuthChoiceSchema` to `__all__`.

- [ ] **Step 6: Wire through `discovery._parse_manifest`**

In `opencomputer/plugins/discovery.py`, find the `SetupProvider(...)` construction inside the `setup = ...` block. Update it to include `auth_choices`:

```python
                SetupProvider(
                    id=p.id,
                    auth_methods=tuple(p.auth_methods),
                    env_vars=tuple(p.env_vars),
                    label=p.label,
                    default_model=p.default_model,
                    signup_url=p.signup_url,
                    # Task 3 — rich auth UI metadata, parallel to auth_methods.
                    auth_choices=tuple(
                        AuthChoice(
                            method=a.method,
                            label=a.label,
                            cli_flag=a.cli_flag,
                            option_key=a.option_key,
                            group=a.group,
                            onboarding_priority=a.onboarding_priority,
                        )
                        for a in p.auth_choices
                    ),
                )
```

Add `AuthChoice` to the existing `from plugin_sdk.core import (...)` block at top of file.

- [ ] **Step 7: Run tests**

```
pytest tests/test_auth_choices.py -v
```

Expected: PASS — all 4 tests green.

- [ ] **Step 8: Run full suite**

```
pytest tests/ -x -q 2>&1 | tail -10
```

Expected: 897 PASS.

- [ ] **Step 9: Commit**

```bash
git add plugin_sdk/core.py plugin_sdk/__init__.py opencomputer/plugins/manifest_validator.py opencomputer/plugins/discovery.py tests/test_auth_choices.py
git commit -m "feat(plugin-sdk): add AuthChoice rich auth UI metadata to SetupProvider"
```

---

## Task 4: JSON5 manifest tolerance

**Files:**
- Modify: `pyproject.toml`
- Modify: `opencomputer/plugins/discovery.py`
- Test: `tests/test_json5_tolerance.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_json5_tolerance.py
"""plugin.json may be JSON5 — comments + trailing commas tolerated."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.plugins.discovery import _parse_manifest


def _write(tmp: Path, content: str) -> Path:
    p = tmp / "plugin.json"
    p.write_text(content, encoding="utf-8")
    return p


class TestJSON5Tolerance:
    def test_plain_json_still_parses(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            '{"id":"x","name":"X","version":"0.1.0","entry":"plugin","kind":"tool"}',
        )
        m = _parse_manifest(path)
        assert m is not None
        assert m.id == "x"

    def test_line_comment_tolerated(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
            {
                // line comment
                "id": "x",
                "name": "X",
                "version": "0.1.0",
                "entry": "plugin",
                "kind": "tool"
            }
            """,
        )
        m = _parse_manifest(path)
        assert m is not None
        assert m.id == "x"

    def test_trailing_comma_tolerated(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
            {
                "id": "x",
                "name": "X",
                "version": "0.1.0",
                "entry": "plugin",
                "kind": "tool",
            }
            """,
        )
        m = _parse_manifest(path)
        assert m is not None
        assert m.id == "x"

    def test_block_comment_tolerated(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
            {
                /* block
                   comment */
                "id": "x",
                "name": "X",
                "version": "0.1.0",
                "entry": "plugin",
                "kind": "tool"
            }
            """,
        )
        m = _parse_manifest(path)
        assert m is not None

    def test_garbage_neither_json_nor_json5_returns_none(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "this is not json")
        m = _parse_manifest(path)
        assert m is None
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_json5_tolerance.py -v
```

Expected: FAIL — `test_line_comment_tolerated` etc. fail because `json.loads` rejects comments.

- [ ] **Step 3: Add `json5` dep**

In `pyproject.toml`, find `dependencies = [` block and add (alphabetical insertion):

```toml
    "json5>=0.9",
```

Run:

```
uv sync
```

- [ ] **Step 4: Two-tier parse in discovery**

In `opencomputer/plugins/discovery.py`, replace the `_parse_manifest` opening:

```python
def _parse_manifest(manifest_path: Path) -> PluginManifest | None:
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to read manifest %s: %s", manifest_path, e)
        return None

    # Task 4 (openclaw-parity): two-tier parse. Try strict json first
    # (zero overhead for compliant manifests), fall back to json5 only
    # on JSONDecodeError so authors can use comments + trailing commas
    # in their plugin.json. See openclaw manifest.json5-tolerance.test.ts.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import json5 as _json5
            data = _json5.loads(text)
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to parse manifest %s: %s", manifest_path, e)
            return None
```

Remove the original `try: data = json.loads(...)` lines.

- [ ] **Step 5: Run tests**

```
pytest tests/test_json5_tolerance.py -v
```

Expected: PASS — all 5 tests green.

- [ ] **Step 6: Run full suite**

```
pytest tests/ -x -q 2>&1 | tail -10
```

Expected: 902 PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml opencomputer/plugins/discovery.py tests/test_json5_tolerance.py uv.lock
git commit -m "feat(manifest): JSON5 tolerance for plugin.json (comments, trailing commas)"
```

---

## Task 5: 256KB manifest size cap

**Files:**
- Modify: `opencomputer/plugins/discovery.py`
- Test: `tests/test_manifest_size_cap.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_manifest_size_cap.py
"""plugin.json size is capped at 256KB to defend discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.plugins.discovery import MAX_MANIFEST_BYTES, _parse_manifest


class TestManifestSizeCap:
    def test_normal_size_parses(self, tmp_path: Path) -> None:
        path = tmp_path / "plugin.json"
        path.write_text(
            '{"id":"x","name":"X","version":"0.1.0","entry":"plugin","kind":"tool"}',
            encoding="utf-8",
        )
        assert _parse_manifest(path) is not None

    def test_oversized_skipped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        path = tmp_path / "plugin.json"
        # Pad description to exceed the cap.
        padding = "x" * (MAX_MANIFEST_BYTES + 1)
        path.write_text(
            '{"id":"x","name":"X","version":"0.1.0","entry":"plugin",'
            f'"kind":"tool","description":"{padding}"}}',
            encoding="utf-8",
        )
        with caplog.at_level("WARNING", logger="opencomputer.plugins.discovery"):
            result = _parse_manifest(path)
        assert result is None
        assert any("exceeds" in rec.message for rec in caplog.records)

    def test_exact_boundary_parses(self, tmp_path: Path) -> None:
        path = tmp_path / "plugin.json"
        # Build a manifest exactly at MAX_MANIFEST_BYTES.
        prefix = '{"id":"x","name":"X","version":"0.1.0","entry":"plugin","kind":"tool","description":"'
        suffix = '"}'
        pad_len = MAX_MANIFEST_BYTES - len(prefix) - len(suffix)
        path.write_text(prefix + ("x" * pad_len) + suffix, encoding="utf-8")
        assert path.stat().st_size == MAX_MANIFEST_BYTES
        assert _parse_manifest(path) is not None
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_manifest_size_cap.py -v
```

Expected: FAIL — `MAX_MANIFEST_BYTES` not defined.

- [ ] **Step 3: Add size cap to `_parse_manifest`**

In `opencomputer/plugins/discovery.py`, near top of file with other constants:

```python
# Task 5 (openclaw-parity) — defence against pathological manifests.
# 256KB is plenty for any reasonable plugin description; rejects 100MB
# DOS attempts before we read the file.
MAX_MANIFEST_BYTES = 256 * 1024
```

Update `_parse_manifest` to check size before reading:

```python
def _parse_manifest(manifest_path: Path) -> PluginManifest | None:
    try:
        size = manifest_path.stat().st_size
    except OSError as e:
        logger.warning("failed to stat manifest %s: %s", manifest_path, e)
        return None
    if size > MAX_MANIFEST_BYTES:
        logger.warning(
            "manifest %s exceeds %d bytes (size=%d) — skipping",
            manifest_path,
            MAX_MANIFEST_BYTES,
            size,
        )
        return None
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to read manifest %s: %s", manifest_path, e)
        return None
    # ... existing JSON5 two-tier parse ...
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_manifest_size_cap.py -v
```

Expected: PASS — all 3 tests green.

- [ ] **Step 5: Run full suite**

```
pytest tests/ -x -q 2>&1 | tail -10
```

Expected: 905 PASS.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/plugins/discovery.py tests/test_manifest_size_cap.py
git commit -m "feat(manifest): 256KB size cap on plugin.json discovery"
```

---

## Task 6: `activation_planner` module

**Files:**
- Create: `opencomputer/plugins/activation_planner.py`
- Modify: `opencomputer/plugins/__init__.py`
- Test: `tests/test_activation_planner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_activation_planner.py
"""Activation planner — derive activation list from manifest triggers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from opencomputer.plugins.activation_planner import (
    ActivationTriggers,
    plan_activations,
)
from opencomputer.plugins.discovery import PluginCandidate
from plugin_sdk.core import PluginActivation, PluginManifest


def _make_candidate(
    plugin_id: str,
    *,
    activation: PluginActivation | None = None,
    tool_names: tuple[str, ...] = (),
) -> PluginCandidate:
    manifest = PluginManifest(
        id=plugin_id,
        name=plugin_id,
        version="0.1.0",
        description="",
        author="",
        homepage="",
        license="MIT",
        kind="tool",
        entry="plugin",
        profiles=None,
        single_instance=False,
        enabled_by_default=False,
        tool_names=tool_names,
        optional_tool_names=(),
        mcp_servers=(),
        model_support=None,
        legacy_plugin_ids=(),
        setup=None,
        activation=activation,
        min_host_version="",
    )
    return PluginCandidate(
        manifest=manifest,
        root_dir=Path("/tmp/fake"),
        manifest_path=Path("/tmp/fake/plugin.json"),
    )


class TestActivationPlanner:
    def test_no_triggers_no_activations(self) -> None:
        cands = [_make_candidate("a", activation=PluginActivation(on_providers=("openai",)))]
        result = plan_activations(cands, ActivationTriggers())
        assert result == []

    def test_provider_trigger_activates_match(self) -> None:
        cands = [
            _make_candidate("a", activation=PluginActivation(on_providers=("anthropic",))),
            _make_candidate("b", activation=PluginActivation(on_providers=("openai",))),
        ]
        result = plan_activations(
            cands,
            ActivationTriggers(active_providers=frozenset({"anthropic"})),
        )
        assert result == ["a"]

    def test_multiple_triggers_dedup(self) -> None:
        cands = [
            _make_candidate(
                "a",
                activation=PluginActivation(
                    on_providers=("anthropic",), on_tools=("X",)
                ),
            ),
        ]
        result = plan_activations(
            cands,
            ActivationTriggers(
                active_providers=frozenset({"anthropic"}),
                requested_tools=frozenset({"X"}),
            ),
        )
        assert result == ["a"]  # not ["a", "a"]

    def test_legacy_tool_names_path_when_activation_absent(self) -> None:
        cands = [_make_candidate("legacy", tool_names=("LegacyTool",))]
        result = plan_activations(
            cands,
            ActivationTriggers(requested_tools=frozenset({"LegacyTool"})),
        )
        assert result == ["legacy"]

    def test_activation_takes_precedence_over_tool_names(self) -> None:
        # When activation present, tool_names is folded in via on_tools.
        cands = [
            _make_candidate(
                "modern",
                activation=PluginActivation(on_tools=("ModernTool",)),
                tool_names=("AlsoLegacy",),
            )
        ]
        # Modern path: ModernTool triggers, AlsoLegacy does NOT (not declared).
        r1 = plan_activations(
            cands,
            ActivationTriggers(requested_tools=frozenset({"ModernTool"})),
        )
        assert r1 == ["modern"]
        r2 = plan_activations(
            cands,
            ActivationTriggers(requested_tools=frozenset({"AlsoLegacy"})),
        )
        assert r2 == ["modern"]  # tool_names is unioned with on_tools

    def test_command_trigger(self) -> None:
        cands = [_make_candidate("a", activation=PluginActivation(on_commands=("/foo",)))]
        r = plan_activations(cands, ActivationTriggers(invoked_commands=frozenset({"/foo"})))
        assert r == ["a"]

    def test_channel_trigger(self) -> None:
        cands = [
            _make_candidate("a", activation=PluginActivation(on_channels=("telegram",)))
        ]
        r = plan_activations(cands, ActivationTriggers(active_channels=frozenset({"telegram"})))
        assert r == ["a"]

    def test_model_prefix_trigger(self) -> None:
        cands = [
            _make_candidate("a", activation=PluginActivation(on_models=("claude-",)))
        ]
        r = plan_activations(cands, ActivationTriggers(active_model="claude-opus-4-7"))
        assert r == ["a"]

    def test_result_sorted_deterministic(self) -> None:
        cands = [
            _make_candidate("zebra", activation=PluginActivation(on_providers=("x",))),
            _make_candidate("apple", activation=PluginActivation(on_providers=("x",))),
        ]
        r = plan_activations(cands, ActivationTriggers(active_providers=frozenset({"x"})))
        assert r == ["apple", "zebra"]
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_activation_planner.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Create the planner module**

```python
# opencomputer/plugins/activation_planner.py
"""Manifest-driven activation planner.

Sub-project G (openclaw-parity) Task 6. Reads ``PluginManifest.activation``
declarations and a snapshot of current triggers (active providers,
channels, requested tools, invoked commands, active model id) and returns
the deterministic list of plugin ids that should be activated.

Falls back to ``tool_names`` when ``activation`` is None — that's the
legacy Sub-project E (PR #26) inference path. When ``activation`` is
present, ``activation.on_tools`` ∪ ``tool_names`` is the effective tool
trigger set so older plugins that declare only ``tool_names`` still work
even after the manifest schema gains the new block.

Mirrors openclaw ``activation-planner.ts`` shape from
``sources/openclaw-2026.4.23/src/plugins/activation-planner.ts``. Pure
function — no filesystem I/O, no plugin loading.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opencomputer.plugins.discovery import PluginCandidate

__all__ = [
    "ActivationTriggers",
    "plan_activations",
]


@dataclass(frozen=True, slots=True)
class ActivationTriggers:
    """Snapshot of current state that drives activation decisions.

    All fields default to empty so callers can supply only the triggers
    they care about. Frozen + slots so two snapshots can be compared
    deterministically (e.g. caching the planner result by trigger key).
    """

    active_providers: frozenset[str] = field(default_factory=frozenset)
    active_channels: frozenset[str] = field(default_factory=frozenset)
    invoked_commands: frozenset[str] = field(default_factory=frozenset)
    requested_tools: frozenset[str] = field(default_factory=frozenset)
    active_model: str = ""


def plan_activations(
    candidates: list[PluginCandidate],
    triggers: ActivationTriggers,
) -> list[str]:
    """Return ids of plugins whose activation triggers match the snapshot.

    Result is alphabetically sorted for determinism. Plugins with no
    activation declarations AND no tool_names produce no triggers (they
    must be enabled explicitly via config or ``enabled_by_default``).
    """
    activated: set[str] = set()
    for cand in candidates:
        manifest = cand.manifest
        # Compute effective trigger sets — modern (activation block) ∪
        # legacy (tool_names). When activation is None, only tool_names
        # contributes, preserving Sub-project E behavior.
        if manifest.activation is not None:
            on_providers = set(manifest.activation.on_providers)
            on_channels = set(manifest.activation.on_channels)
            on_commands = set(manifest.activation.on_commands)
            on_tools = set(manifest.activation.on_tools) | set(manifest.tool_names)
            on_models = list(manifest.activation.on_models)
        else:
            on_providers = set()
            on_channels = set()
            on_commands = set()
            on_tools = set(manifest.tool_names)
            on_models = []

        if on_providers & triggers.active_providers:
            activated.add(manifest.id)
            continue
        if on_channels & triggers.active_channels:
            activated.add(manifest.id)
            continue
        if on_commands & triggers.invoked_commands:
            activated.add(manifest.id)
            continue
        if on_tools & triggers.requested_tools:
            activated.add(manifest.id)
            continue
        if triggers.active_model:
            for prefix in on_models:
                if triggers.active_model.startswith(prefix):
                    activated.add(manifest.id)
                    break
    return sorted(activated)
```

- [ ] **Step 4: Re-export from package init**

In `opencomputer/plugins/__init__.py`, add:

```python
from opencomputer.plugins.activation_planner import (
    ActivationTriggers,
    plan_activations,
)
```

And update `__all__` if it exists (check the file first; if it doesn't exist, skip — Python will use module-level visibility).

- [ ] **Step 5: Run tests**

```
pytest tests/test_activation_planner.py -v
```

Expected: PASS — all 9 tests green.

- [ ] **Step 6: Run full suite**

```
pytest tests/ -x -q 2>&1 | tail -10
```

Expected: 914 PASS.

- [ ] **Step 7: Commit**

```bash
git add opencomputer/plugins/activation_planner.py opencomputer/plugins/__init__.py tests/test_activation_planner.py
git commit -m "feat(plugins): activation_planner — manifest-driven trigger evaluation"
```

---

## Task 7: `inspect_shape` module + CLI subcommand

**Files:**
- Create: `opencomputer/plugins/inspect_shape.py`
- Modify: `opencomputer/cli_plugin.py`
- Test: `tests/test_inspect_shape.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_inspect_shape.py
"""inspect_shape — compare manifest claims vs actual plugin registrations."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.plugins.discovery import PluginCandidate
from opencomputer.plugins.inspect_shape import (
    PluginShape,
    inspect_shape,
    inspect_shape_from_candidate,
)
from plugin_sdk.core import PluginManifest


def _make_manifest(
    plugin_id: str,
    *,
    tool_names: tuple[str, ...] = (),
    kind: str = "tool",
) -> PluginManifest:
    return PluginManifest(
        id=plugin_id,
        name=plugin_id,
        version="0.1.0",
        description="",
        author="",
        homepage="",
        license="MIT",
        kind=kind,
        entry="plugin",
        profiles=None,
        single_instance=False,
        enabled_by_default=False,
        tool_names=tool_names,
        optional_tool_names=(),
        mcp_servers=(),
        model_support=None,
        legacy_plugin_ids=(),
        setup=None,
        activation=None,
        min_host_version="",
    )


class TestInspectShape:
    def test_unknown_plugin_returns_drift_shape(self) -> None:
        shape = inspect_shape("does-not-exist-xyz")
        assert shape.classification == "drift"
        assert "not loaded" in shape.drift[0].lower() or "unknown" in shape.drift[0].lower()

    def test_clean_plugin_classifies_valid(self) -> None:
        candidate = PluginCandidate(
            manifest=_make_manifest("clean", tool_names=("X",)),
            root_dir=Path("/tmp/fake"),
            manifest_path=Path("/tmp/fake/plugin.json"),
        )
        # Provide a fake registry view that exactly matches the manifest.
        shape = inspect_shape_from_candidate(
            candidate,
            registered_tools=("X",),
            registered_channels=(),
            registered_providers=(),
            registered_hooks=(),
        )
        assert shape.classification == "valid"
        assert shape.drift == ()
        assert shape.declared_tools == ("X",)
        assert shape.actual_tools == ("X",)

    def test_undeclared_tool_in_drift(self) -> None:
        candidate = PluginCandidate(
            manifest=_make_manifest("d", tool_names=("X",)),
            root_dir=Path("/tmp/fake"),
            manifest_path=Path("/tmp/fake/plugin.json"),
        )
        shape = inspect_shape_from_candidate(
            candidate,
            registered_tools=("X", "Y"),  # Y not declared
            registered_channels=(),
            registered_providers=(),
            registered_hooks=(),
        )
        assert shape.classification == "drift"
        assert any("Y" in d for d in shape.drift)

    def test_declared_but_unregistered_tool_in_drift(self) -> None:
        candidate = PluginCandidate(
            manifest=_make_manifest("d", tool_names=("X", "Z")),
            root_dir=Path("/tmp/fake"),
            manifest_path=Path("/tmp/fake/plugin.json"),
        )
        shape = inspect_shape_from_candidate(
            candidate,
            registered_tools=("X",),  # Z missing
            registered_channels=(),
            registered_providers=(),
            registered_hooks=(),
        )
        assert shape.classification == "drift"
        assert any("Z" in d for d in shape.drift)
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_inspect_shape.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Create the shape inspector**

```python
# opencomputer/plugins/inspect_shape.py
"""Plugin shape classifier — compare manifest claims to actual registrations.

Sub-project G (openclaw-parity) Task 7. Mirrors openclaw ``inspect-shape.ts``
shape — reads what a plugin declares in ``plugin.json`` and what it
actually registers via ``register(api)``, then reports drift.

First-cut keeps two classifications: ``valid`` (declarations match
actuals) and ``drift`` (any divergence). Openclaw's full 4-shape model
(plain-capability / hybrid-capability / hook-only / non-capability)
defers to a follow-up.

Pure data — no side effects, no logging, no plugin loading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from opencomputer.plugins.discovery import PluginCandidate, discover, standard_search_paths

__all__ = [
    "PluginShape",
    "inspect_shape",
    "inspect_shape_from_candidate",
]


Classification = Literal["valid", "drift"]


@dataclass(frozen=True, slots=True)
class PluginShape:
    """Result of inspecting one plugin's shape."""

    plugin_id: str
    declared_tools: tuple[str, ...] = ()
    actual_tools: tuple[str, ...] = ()
    declared_channels: tuple[str, ...] = ()
    actual_channels: tuple[str, ...] = ()
    declared_providers: tuple[str, ...] = ()
    actual_providers: tuple[str, ...] = ()
    declared_hooks: tuple[str, ...] = ()
    actual_hooks: tuple[str, ...] = ()
    drift: tuple[str, ...] = ()
    classification: Classification = "valid"


def inspect_shape_from_candidate(
    candidate: PluginCandidate,
    *,
    registered_tools: tuple[str, ...],
    registered_channels: tuple[str, ...],
    registered_providers: tuple[str, ...],
    registered_hooks: tuple[str, ...],
) -> PluginShape:
    """Build a PluginShape from a candidate + a snapshot of what it
    actually registered. Pure — no side effects.

    Used by ``inspect_shape`` itself (with live registry data) and in
    tests (with synthetic registry tuples).
    """
    declared_tools = candidate.manifest.tool_names
    declared_channels: tuple[str, ...] = ()
    declared_providers: tuple[str, ...] = ()
    if candidate.manifest.setup is not None:
        declared_providers = tuple(p.id for p in candidate.manifest.setup.providers)
        declared_channels = tuple(c.id for c in candidate.manifest.setup.channels)
    declared_hooks: tuple[str, ...] = ()  # No declared-hook field in manifest yet.

    drift: list[str] = []

    declared_tools_set = set(declared_tools) | set(candidate.manifest.optional_tool_names)
    actual_tools_set = set(registered_tools)
    for missing in sorted(declared_tools_set - actual_tools_set):
        if missing in candidate.manifest.optional_tool_names:
            continue  # optional, fine if absent
        drift.append(f"tool {missing!r} declared but not registered")
    for extra in sorted(actual_tools_set - declared_tools_set):
        drift.append(f"tool {extra!r} registered but not declared")

    declared_channels_set = set(declared_channels)
    actual_channels_set = set(registered_channels)
    for missing in sorted(declared_channels_set - actual_channels_set):
        drift.append(f"channel {missing!r} declared but not registered")
    for extra in sorted(actual_channels_set - declared_channels_set):
        drift.append(f"channel {extra!r} registered but not declared")

    declared_providers_set = set(declared_providers)
    actual_providers_set = set(registered_providers)
    for missing in sorted(declared_providers_set - actual_providers_set):
        drift.append(f"provider {missing!r} declared but not registered")
    for extra in sorted(actual_providers_set - declared_providers_set):
        drift.append(f"provider {extra!r} registered but not declared")

    classification: Classification = "drift" if drift else "valid"

    return PluginShape(
        plugin_id=candidate.manifest.id,
        declared_tools=tuple(declared_tools),
        actual_tools=tuple(sorted(actual_tools_set)),
        declared_channels=tuple(declared_channels),
        actual_channels=tuple(sorted(actual_channels_set)),
        declared_providers=tuple(declared_providers),
        actual_providers=tuple(sorted(actual_providers_set)),
        declared_hooks=tuple(declared_hooks),
        actual_hooks=tuple(sorted(registered_hooks)),
        drift=tuple(drift),
        classification=classification,
    )


def inspect_shape(plugin_id: str) -> PluginShape:
    """Inspect a plugin by id. Returns a PluginShape; never raises.

    Behavior:
    - Plugin id not found in discovery → drift shape with "plugin not loaded".
    - Plugin id found + loaded → real comparison.
    - Plugin id found + load failure → drift shape with the load error.

    Reads from the live ``ToolRegistry`` / ``ChannelDirectory`` /
    ``HookEngine`` / plugin registry to figure out what was actually
    registered.
    """
    candidates = discover(standard_search_paths())
    matched = next((c for c in candidates if c.manifest.id == plugin_id), None)
    if matched is None:
        return PluginShape(
            plugin_id=plugin_id,
            classification="drift",
            drift=(f"plugin {plugin_id!r} not loaded — no candidate found in search paths",),
        )

    # Try to load the plugin (best-effort) so the registry reflects its
    # registrations. If the plugin is already loaded, the loader is
    # idempotent. If load raises, we capture the error in drift.
    actual_tools: tuple[str, ...] = ()
    actual_channels: tuple[str, ...] = ()
    actual_providers: tuple[str, ...] = ()
    actual_hooks: tuple[str, ...] = ()
    load_error: str | None = None
    try:
        from opencomputer.plugins.loader import load_plugin
        from opencomputer.plugins.registry import registry as plugin_registry

        # PluginRegistry.loaded is list[LoadedPlugin] — iterate to find ours.
        loaded: object | None = None
        for lp in plugin_registry.loaded:
            if lp.candidate.manifest.id == plugin_id:
                loaded = lp
                break
        if loaded is None:
            load_plugin(matched, plugin_registry)
            for lp in plugin_registry.loaded:
                if lp.candidate.manifest.id == plugin_id:
                    loaded = lp
                    break
        if loaded is not None:
            regs = loaded.registrations
            actual_tools = tuple(sorted(regs.tool_names))
            actual_channels = tuple(sorted(regs.channel_names))
            actual_providers = tuple(sorted(regs.provider_names))
            # hook_specs is identity-keyed (no names); count only.
            actual_hooks = tuple(f"hook[{i}]" for i in range(len(regs.hook_specs)))
    except Exception as e:  # noqa: BLE001
        load_error = f"load failed: {type(e).__name__}: {e}"

    shape = inspect_shape_from_candidate(
        matched,
        registered_tools=actual_tools,
        registered_channels=actual_channels,
        registered_providers=actual_providers,
        registered_hooks=actual_hooks,
    )
    if load_error is not None:
        # Append the load error to existing drift list and force drift
        # classification, replacing any "valid" verdict.
        new_drift = (load_error, *shape.drift)
        from dataclasses import replace
        shape = replace(shape, drift=new_drift, classification="drift")
    return shape
```

> **Registry shape (verified during self-audit):** `PluginRegistry.loaded` is `list[LoadedPlugin]`; iterate to find the one matching `plugin_id`. Each `LoadedPlugin.registrations` is a `PluginRegistrations` dataclass exposing `tool_names: tuple[str, ...]`, `channel_names: tuple[str, ...]`, `provider_names: tuple[str, ...]`, and `hook_specs: tuple[Any, ...]` (hooks are identity-tracked, not name-tracked, so we surface `hook[i]` placeholders). The test for this task uses `inspect_shape_from_candidate` directly (no registry dep) — that test ships green regardless. The live `inspect_shape()` integration is exercised by Task 12's smoke test.

- [ ] **Step 4: Run unit tests**

```
pytest tests/test_inspect_shape.py -v
```

Expected: PASS — all 4 tests green.

- [ ] **Step 5: Add `plugin inspect <id>` Typer subcommand**

The CLI subcommand is exposed via `app.add_typer(plugin_app, name="plugin")` in `cli.py:2763`, so user-facing form is `opencomputer plugin inspect <id>` (singular). In `opencomputer/cli_plugin.py`, locate the `plugin_app` Typer instance (line 46) and add:

```python
@plugin_app.command("inspect")
def plugin_inspect(plugin_id: str) -> None:
    """Inspect a plugin's shape — compare manifest claims to actual registrations."""
    from opencomputer.plugins.inspect_shape import inspect_shape

    shape = inspect_shape(plugin_id)
    typer.echo(f"Plugin: {shape.plugin_id}")
    typer.echo(f"Status: {shape.classification}")
    typer.echo("")
    typer.echo("Declared tools (manifest):")
    for t in shape.declared_tools or ("(none)",):
        typer.echo(f"  - {t}")
    typer.echo("Actual tools (registered):")
    for t in shape.actual_tools or ("(none)",):
        typer.echo(f"  - {t}")
    typer.echo("")
    typer.echo("Declared providers (manifest):")
    for p in shape.declared_providers or ("(none)",):
        typer.echo(f"  - {p}")
    typer.echo("Actual providers (registered):")
    for p in shape.actual_providers or ("(none)",):
        typer.echo(f"  - {p}")
    if shape.drift:
        typer.echo("")
        typer.echo("DRIFT:")
        for d in shape.drift:
            typer.echo(f"  - {d}")
        raise typer.Exit(code=1)
```

- [ ] **Step 6: Smoke-test the CLI**

```
python -m opencomputer plugin inspect anthropic-provider 2>&1 | head -20
```

Expected output (or similar):
```
Plugin: anthropic-provider
Status: valid
...
```

If the registry-attribute lookup doesn't match (because `loaded` doesn't expose `tool_names` etc.), the output will say drift "load failed: AttributeError" — that's fine for now. The follow-up Task 9 integration test pins the contract.

- [ ] **Step 7: Run full suite**

```
pytest tests/ -x -q 2>&1 | tail -10
```

Expected: 918 PASS.

- [ ] **Step 8: Commit**

```bash
git add opencomputer/plugins/inspect_shape.py opencomputer/cli_plugin.py tests/test_inspect_shape.py
git commit -m "feat(plugins): inspect_shape + opencomputer plugins inspect <id>"
```

---

## Task 8: `SecretRef` wire primitive + `SecretResolver`

**Files:**
- Create: `plugin_sdk/wire_primitives.py`
- Modify: `plugin_sdk/__init__.py`
- Test: `tests/test_secret_ref.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_secret_ref.py
"""SecretRef wire primitive + SecretResolver registry."""

from __future__ import annotations

import json
import uuid

import pytest

from plugin_sdk.wire_primitives import SecretRef, SecretResolver


class TestSecretRef:
    def test_dump_does_not_contain_value(self) -> None:
        ref = SecretRef(ref_id="abc123", hint="anthropic-api-key")
        dumped = ref.model_dump()
        assert dumped == {"$secret_ref": "abc123", "hint": "anthropic-api-key"}
        # The actual secret value should never live on the ref itself.
        assert "value" not in dumped
        assert "secret" not in dumped

    def test_json_roundtrip_preserves_ref(self) -> None:
        ref = SecretRef(ref_id="abc123", hint="x")
        s = json.dumps(ref.model_dump())
        loaded = json.loads(s)
        assert loaded == {"$secret_ref": "abc123", "hint": "x"}

    def test_hint_optional_default_empty(self) -> None:
        ref = SecretRef(ref_id="xyz")
        assert ref.hint == ""
        assert ref.model_dump() == {"$secret_ref": "xyz", "hint": ""}

    def test_repr_does_not_leak(self) -> None:
        # Even if someone accidentally prints a SecretRef, no value is
        # available because SecretRef doesn't carry one.
        ref = SecretRef(ref_id="abc")
        assert "abc" in repr(ref)
        assert "value" not in repr(ref).lower()


class TestSecretResolver:
    def test_register_and_resolve(self) -> None:
        resolver = SecretResolver()
        ref = resolver.register(value="sk-real-key", hint="anthropic")
        assert isinstance(ref, SecretRef)
        assert ref.hint == "anthropic"
        assert resolver.resolve(ref) == "sk-real-key"

    def test_resolve_unknown_returns_none(self) -> None:
        resolver = SecretResolver()
        ref = SecretRef(ref_id="never-registered")
        assert resolver.resolve(ref) is None

    def test_register_returns_unique_ref_ids(self) -> None:
        resolver = SecretResolver()
        r1 = resolver.register(value="v1")
        r2 = resolver.register(value="v2")
        assert r1.ref_id != r2.ref_id

    def test_resolve_by_ref_id_string(self) -> None:
        resolver = SecretResolver()
        ref = resolver.register(value="x")
        # Caller may have just the ref_id (e.g. parsed from wire).
        assert resolver.resolve_by_id(ref.ref_id) == "x"

    def test_resolvers_are_isolated(self) -> None:
        # Two resolver instances do not share state.
        r1 = SecretResolver()
        r2 = SecretResolver()
        ref = r1.register(value="only-in-r1")
        assert r1.resolve(ref) == "only-in-r1"
        assert r2.resolve(ref) is None

    def test_clear_purges_state(self) -> None:
        resolver = SecretResolver()
        ref = resolver.register(value="x")
        assert resolver.resolve(ref) == "x"
        resolver.clear()
        assert resolver.resolve(ref) is None
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_secret_ref.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Create the wire-primitives module**

```python
# plugin_sdk/wire_primitives.py
"""Typed wire primitives that should never leak through the protocol.

Sub-project G (openclaw-parity) Task 8. Provides ``SecretRef`` — an
opaque reference to a secret (API key, OAuth token, …) that the wire
serializes as a ref-id only, not the value. Resolution happens in-process
via ``SecretResolver`` which never serializes the registry.

Mirrors openclaw ``primitives.secretref.test.ts`` — secret references
are a typed primitive whose ``model_dump()`` cannot accidentally include
the value.

**Adoption pattern** (Task 6 in spec is opportunistic): use ``SecretRef``
in NEW wire methods that carry credentials (e.g. ``auth.set_token``),
not in existing ``params: dict[str, Any]`` callsites. Migrating the
existing call sites is a separate hardening pass.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

__all__ = [
    "SecretRef",
    "SecretResolver",
]


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Opaque reference to a secret. The wire transport never serializes
    the value; only ``ref_id`` and ``hint`` (which is safe to log).

    Construct directly only when you already have a ref_id (e.g. parsed
    from wire). Most callers should use ``SecretResolver.register(value)``
    which generates a fresh ref_id and stashes the value in-process.
    """

    ref_id: str
    hint: str = ""

    def model_dump(self) -> dict[str, str]:
        """Wire representation — explicit ``$secret_ref`` discriminator
        so receivers can detect a SecretRef inside an arbitrary
        ``dict[str, Any]`` params blob."""
        return {"$secret_ref": self.ref_id, "hint": self.hint}


class SecretResolver:
    """Per-process registry mapping ref_id → secret value.

    Intentionally NOT thread-safe — callers wrap with their own lock if
    they share a resolver across threads. Intentionally NOT pickled —
    serializing a resolver would defeat the purpose of SecretRef.

    A resolver instance is the natural unit of secret-scope (per-session,
    per-call, per-test). Two resolvers don't share state — see
    test_resolvers_are_isolated.
    """

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def register(self, *, value: str, hint: str = "") -> SecretRef:
        """Stash ``value`` and return a SecretRef carrying a fresh
        ref_id + the provided hint. The value never leaves this
        resolver — it's not stored on the SecretRef itself."""
        ref_id = uuid.uuid4().hex
        self._values[ref_id] = value
        return SecretRef(ref_id=ref_id, hint=hint)

    def resolve(self, ref: SecretRef) -> str | None:
        """Return the value for ``ref``, or None if this resolver doesn't
        know the ref_id (different resolver, expired, etc.)."""
        return self._values.get(ref.ref_id)

    def resolve_by_id(self, ref_id: str) -> str | None:
        """Same as ``resolve`` but for callers that only have the
        ref_id string (e.g. parsed from wire JSON)."""
        return self._values.get(ref_id)

    def clear(self) -> None:
        """Drop all registered secrets. Test helper / cleanup."""
        self._values.clear()
```

- [ ] **Step 4: Export from the SDK**

In `plugin_sdk/__init__.py`:

```python
from plugin_sdk.wire_primitives import SecretRef, SecretResolver
```

And `__all__`:

```python
    "SecretRef",
    "SecretResolver",
```

- [ ] **Step 5: Run tests**

```
pytest tests/test_secret_ref.py -v
```

Expected: PASS — all 11 tests green.

- [ ] **Step 6: Verify SDK boundary still clean**

```
pytest tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v
```

Expected: PASS — SecretRef/SecretResolver don't import from opencomputer.

- [ ] **Step 7: Run full suite**

```
pytest tests/ -x -q 2>&1 | tail -10
```

Expected: 929 PASS.

- [ ] **Step 8: Commit**

```bash
git add plugin_sdk/wire_primitives.py plugin_sdk/__init__.py tests/test_secret_ref.py
git commit -m "feat(plugin-sdk): SecretRef + SecretResolver wire primitive"
```

---

## Task 9: Typed `ErrorCode` enum + `WireResponse.code` field

**Files:**
- Create: `opencomputer/gateway/error_codes.py`
- Modify: `opencomputer/gateway/protocol.py`
- Modify: `opencomputer/gateway/protocol_v2.py`
- Test: `tests/test_error_codes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_error_codes.py
"""Typed ErrorCode enum + WireResponse.code field."""

from __future__ import annotations

import pytest

from opencomputer.gateway.error_codes import ErrorCode
from opencomputer.gateway.protocol import WireResponse


class TestErrorCode:
    def test_value_is_string(self) -> None:
        assert ErrorCode.PLUGIN_NOT_FOUND.value == "plugin_not_found"
        assert isinstance(ErrorCode.PLUGIN_NOT_FOUND.value, str)

    def test_str_enum_compares_to_string(self) -> None:
        # StrEnum semantics: enum value == its string value.
        assert ErrorCode.TOOL_DENIED == "tool_denied"

    def test_all_codes_lowercase_snake(self) -> None:
        import re

        for code in ErrorCode:
            assert re.match(r"^[a-z][a-z0-9_]*$", code.value), (
                f"{code.name}={code.value!r} not snake_case"
            )

    def test_codes_cover_expected_categories(self) -> None:
        names = {c.name for c in ErrorCode}
        for required in (
            "PLUGIN_NOT_FOUND",
            "PLUGIN_INCOMPATIBLE",
            "PROVIDER_AUTH_FAILED",
            "TOOL_DENIED",
            "CONSENT_BLOCKED",
            "METHOD_NOT_FOUND",
            "INVALID_PARAMS",
            "INTERNAL_ERROR",
            "RATE_LIMITED",
            "SESSION_NOT_FOUND",
        ):
            assert required in names, f"missing ErrorCode.{required}"


class TestWireResponseCode:
    def test_default_code_none(self) -> None:
        r = WireResponse(id="1", ok=True)
        assert r.code is None

    def test_explicit_code_persists(self) -> None:
        r = WireResponse(
            id="1",
            ok=False,
            error="not found",
            code=ErrorCode.PLUGIN_NOT_FOUND.value,
        )
        assert r.code == "plugin_not_found"

    def test_back_compat_old_response_without_code_parses(self) -> None:
        # Existing wire callers send only error: str
        r = WireResponse(id="1", ok=False, error="boom")
        assert r.code is None
        assert r.error == "boom"

    def test_round_trip_through_dict(self) -> None:
        r = WireResponse(
            id="1",
            ok=False,
            error="x",
            code=ErrorCode.TOOL_DENIED.value,
        )
        d = r.model_dump()
        r2 = WireResponse.model_validate(d)
        assert r2.code == "tool_denied"
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_error_codes.py -v
```

Expected: FAIL — module not found, code field unknown on WireResponse.

- [ ] **Step 3: Create the enum module**

```python
# opencomputer/gateway/error_codes.py
"""Typed wire error codes — programmable categories.

Sub-project G (openclaw-parity) Task 9. ``WireResponse.error`` is opaque
text. Wire clients (TUI, IDE bridges) cannot ``match`` on errors. This
enum gives them a programmable category that round-trips through JSON
as a stable snake_case string.

Mirrors openclaw ``error-codes.ts`` shape from
``sources/openclaw-2026.4.23/src/gateway/protocol/schema/error-codes.ts``.

Add new codes to the END of the enum; never renumber. Existing wire
callers tolerate unknown codes gracefully (treat as INTERNAL_ERROR).
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ErrorCode"]


class ErrorCode(StrEnum):
    """Programmable error categories for ``WireResponse.code``.

    The string value is the wire shape — lowercase snake_case. Use
    ``.value`` when serializing if you want a pure ``str``; the enum
    itself compares equal to its value for client convenience.
    """

    # Plugin lifecycle
    PLUGIN_NOT_FOUND = "plugin_not_found"
    PLUGIN_INCOMPATIBLE = "plugin_incompatible"

    # Auth / provider
    PROVIDER_AUTH_FAILED = "provider_auth_failed"

    # Tools / consent
    TOOL_DENIED = "tool_denied"
    CONSENT_BLOCKED = "consent_blocked"

    # Wire-protocol layer
    METHOD_NOT_FOUND = "method_not_found"
    INVALID_PARAMS = "invalid_params"

    # Reliability
    INTERNAL_ERROR = "internal_error"
    RATE_LIMITED = "rate_limited"

    # Session
    SESSION_NOT_FOUND = "session_not_found"
```

- [ ] **Step 4: Extend `WireResponse` with `code` field**

In `opencomputer/gateway/protocol.py`, modify `WireResponse`:

```python
class WireResponse(BaseModel):
    type: Literal["res"] = "res"
    id: str
    ok: bool
    payload: dict[str, Any] | None = None
    error: str | None = None
    # Task 9 — programmable error category. Optional; old clients ignore.
    # Value mirrors ErrorCode enum (snake_case strings) so wire is stable
    # even if the enum gains new codes later.
    code: str | None = None
```

- [ ] **Step 5: Re-export from `protocol_v2`**

In `opencomputer/gateway/protocol_v2.py`, add to the `from opencomputer.gateway.protocol import (...)` block:

```python
# (no change needed — WireResponse re-exports automatically since it's
# the same class)
```

And add `ErrorCode` to the existing imports + `__all__`:

```python
from opencomputer.gateway.error_codes import ErrorCode
```

```python
    "ErrorCode",
```

- [ ] **Step 6: Run tests**

```
pytest tests/test_error_codes.py -v
```

Expected: PASS — all 8 tests green.

- [ ] **Step 7: Run full suite**

```
pytest tests/ -x -q 2>&1 | tail -10
```

Expected: 937 PASS.

- [ ] **Step 8: Commit**

```bash
git add opencomputer/gateway/error_codes.py opencomputer/gateway/protocol.py opencomputer/gateway/protocol_v2.py tests/test_error_codes.py
git commit -m "feat(gateway): typed ErrorCode enum + WireResponse.code field"
```

---

## Task 10: `min_host_version` enforcement at load time

**Files:**
- Modify: `opencomputer/plugins/loader.py`
- Test: extend `tests/test_min_host_version.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_min_host_version.py`:

```python
class TestMinHostVersionEnforcement:
    """At load time, mismatch raises with both versions in the message."""

    def test_load_passes_when_no_pin(self) -> None:
        # Empty min_host_version always passes regardless of host.
        from opencomputer.plugins.loader import _check_min_host_version

        _check_min_host_version(plugin_id="x", min_host_version="", host_version="2026.4.27")
        # No raise = pass.

    def test_load_passes_when_host_higher(self) -> None:
        from opencomputer.plugins.loader import _check_min_host_version

        _check_min_host_version(
            plugin_id="x", min_host_version="2026.1.1", host_version="2026.4.27"
        )

    def test_load_passes_when_host_equal(self) -> None:
        from opencomputer.plugins.loader import _check_min_host_version

        _check_min_host_version(
            plugin_id="x", min_host_version="2026.4.27", host_version="2026.4.27"
        )

    def test_load_raises_when_host_lower(self) -> None:
        from opencomputer.plugins.loader import (
            PluginIncompatibleError,
            _check_min_host_version,
        )

        with pytest.raises(PluginIncompatibleError) as ei:
            _check_min_host_version(
                plugin_id="x", min_host_version="2026.5.0", host_version="2026.4.27"
            )
        msg = str(ei.value)
        assert "x" in msg
        assert "2026.5.0" in msg
        assert "2026.4.27" in msg
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_min_host_version.py::TestMinHostVersionEnforcement -v
```

Expected: FAIL — `_check_min_host_version` not defined / `PluginIncompatibleError` not exported.

- [ ] **Step 3: Add the enforcement helper to loader**

In `opencomputer/plugins/loader.py`, near the top with other definitions:

```python
class PluginIncompatibleError(RuntimeError):
    """Raised at load time when a plugin's min_host_version is greater
    than the running ``opencomputer.__version__``. Halts that plugin's
    load — others continue."""


def _check_min_host_version(
    *, plugin_id: str, min_host_version: str, host_version: str
) -> None:
    """Compare a plugin's ``min_host_version`` to the running host.

    Empty min_host_version skips the check (back-compat). Otherwise
    parse with packaging.version.Version and raise
    PluginIncompatibleError on mismatch.

    Task 10 (openclaw-parity).
    """
    if not min_host_version:
        return
    from packaging.version import InvalidVersion, Version

    try:
        required = Version(min_host_version)
        current = Version(host_version)
    except InvalidVersion as e:
        # Should already have been caught at validate_manifest time;
        # if it slipped through, fail closed with a clear message.
        raise PluginIncompatibleError(
            f"plugin {plugin_id!r} declares unparseable min_host_version "
            f"{min_host_version!r}: {e}"
        ) from e
    if current < required:
        raise PluginIncompatibleError(
            f"plugin {plugin_id!r} requires opencomputer >= "
            f"{min_host_version} but host is {host_version}"
        )
```

Then wire it into `load_plugin` near the start, after the candidate is extracted:

```python
def load_plugin(candidate: PluginCandidate, registry: PluginRegistry) -> ...:
    # ... existing prelude ...

    # Task 10 — enforce min_host_version BEFORE we import the plugin's
    # entry module. A version mismatch never invokes plugin code.
    import opencomputer

    _check_min_host_version(
        plugin_id=candidate.manifest.id,
        min_host_version=candidate.manifest.min_host_version,
        host_version=opencomputer.__version__,
    )

    # ... existing import + register flow ...
```

> **Behavior contract:** The current `load_plugin` semantics: failures bubble. Caller (`PluginRegistry.load`) is expected to catch + log + skip the plugin. Confirm this matches existing behavior; if `load_plugin` swallows exceptions today, do the same here. The test above only exercises `_check_min_host_version` directly, so it's stable regardless.

- [ ] **Step 4: Run tests**

```
pytest tests/test_min_host_version.py::TestMinHostVersionEnforcement -v
```

Expected: PASS — all 4 tests green.

- [ ] **Step 5: Run full suite**

```
pytest tests/ -x -q 2>&1 | tail -10
```

Expected: 941 PASS.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/plugins/loader.py tests/test_min_host_version.py
git commit -m "feat(plugins): enforce min_host_version at load time"
```

---

## Task 11: Extension boundary test + frozen inventory

**Files:**
- Create: `tests/test_plugin_extension_boundary.py`
- Create: `tests/fixtures/plugin_extension_import_boundary_inventory.json`
- Create: `scripts/refresh_extension_boundary_inventory.py`

- [ ] **Step 1: Build the inventory generator**

```python
# scripts/refresh_extension_boundary_inventory.py
"""Refresh the frozen-inventory fixture for the extension-boundary test.

Walks ``extensions/*/**.py`` and records every ``from opencomputer.X
import Y`` / ``import opencomputer.X``. Output is a JSON file mapping
relative-path → sorted list of imported modules.

Run when an extension is removed OR an existing extension's imports
legitimately need to change. NEW extensions should not introduce new
``opencomputer.*`` imports — the boundary test will fail in that case.

Sub-project G (openclaw-parity) Task 11. Mirrors openclaw
``test/fixtures/plugin-extension-import-boundary-inventory.json``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

INVENTORY_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "plugin_extension_import_boundary_inventory.json"
)
EXTENSIONS_DIR = Path(__file__).resolve().parent.parent / "extensions"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _collect_imports(py_path: Path) -> list[str]:
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "opencomputer" or mod.startswith("opencomputer."):
                found.add(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "opencomputer" or alias.name.startswith("opencomputer."):
                    found.add(alias.name)
    return sorted(found)


def main() -> int:
    inventory: dict[str, list[str]] = {}
    for py in sorted(EXTENSIONS_DIR.rglob("*.py")):
        rel = py.relative_to(REPO_ROOT).as_posix()
        imports = _collect_imports(py)
        if imports:
            inventory[rel] = imports
    INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY_PATH.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(inventory)} entries to {INVENTORY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Generate the seed inventory**

```
python scripts/refresh_extension_boundary_inventory.py
```

Expected: writes `tests/fixtures/plugin_extension_import_boundary_inventory.json` with ~27 entries.

- [ ] **Step 3: Inspect the generated inventory**

```
ls -la tests/fixtures/plugin_extension_import_boundary_inventory.json
head -40 tests/fixtures/plugin_extension_import_boundary_inventory.json
```

Confirm it lists `extensions/anthropic-provider/provider.py` etc. with their actual imports. This file is committed.

- [ ] **Step 4: Write the boundary test**

```python
# tests/test_plugin_extension_boundary.py
"""Boundary test — extensions may only import from plugin_sdk.

Extensions that currently import from ``opencomputer.*`` are listed in a
frozen inventory. The test FAILS when:

  (a) An extension introduces a NEW ``from opencomputer.*`` import not
      in the inventory, OR
  (b) The inventory has a stale entry (file removed / renamed).

To resolve a failure:

  - If you legitimately need a new core import: bring it through
    ``plugin_sdk`` instead, then update the inventory only as a last
    resort with ``python scripts/refresh_extension_boundary_inventory.py``.
  - If you removed a file or stopped using a core import: regenerate the
    inventory the same way.

Sub-project G (openclaw-parity) Task 11.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS_DIR = REPO_ROOT / "extensions"
INVENTORY_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "plugin_extension_import_boundary_inventory.json"
)


def _collect_imports(py_path: Path) -> list[str]:
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "opencomputer" or mod.startswith("opencomputer."):
                found.add(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "opencomputer" or alias.name.startswith("opencomputer."):
                    found.add(alias.name)
    return sorted(found)


def _live_inventory() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for py in sorted(EXTENSIONS_DIR.rglob("*.py")):
        rel = py.relative_to(REPO_ROOT).as_posix()
        imports = _collect_imports(py)
        if imports:
            out[rel] = imports
    return out


def _frozen_inventory() -> dict[str, list[str]]:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


class TestExtensionBoundary:
    def test_inventory_file_exists(self) -> None:
        assert INVENTORY_PATH.exists(), (
            f"frozen inventory not found at {INVENTORY_PATH} — "
            "run `python scripts/refresh_extension_boundary_inventory.py`"
        )

    def test_no_new_violations(self) -> None:
        live = _live_inventory()
        frozen = _frozen_inventory()
        new_files = sorted(set(live) - set(frozen))
        assert not new_files, (
            "NEW extension files import from opencomputer.* "
            "(should import from plugin_sdk only):\n  "
            + "\n  ".join(f"{f}: {live[f]}" for f in new_files)
            + "\n\nFix by routing the import through plugin_sdk, OR if truly "
            "unavoidable, run `python scripts/refresh_extension_boundary_inventory.py` "
            "to update the inventory."
        )

    def test_no_new_imports_in_existing_files(self) -> None:
        live = _live_inventory()
        frozen = _frozen_inventory()
        new_imports: list[str] = []
        for f, imports in live.items():
            if f not in frozen:
                continue
            extras = sorted(set(imports) - set(frozen[f]))
            if extras:
                new_imports.append(f"{f}: {extras}")
        assert not new_imports, (
            "Existing extension files added NEW opencomputer.* imports:\n  "
            + "\n  ".join(new_imports)
            + "\n\nFix by routing through plugin_sdk, OR run "
            "`python scripts/refresh_extension_boundary_inventory.py`."
        )

    def test_no_stale_inventory_entries(self) -> None:
        live = _live_inventory()
        frozen = _frozen_inventory()
        stale = sorted(set(frozen) - set(live))
        assert not stale, (
            "Inventory has entries for files that no longer exist or no "
            "longer import opencomputer.*:\n  "
            + "\n  ".join(stale)
            + "\n\nRun `python scripts/refresh_extension_boundary_inventory.py` "
            "to clean up."
        )
```

- [ ] **Step 5: Run the boundary test**

```
pytest tests/test_plugin_extension_boundary.py -v
```

Expected: PASS — 4 tests green. (The `live == frozen` invariant holds the moment after seed generation.)

- [ ] **Step 6: Self-test the failure paths manually**

Add a temporary test to confirm the failure messages are useful:

```
# Temporary check — DO NOT commit this part.
echo 'from opencomputer.agent.loop import AgentLoop' >> extensions/dev-tools/__init__.py
pytest tests/test_plugin_extension_boundary.py -v
git checkout extensions/dev-tools/__init__.py
```

Expected: failure message names `extensions/dev-tools/__init__.py` and `opencomputer.agent.loop`. Then revert.

- [ ] **Step 7: Run full suite**

```
pytest tests/ -x -q 2>&1 | tail -10
```

Expected: 945 PASS.

- [ ] **Step 8: Commit**

```bash
git add tests/test_plugin_extension_boundary.py tests/fixtures/plugin_extension_import_boundary_inventory.json scripts/refresh_extension_boundary_inventory.py
git commit -m "test: extension boundary test + frozen inventory of current violators"
```

---

## Task 12: Docs + CHANGELOG

**Files:**
- Modify: `OpenComputer/CLAUDE.md`
- Modify: `OpenComputer/CHANGELOG.md`
- Modify: `OpenComputer/extensions/anthropic-provider/plugin.json` (smoke test)

- [ ] **Step 1: Set `min_host_version` on a bundled plugin as a smoke test**

Current host version (per `pyproject.toml`) is `2026.4.27`. Pick a value strictly less than that so the check fires + passes:

In `extensions/anthropic-provider/plugin.json`, add the field:

```json
{
  "id": "anthropic-provider",
  ...
  "min_host_version": "2026.1.1",
  ...
}
```

- [ ] **Step 2: Verify it loads cleanly**

```
python -m opencomputer plugin inspect anthropic-provider 2>&1 | head -10
```

Expected: `Status: valid` — manifest pin (`2026.1.1`) is satisfied by current host (`2026.4.27`); `_check_min_host_version` permits the load.

- [ ] **Step 3: Update CLAUDE.md manifest reference section**

Find the section in `OpenComputer/CLAUDE.md` near the manifest discussion (search "Plugin registration is Python-declarative") and add a paragraph after it:

```markdown
**Manifest schema v4 fields (added 2026-05-03):**

- `min_host_version` (string) — minimum `opencomputer.__version__` required. Empty = no check.
- `activation` (object) — manifest-declared triggers: `on_providers`, `on_channels`, `on_commands`, `on_tools`, `on_models`. Falls back to `tool_names` when absent.
- `setup.providers[].auth_choices` (array) — rich auth UI metadata: per-method `label`, `cli_flag`, `option_key`, `group`, `onboarding_priority`. Falls back to `auth_methods: list[str]` when empty.

All v4 fields are optional; v3 manifests parse unchanged.
```

- [ ] **Step 4: Update CHANGELOG**

In `OpenComputer/CHANGELOG.md`, under the `## [Unreleased]` section, add:

```markdown
### Added
- Manifest schema v4 fields (`min_host_version`, `activation`, `setup.providers[].auth_choices`).
- `opencomputer plugin inspect <id>` — compare manifest claims to actual registrations.
- `SecretRef` + `SecretResolver` typed wire primitives in `plugin_sdk.wire_primitives`.
- `ErrorCode` typed enum in `opencomputer.gateway.error_codes` + `WireResponse.code` field.
- JSON5 tolerance for `plugin.json` (comments + trailing commas).
- 256KB cap on `plugin.json` size at discovery.
- Extension boundary test (`tests/test_plugin_extension_boundary.py`) with frozen inventory of legacy violators.

### Changed
- `discovery._parse_manifest` is now two-tier (json → json5 fallback).
- `loader.load_plugin` enforces `min_host_version` before importing entry module.
```

- [ ] **Step 5: Final full suite + lint**

```
pytest tests/ -x -q 2>&1 | tail -10
ruff check opencomputer/ plugin_sdk/ extensions/ tests/ scripts/
```

Expected: 945 PASS, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/CLAUDE.md OpenComputer/CHANGELOG.md OpenComputer/extensions/anthropic-provider/plugin.json
git commit -m "docs(superpowers): record openclaw-parity port — manifest v4 + new modules"
```

---

## Task 13: Open the PR

- [ ] **Step 1: Push the branch**

```
git push -u origin HEAD
```

- [ ] **Step 2: Open the PR**

```
gh pr create --title "feat(plugins): openclaw-parity port — 9 items, manifest v4" --body "$(cat <<'EOF'
## Summary

Ports nine load-bearing pieces of openclaw's plugin/wire contract that
OpenComputer's reference-import phase missed. Schema bump v3 → v4; every
new field optional, so v3 manifests still parse.

- min_host_version pinning at load
- extension boundary test (frozen-inventory pattern, advisory mode)
- activation block in manifest + planner
- SecretRef wire primitive
- plugins inspect <id> + shape classifier
- typed ErrorCode enum
- JSON5 manifest tolerance
- 256KB manifest size cap
- providerAuthChoices rich auth UI metadata

Out of scope: plugin_sdk subpath split (item #10 — plugin-author migration,
own PR), boundary-violator cleanup (separate per-extension PRs), existing
wire-method SecretRef migration (opportunistic only).

Spec: docs/superpowers/specs/2026-05-03-openclaw-parity-port-design.md
Plan: docs/superpowers/plans/2026-05-03-openclaw-parity-port.md

## Test plan

- [ ] pytest: 945 tests pass (885 → 945 = +60 new)
- [ ] ruff clean
- [ ] opencomputer plugin inspect anthropic-provider returns Status: valid
- [ ] Extension boundary test passes against frozen inventory
- [ ] No regressions in existing 885 tests

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Verify CI is green**

```
gh pr checks
```

Expected: all checks pass.

---

## Self-Review (writing-plans skill checklist)

**1. Spec coverage:** Spec items 4.1 through 4.9 each map to specific tasks:
- 4.1 min_host_version → Task 1 (field) + Task 10 (enforcement)
- 4.2 extension boundary → Task 11
- 4.3 activation block → Task 2 (field) + Task 6 (planner)
- 4.4 SecretRef → Task 8
- 4.5 inspect_shape → Task 7
- 4.6 ErrorCode → Task 9
- 4.7 JSON5 → Task 4
- 4.8 256KB cap → Task 5
- 4.9 auth_choices → Task 3
All covered.

**2. Placeholder scan:** No "TBD" / "TODO" / "fill in" / "similar to" placeholders. Each step has actual code.

**3. Type consistency:**
- `PluginActivation` referenced in Task 6 matches the dataclass defined in Task 2.
- `SecretRef` / `SecretResolver` referenced in Task 8 are defined there; not used elsewhere.
- `ActivationTriggers` defined in Task 6.
- `PluginShape` defined in Task 7.
- `ErrorCode` defined in Task 9 used in test names there only.
- `_check_min_host_version` and `PluginIncompatibleError` defined in Task 10.
- `MAX_MANIFEST_BYTES` exported from `discovery.py` in Task 5; imported in Task 5's tests.

All names used in later tasks match earlier definitions. No drift.

**4. Test names + commands:** All `pytest` invocations use `-v` and the explicit test path. All commit messages follow the conventional `feat:` / `test:` / `docs:` prefixes already used on `main`.
