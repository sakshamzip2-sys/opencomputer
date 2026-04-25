"""Typed validator for `plugin.json` manifests.

`discovery._parse_manifest` does only minimal field-presence checks. This
validator runs the same parsed dict through a pydantic model so:

- Wrong types (e.g. `enabled: "yes"` instead of `true`) get rejected with
  a useful message.
- Unknown `kind` values fail at scan time, not at runtime.
- Required fields (id, name, version, entry) are enforced uniformly.
- Empty `entry` (a common copy-paste bug — generates an unimportable
  plugin) is caught up front.

Source: openclaw's `src/plugins/manifest.ts` validator pattern. Phase 12g.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: Plugin id format: lowercase letters, digits, hyphens. No leading/trailing
#: hyphens. 1-64 chars. Matches openclaw's id rules.
_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

#: Semver-ish — accept M.m.p, M.m, or M (people forget patch numbers).
#: Pre-release/build metadata after a hyphen is allowed.
_VERSION_RE = re.compile(r"^\d+(\.\d+){0,2}(?:-[\w.]+)?$")


PluginKind = Literal["channel", "provider", "tool", "skill", "mixed"]


class SetupProviderSchema(BaseModel):
    """Typed mirror of `plugin_sdk.core.SetupProvider` for validation only.

    Sub-project G.23 (Tier 4 OpenClaw port). Mirrors OpenClaw's
    ``PluginManifestSetupProvider`` shape from
    ``sources/openclaw-2026.4.23/src/plugins/manifest.ts:76-83``.
    Sub-project G.24 adds the display fields used by the setup wizard.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    auth_methods: list[str] = Field(default_factory=list)
    env_vars: list[str] = Field(default_factory=list)
    # G.24 — wizard display fields. Optional; empty string means "no value".
    label: str = Field(default="", max_length=128)
    default_model: str = Field(default="", max_length=128)
    signup_url: str = Field(default="", max_length=512)

    @field_validator("auth_methods", "env_vars", mode="before")
    @classmethod
    def _drop_empty_strings(cls, v: object) -> object:
        if isinstance(v, list):
            return [s for s in v if isinstance(s, str) and s.strip()]
        return v


class PluginSetupSchema(BaseModel):
    """Typed mirror of `plugin_sdk.core.PluginSetup` for validation only."""

    model_config = ConfigDict(extra="forbid")

    providers: list[SetupProviderSchema] = Field(default_factory=list)
    requires_runtime: bool = Field(default=False)


