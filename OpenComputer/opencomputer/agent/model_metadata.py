"""Model metadata registry — context length + cost lookup (G.32 / Tier 4).

A small in-memory registry that answers two questions about any model
id without hitting an external pricing API:

* ``context_length(model_id)`` → max prompt+completion tokens.
* ``cost_per_million(model_id)`` → ``(input_usd_per_million,
  output_usd_per_million)`` tuple, or ``None`` if unknown.

The registry ships with a curated default catalog covering the models
OC users actually run today (Anthropic + OpenAI). New entries can be
contributed at runtime by plugins via ``register_model(...)`` so a
third-party provider plugin can teach core about its own models'
metadata without forking the catalog.

Why this lives in core (not the provider plugin):

* The cost-guard module (G.8) and CompactionEngine both want to read
  context-length / cost without instantiating the provider plugin.
  Putting the table here keeps those callers cheap.
* Provider plugins still own runtime decisions (auth, transport,
  schema mapping). They just contribute metadata to the shared
  registry on ``register()``.

The registry is process-local. There's no persistence — each fresh
agent process re-builds the table from the curated defaults plus
whatever plugins register. Mirrors how Hermes maintains its
``catalog`` map at ``sources/hermes-agent-2026.4.23/agent/catalog.py``.

Round 2A P-11 — user-curated entries via ``opencomputer models add`` are
persisted to ``<profile_home>/model_overrides.yaml`` and re-applied on
each :func:`apply_overrides_file` call. User overrides win over plugin-
shipped catalogs (the user explicitly asked for them), and plugin-
shipped entries always win over the curated defaults.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("opencomputer.agent.model_metadata")


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    """Per-model metadata. All numeric fields are nullable so callers
    can distinguish "not declared" from "declared as 0"."""

    model_id: str
    context_length: int | None = None
    """Max prompt+completion tokens. ``None`` means unknown."""

    input_usd_per_million: float | None = None
    """USD cost per 1,000,000 input tokens. ``None`` means unknown."""

    output_usd_per_million: float | None = None
    """USD cost per 1,000,000 output tokens. ``None`` means unknown."""

    provider_id: str | None = None
    """Optional provider id this model belongs to (``"anthropic"``,
    ``"openai"``, ``"bedrock"``, ...). Round 2A P-11 — used by
    ``opencomputer models add`` so the CLI can filter ``models list``
    by provider and so user-added overrides remember which provider
    they target. ``None`` for the curated G.32 defaults so existing
    callers keep working unchanged."""


# Curated defaults. Numbers reflect the documented context-length /
# pricing pages for each model family as of 2026-04. Plugins are
# encouraged to override entries via register_model() rather than
# editing this dict in place.
_DEFAULT_CATALOG: dict[str, ModelMetadata] = {
    # Anthropic — Claude family
    "claude-opus-4-7": ModelMetadata(
        model_id="claude-opus-4-7",
        context_length=200_000,
        input_usd_per_million=15.00,
        output_usd_per_million=75.00,
    ),
    "claude-sonnet-4-6": ModelMetadata(
        model_id="claude-sonnet-4-6",
        context_length=200_000,
        input_usd_per_million=3.00,
        output_usd_per_million=15.00,
    ),
    "claude-haiku-4-5-20251001": ModelMetadata(
        model_id="claude-haiku-4-5-20251001",
        context_length=200_000,
        input_usd_per_million=0.80,
        output_usd_per_million=4.00,
    ),
    # OpenAI — GPT-5 + reasoning
    "gpt-5.4": ModelMetadata(
        model_id="gpt-5.4",
        context_length=128_000,
        input_usd_per_million=10.00,
        output_usd_per_million=40.00,
    ),
    "gpt-4o": ModelMetadata(
        model_id="gpt-4o",
        context_length=128_000,
        input_usd_per_million=2.50,
        output_usd_per_million=10.00,
    ),
    "o1": ModelMetadata(
        model_id="o1",
        context_length=200_000,
        input_usd_per_million=15.00,
        output_usd_per_million=60.00,
    ),
    "o3": ModelMetadata(
        model_id="o3",
        context_length=200_000,
        input_usd_per_million=15.00,
        output_usd_per_million=60.00,
    ),
    "o4-mini": ModelMetadata(
        model_id="o4-mini",
        context_length=200_000,
        input_usd_per_million=1.10,
        output_usd_per_million=4.40,
    ),
}


_lock = threading.Lock()
_registry: dict[str, ModelMetadata] = dict(_DEFAULT_CATALOG)


def get_metadata(model_id: str) -> ModelMetadata | None:
    """Return the metadata entry for ``model_id``, or ``None`` if absent.

    Lookup is exact-match on ``model_id``. Callers that want
    prefix-based lookup (so ``claude-opus-4-7-20250101`` matches
    ``claude-opus-4-7``) should walk the prefixes themselves —
    different callers want different prefix-stripping rules and we
    don't want to bake one in here.
    """
    with _lock:
        return _registry.get(model_id)


def context_length(model_id: str) -> int | None:
    """Convenience: return the context length for ``model_id``, or ``None``."""
    meta = get_metadata(model_id)
    return meta.context_length if meta else None


def cost_per_million(model_id: str) -> tuple[float, float] | None:
    """Convenience: return ``(input_cost, output_cost)`` per 1M tokens, or ``None``.

    Returns ``None`` only when EITHER the entry is missing OR neither
    cost field is populated. A partial entry (only input cost known)
    surfaces as ``(input, 0.0)`` so callers don't crash on the unpack.
    """
    meta = get_metadata(model_id)
    if meta is None:
        return None
    if meta.input_usd_per_million is None and meta.output_usd_per_million is None:
        return None
    return (
        meta.input_usd_per_million or 0.0,
        meta.output_usd_per_million or 0.0,
    )


def register_model(meta: ModelMetadata, *, replace: bool = False) -> None:
    """Add (or replace) a metadata entry.

    Plugins call this from ``register(api)`` to teach core about their
    models. ``replace=False`` (default) silently keeps the existing
    entry on collision so the curated defaults stay authoritative
    unless a plugin explicitly opts in to overriding them.
    """
    with _lock:
        existing = _registry.get(meta.model_id)
        if existing is not None and not replace:
            logger.debug(
                "model_metadata.register_model: %r already present; "
                "skipping (pass replace=True to override)",
                meta.model_id,
            )
            return
        _registry[meta.model_id] = meta


def list_models() -> list[ModelMetadata]:
    """Return all registered metadata entries, sorted by model_id."""
    with _lock:
        return sorted(_registry.values(), key=lambda m: m.model_id)


def reset_to_defaults() -> None:
    """Reset the registry to the curated catalog. Test-only helper.

    Production code never calls this — it would discard third-party
    plugin contributions.
    """
    with _lock:
        _registry.clear()
        _registry.update(_DEFAULT_CATALOG)


# ---------------------------------------------------------------------------
# Round 2A P-11 — user-curated registration + file-backed overrides
# ---------------------------------------------------------------------------


# Statuses returned by :func:`register_user_model` so the CLI can render the
# right one-line summary without re-implementing the merge logic itself.
ADD_STATUS_ADDED = "added"
ADD_STATUS_UPDATED = "updated"
ADD_STATUS_NOOP = "noop"


def register_user_model(
    *,
    provider_id: str,
    model_id: str,
    alias: str | None = None,
    context_length: int | None = None,
    input_usd_per_million: float | None = None,
    output_usd_per_million: float | None = None,
) -> tuple[str, ModelMetadata]:
    """Add or update a (provider, model) entry from user input.

    Round 2A P-11 — backs ``opencomputer models add``. Semantics:

    * **Add new**: if no entry exists for ``model_id``, create one and
      return ``(ADD_STATUS_ADDED, meta)``.
    * **Update existing**: if any of ``context_length`` /
      ``input_usd_per_million`` / ``output_usd_per_million`` /
      ``provider_id`` differ from the stored entry, replace the entry
      and return ``(ADD_STATUS_UPDATED, meta)``.
    * **No-op**: if the entry exists and the user passed no flags
      that would change it, leave it alone and return
      ``(ADD_STATUS_NOOP, existing_meta)``.

    Non-destructive: never removes an entry; only ADDs or MUTATES.
    The CLI persists the result to ``model_overrides.yaml`` separately
    so subsequent process starts re-apply the same delta.

    ``alias`` registers an additional pointer to the same metadata
    (e.g. ``--alias claude-fast`` for ``claude-haiku-4-5-20251001``)
    so downstream callers can resolve either id to the same entry.
    """
    with _lock:
        existing = _registry.get(model_id)
        # Build the new entry: start from existing (if any) and overlay
        # the caller-supplied non-None values.
        base = existing if existing is not None else ModelMetadata(model_id=model_id)
        kwargs = {
            "context_length": context_length if context_length is not None
            else base.context_length,
            "input_usd_per_million": input_usd_per_million
            if input_usd_per_million is not None
            else base.input_usd_per_million,
            "output_usd_per_million": output_usd_per_million
            if output_usd_per_million is not None
            else base.output_usd_per_million,
            "provider_id": provider_id,
        }
        new_meta = replace(base, **kwargs)

        if existing is not None and new_meta == existing:
            status = ADD_STATUS_NOOP
        elif existing is None:
            status = ADD_STATUS_ADDED
        else:
            status = ADD_STATUS_UPDATED

        _registry[model_id] = new_meta

        # ``alias`` adds a second key that resolves to the same meta.
        # Store as a separate (alias-keyed) entry so ``get_metadata(alias)``
        # works without callers needing to know about a separate map.
        if alias and alias != model_id:
            alias_meta = replace(new_meta, model_id=alias)
            _registry[alias] = alias_meta

        return status, new_meta


def _overrides_file_default() -> Path:
    """Default path for the user-overrides YAML file.

    Lazy import of :func:`opencomputer.agent.config._home` so this
    module stays importable without the wider config surface (the
    config module imports yaml + os; we already have those).
    """
    from opencomputer.agent.config import _home

    return _home() / "model_overrides.yaml"


def _serialize_entry(meta: ModelMetadata, *, alias: str | None) -> dict[str, Any]:
    """Convert a ModelMetadata + optional alias to a YAML-friendly dict.

    Drops ``None`` fields so the on-disk file stays minimal.
    """
    out: dict[str, Any] = {"model_id": meta.model_id}
    if meta.provider_id is not None:
        out["provider_id"] = meta.provider_id
    if meta.context_length is not None:
        out["context_length"] = meta.context_length
    if meta.input_usd_per_million is not None:
        out["input_usd_per_million"] = meta.input_usd_per_million
    if meta.output_usd_per_million is not None:
        out["output_usd_per_million"] = meta.output_usd_per_million
    if alias:
        out["alias"] = alias
    return out


def _load_overrides_yaml(path: Path) -> list[dict[str, Any]]:
    """Read and parse the overrides YAML. Returns ``[]`` on missing/invalid.

    Fail-safe per plan: a corrupt file is logged at ERROR and treated
    as empty so a malformed write can't wedge CLI startup.
    """
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as e:
        logger.error("model_overrides.yaml: failed to parse %s: %s", path, e)
        return []
    if isinstance(raw, list):
        # Support a top-level list shape too — tolerant input parser.
        entries = raw
    elif isinstance(raw, dict):
        entries = raw.get("models", [])
        if not isinstance(entries, list):
            logger.error(
                "model_overrides.yaml: 'models' must be a list, got %s; ignoring",
                type(entries).__name__,
            )
            return []
    else:
        logger.error(
            "model_overrides.yaml: top-level must be a mapping or list, got %s",
            type(raw).__name__,
        )
        return []
    valid: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("model_id"):
            logger.error(
                "model_overrides.yaml: skipping malformed entry (missing model_id): %r",
                entry,
            )
            continue
        valid.append(entry)
    return valid


def _atomic_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically with mode 0600.

    Uses tempfile in the same directory + ``os.replace`` so a partial
    write never leaves a half-written ``model_overrides.yaml`` behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".model_overrides.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, default_flow_style=False, sort_keys=False)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup on failure; never mask the original error.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def upsert_override_file(
    *,
    provider_id: str,
    model_id: str,
    alias: str | None = None,
    context_length: int | None = None,
    input_usd_per_million: float | None = None,
    output_usd_per_million: float | None = None,
    path: Path | None = None,
) -> Path:
    """Persist a single (provider, model) entry into the overrides YAML.

    Idempotent + non-destructive at the file level: existing entries
    for OTHER (provider, model) pairs are preserved; the entry for
    ``model_id`` is replaced (or appended if absent).

    Returns the path written. Atomic write — partial failures don't
    corrupt the file.
    """
    target = path or _overrides_file_default()
    existing = _load_overrides_yaml(target)
    new_entry: dict[str, Any] = {"provider_id": provider_id, "model_id": model_id}
    if context_length is not None:
        new_entry["context_length"] = context_length
    if input_usd_per_million is not None:
        new_entry["input_usd_per_million"] = input_usd_per_million
    if output_usd_per_million is not None:
        new_entry["output_usd_per_million"] = output_usd_per_million
    if alias:
        new_entry["alias"] = alias

    merged: list[dict[str, Any]] = []
    replaced = False
    for entry in existing:
        if entry.get("model_id") == model_id and entry.get("provider_id") == provider_id:
            # Merge: caller-supplied non-None values overwrite stored ones,
            # but we keep stored fields the caller didn't touch.
            combined = dict(entry)
            combined.update(new_entry)
            merged.append(combined)
            replaced = True
        else:
            merged.append(entry)
    if not replaced:
        merged.append(new_entry)

    _atomic_write_yaml(target, {"models": merged})
    return target


def apply_overrides_file(path: Path | None = None) -> int:
    """Read ``model_overrides.yaml`` and merge each entry into the registry.

    Called once at CLI startup (after plugins have registered their own
    catalogs). Returns the count of entries applied. Plugin-shipped
    entries are overwritten because the user explicitly asked for the
    override; the curated G.32 defaults are also overridable for the
    same reason. Missing or empty file → 0, no-op.
    """
    target = path or _overrides_file_default()
    entries = _load_overrides_yaml(target)
    applied = 0
    for entry in entries:
        try:
            register_user_model(
                provider_id=entry.get("provider_id") or "",
                model_id=entry["model_id"],
                alias=entry.get("alias"),
                context_length=entry.get("context_length"),
                input_usd_per_million=entry.get("input_usd_per_million"),
                output_usd_per_million=entry.get("output_usd_per_million"),
            )
            applied += 1
        except Exception as e:  # noqa: BLE001 — never break startup over a bad row
            logger.error(
                "model_overrides.yaml: failed to apply entry %r: %s", entry, e
            )
    return applied


__all__ = [
    "ADD_STATUS_ADDED",
    "ADD_STATUS_NOOP",
    "ADD_STATUS_UPDATED",
    "ModelMetadata",
    "apply_overrides_file",
    "context_length",
    "cost_per_million",
    "get_metadata",
    "list_models",
    "register_model",
    "register_user_model",
    "reset_to_defaults",
    "upsert_override_file",
]