class ModelSupportSchema(BaseModel):
    """Typed mirror of `plugin_sdk.core.ModelSupport` for validation only.

    Sub-project G.21 (Tier 4 OpenClaw port). Mirrors
    ``sources/openclaw-2026.4.23/src/plugins/manifest-registry.ts``'s
    ``modelSupport`` JSON shape. Extra fields rejected — mirrors the
    parent manifest's ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid")

    model_prefixes: list[str] = Field(default_factory=list)
    model_patterns: list[str] = Field(default_factory=list)

    @field_validator("model_prefixes", "model_patterns", mode="before")
    @classmethod
    def _drop_empty_strings(cls, v: object) -> object:
        # OpenClaw's tolerance pattern: silently drop empty / whitespace
        # entries (they'd match every model id and break prefix
        # resolution). See sources/openclaw-2026.4.23/src/plugins/
        # manifest.json5-tolerance.test.ts:84-99 ("normalizes
        # modelSupport metadata from the manifest").
        if isinstance(v, list):
            return [s for s in v if isinstance(s, str) and s.strip()]
        return v


class PluginManifestSchema(BaseModel):
    """Typed mirror of `plugin_sdk.core.PluginManifest` for validation only."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    entry: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    author: str = Field(default="", max_length=128)
    homepage: str = Field(default="", max_length=256)
    license: str = Field(default="MIT", max_length=64)
    kind: PluginKind = Field(default="mixed")
    # Phase 14.C
    profiles: list[str] | None = Field(default=None)
    single_instance: bool = Field(default=False)
    # Phase 12b5 Sub-project E — tool_names for demand-driven activation.
    # default_factory keeps existing manifests (without this field) valid
    # under extra="forbid".
    tool_names: list[str] = Field(default_factory=list)
    # Phase 12b1 Sub-project A — Honcho-as-default
    enabled_by_default: bool = Field(default=False)
    # Sub-project G.11 (Tier 2.13) — MCP catalog binding. Plugin-declared
    # MCP server presets that should be installed when this plugin
    # activates. Each entry is a preset slug from
    # ``opencomputer.mcp.presets.PRESETS`` (e.g. ``"filesystem"``,
    # ``"github"``). Default empty list means the plugin needs no MCPs.
    mcp_servers: list[str] = Field(default_factory=list)
    # Sub-project G.21 (Tier 4 OpenClaw port) — model-prefix
    # auto-activation for provider plugins. Default ``None`` means "no
    # model affinity"; legacy bundled providers without this field stay
    # backwards-compatible.
    model_support: ModelSupportSchema | None = Field(default=None)
    # Sub-project G.22 (Tier 4 OpenClaw port) — historical ids this
    # plugin used to be known by. The loader maps old user-config
    # references to the current id. Default empty list = never renamed.
    legacy_plugin_ids: list[str] = Field(default_factory=list)
    # Sub-project G.23 (Tier 4 OpenClaw port) — cheap setup metadata so
    # provider plugins self-describe their env-var / auth-method
    # requirements. Wizard + doctor read this without loading the
    # plugin's Python. Default ``None`` means "no declarations".
    setup: PluginSetupSchema | None = Field(default=None)
    # Phase 14.M/N — already in use via ProfileConfig/WorkspaceOverlay but
    # manifests often carry a schema_version field. Accept it silently.
    schema_version: int | None = Field(default=None)

    @field_validator("legacy_plugin_ids", mode="before")
    @classmethod
    def _drop_empty_legacy_ids(cls, v: object) -> object:
        # Same OpenClaw tolerance: empty / whitespace entries are bugs
        # that would silently match every empty id elsewhere. Drop them.
        if isinstance(v, list):
            return [s for s in v if isinstance(s, str) and s.strip()]
        return v

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(
                f"id {v!r} must be lowercase letters/digits/hyphens, "
                f"start+end with alphanumeric, 1-64 chars"
            )
        return v

    @field_validator("version")
    @classmethod
    def _version_format(cls, v: str) -> str:
        if not _VERSION_RE.match(v):
            raise ValueError(
                f"version {v!r} must be semver-ish (e.g. '1', '1.2', '1.2.3', '1.2.3-beta')"
            )
        return v

    @field_validator("entry")
    @classmethod
    def _entry_format(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("entry must not be empty")
        if "/" in v or "\\" in v or v.endswith(".py"):
            raise ValueError(
                f"entry {v!r} should be a Python module name (e.g. 'plugin'), "
                f"not a path or .py filename"
            )
        return v


def validate_manifest(data: dict[str, Any]) -> tuple[PluginManifestSchema | None, str]:
    """Validate a parsed plugin.json dict.

    Returns `(schema, "")` on success or `(None, error_message)` on
    failure. Caller should log the error and skip the candidate — never
    raise into the discovery loop, since one bad plugin shouldn't break
    all others.
    """
    try:
        schema = PluginManifestSchema.model_validate(data)
        return schema, ""
    except ValidationError as e:
        # Build a single-line error per field for the log.
        parts = []
        for err in e.errors():
            field = ".".join(str(x) for x in err["loc"]) or "<root>"
            parts.append(f"{field}: {err['msg']}")
        return None, "; ".join(parts)


__all__ = [
    "ModelSupportSchema",
    "PluginKind",
    "PluginManifestSchema",
    "PluginSetupSchema",
    "SetupProviderSchema",
    "validate_manifest",
]
