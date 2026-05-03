"""
Anthropic provider — wraps the Anthropic SDK behind BaseProvider.

This is the first concrete provider. Later it can be moved to an
extension package (extensions/anthropic-provider/) for dogfooding the
plugin system, but for Phase 1 it lives in-tree so we can ship quickly.
"""

from __future__ import annotations

import base64
import importlib.util
import logging
import mimetypes
import os
import sys
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from anthropic import AsyncAnthropic
from anthropic import RateLimitError as AnthropicRateLimitError
from anthropic.types import Message as AnthropicMessage
from pydantic import BaseModel, Field

from opencomputer.agent.credential_pool import CredentialPool
from opencomputer.agent.model_capabilities import (
    supports_adaptive_thinking,
    supports_temperature,
)
from opencomputer.agent.prompt_caching import apply_full_cache_control
from opencomputer.agent.rate_guard import (
    format_remaining,
    rate_limit_remaining,
    record_rate_limit,
)
from opencomputer.inference.observability import LLMCallEvent, record_llm_call
from opencomputer.inference.pricing import compute_cost_usd
from opencomputer.tools.schema_sanitizer import (
    normalize_tool_input_schema_for_anthropic,
)
from plugin_sdk.core import Message, ToolCall
from plugin_sdk.pdf_helpers import (
    PDF_HARD_PAGE_LIMIT,
    PDF_MAX_BYTES,
    PDF_SOFT_PAGE_LIMIT,
    count_pdf_pages,
    pdf_to_base64,
)
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    RateLimitedError,
    StreamEvent,
    Usage,
)
from plugin_sdk.tool_contract import ToolSchema

_RATE_GUARD_PROVIDER = "anthropic"


#: Anthropic's tool-validator caps the number of tools that may carry
#: ``strict: true`` at 20. OC ships well over 20 tools that opt in via
#: ``strict_mode = True`` (Sessions/Read/Write/Bash/etc.), so we drop
#: the strict flag at the wire boundary — the schema sanitizer's
#: ``additionalProperties: false`` + pruned-required logic enforces the
#: same shape constraints at JSON-Schema level. Sending strict-true on
#: >20 tools yields ``BadRequestError: Too many strict tools (33).
#: The maximum number of strict tools supported is 20``.
_DROP_STRICT_FLAG_FROM_ANTHROPIC_TOOLS: bool = True


def _format_tools_for_anthropic(
    tools: list[ToolSchema] | None,
) -> list[dict[str, Any]]:
    """Convert OC ToolSchemas to Anthropic input_schema format with sanitization.

    Mirrors Hermes's ``convert_tools_to_anthropic`` + ``_normalize_tool_input_schema``
    pipeline (``agent/anthropic_adapter.py``). Strips:
      - Nullable anyOf/oneOf unions (Anthropic rejects null branches)
      - Numeric constraints on integer/number (Anthropic 2025 validator
        rejects minimum/maximum/exclusiveMin/exclusiveMax/multipleOf)
      - Array/object cardinality constraints
      - The ``strict: true`` flag — Anthropic caps strict tools at 20
        and OC ships >20 strict-marked tools (see comment above).

    Tools with no parameters get the canonical ``{"type":"object","properties":{}}``
    shape so Anthropic's strict validator accepts them.
    """
    if not tools:
        return []
    out: list[dict[str, Any]] = []
    for t in tools:
        formatted = t.to_anthropic_format()
        formatted["input_schema"] = normalize_tool_input_schema_for_anthropic(
            formatted.get("input_schema")
        )
        if _DROP_STRICT_FLAG_FROM_ANTHROPIC_TOOLS:
            formatted.pop("strict", None)
        out.append(formatted)
    return out


def _check_rate_limit() -> None:
    """TS-T7 — short-circuit if a previous 429 hasn't reset yet."""
    remaining = rate_limit_remaining(_RATE_GUARD_PROVIDER)
    if remaining is not None:
        raise RateLimitedError(
            _RATE_GUARD_PROVIDER,
            f"Anthropic rate-limited; wait {format_remaining(remaining)}",
        )


def _record_429(exc: AnthropicRateLimitError) -> None:
    """TS-T7 — persist the 429 so concurrent sessions back off too."""
    headers: dict[str, str] | None = None
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            headers = dict(response.headers)
        except Exception:
            headers = None
    record_rate_limit(_RATE_GUARD_PROVIDER, headers=headers)


_log = logging.getLogger("opencomputer.providers.anthropic")


# ─── SP4 — Anthropic Skills-via-API (opt-in) ──────────────────
# See docs/superpowers/specs/2026-05-02-sp4-skills-via-api-design.md
ANTHROPIC_SKILLS_BETA_HEADERS = (
    "code-execution-2025-08-25",
    "skills-2025-10-02",
    "files-api-2025-04-14",
)

CODE_EXECUTION_TOOL = {
    "type": "code_execution_20250825",
    "name": "code_execution",
}


def _resolve_anthropic_skills(runtime) -> list[str]:
    """Get the list of Anthropic skill IDs to enable for this call.

    Resolution order:
    1. ``runtime.custom["anthropic_skills"]`` (explicit programmatic)
    2. ``OPENCOMPUTER_ANTHROPIC_SKILLS`` env var (comma-separated)
    3. ``[]`` (no skills)

    Bad input (non-list, non-strings) is logged and ignored. ``runtime``
    may be ``None`` — in that case we still consult the env var.
    """
    if runtime is not None:
        explicit = (getattr(runtime, "custom", {}) or {}).get("anthropic_skills")
        if explicit is not None:
            if isinstance(explicit, list) and all(isinstance(s, str) for s in explicit):
                return [s for s in explicit if s.strip()]
            _log.warning(
                "anthropic_skills runtime flag has bad type %r; expected list[str]",
                type(explicit).__name__,
            )
            return []
    env = os.environ.get("OPENCOMPUTER_ANTHROPIC_SKILLS", "").strip()
    if env:
        return [s.strip() for s in env.split(",") if s.strip()]
    return []


def _build_skills_container(skill_ids: list[str]) -> dict:
    """Build the ``container.skills`` array per Anthropic Skills-via-API spec."""
    return {
        "skills": [
            {"type": "anthropic", "skill_id": sid, "version": "latest"}
            for sid in skill_ids
        ]
    }


def _augment_kwargs_for_skills(
    *,
    kwargs: dict,
    skill_ids: list[str],
) -> dict:
    """Mutate kwargs to enable Anthropic Skills-via-API.

    - Adds the three required beta headers (preserving any existing
      comma-separated ``anthropic-beta`` values).
    - Adds ``container.skills``.
    - Appends the ``code_execution_20250825`` tool (skips if already
      present, so it is safe to call twice).

    Returns the same dict (mutated in place) for convenience. No-op when
    ``skill_ids`` is empty — the kwargs are returned unchanged so this is
    safe to call unconditionally on every request.
    """
    if not skill_ids:
        return kwargs

    # Beta headers — preserve any existing comma-separated betas
    extra = dict(kwargs.get("extra_headers") or {})
    existing_betas = [
        b.strip() for b in extra.get("anthropic-beta", "").split(",") if b.strip()
    ]
    for beta in ANTHROPIC_SKILLS_BETA_HEADERS:
        if beta not in existing_betas:
            existing_betas.append(beta)
    if existing_betas:
        extra["anthropic-beta"] = ",".join(existing_betas)
    kwargs["extra_headers"] = extra

    # container.skills
    kwargs["container"] = _build_skills_container(skill_ids)

    # code_execution tool (required for skills to actually run)
    tools = list(kwargs.get("tools") or [])
    if not any(t.get("type") == "code_execution_20250825" for t in tools):
        tools.append(CODE_EXECUTION_TOOL)
    kwargs["tools"] = tools

    return kwargs


# ── SP2+SP3 integration: sibling-module lazy loaders ──
# ``files_cache.py`` and ``files_client.py`` live next to this provider
# in extensions/anthropic-provider/. The extension dir isn't on
# ``sys.path`` so ``from .files_cache import …`` would only work when the
# provider is imported as a package. Tests load this module via
# ``spec_from_file_location`` (no parent package), so we mirror the
# ``opencomputer/cli_files.py`` pattern: load each sibling under a stable
# synthetic module name and lazy-cache the result.
_EXT_DIR = Path(__file__).parent
_FILES_CACHE_PATH = _EXT_DIR / "files_cache.py"
_FILES_CLIENT_PATH = _EXT_DIR / "files_client.py"
_FILES_CACHE_SYNTHETIC = "extensions_anthropic_provider_files_cache"
_FILES_CLIENT_SYNTHETIC = "extensions_anthropic_provider_files_client"


def _load_sibling(synthetic_name: str, path: Path):
    """Load a sibling extension module via spec_from_file_location.

    Cached in ``sys.modules`` under ``synthetic_name`` so repeated loads
    return the same module object (matches ``opencomputer/cli_files.py``).
    """
    existing = sys.modules.get(synthetic_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(synthetic_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[synthetic_name] = module
    spec.loader.exec_module(module)
    return module


def _files_cache_module():
    return _load_sibling(_FILES_CACHE_SYNTHETIC, _FILES_CACHE_PATH)


def _files_client_module():
    return _load_sibling(_FILES_CLIENT_SYNTHETIC, _FILES_CLIENT_PATH)


_SUPPORTED_IMAGE_MEDIA_TYPES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)
_IMAGE_MAX_BYTES = 5 * 1024 * 1024  # Anthropic's per-image cap


def _build_anthropic_image_block(
    path: Path, media_type: str
) -> dict[str, Any] | None:
    """Build an Anthropic ``image`` content block from a local image path.

    Returns ``None`` and logs a WARNING if the file is unreadable or
    exceeds Anthropic's 5 MB per-image cap. Never raises — a bad
    attachment must not kill the turn.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        _log.warning("image attachment unreadable: %s (%s)", path, exc)
        return None
    if len(data) > _IMAGE_MAX_BYTES:
        _log.warning(
            "image attachment over 5 MB cap; skipping: %s (%d bytes)",
            path,
            len(data),
        )
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def _build_pdf_base64_block_sync(path: Path) -> dict[str, Any] | None:
    """SP2 base64-inline PDF block builder — synchronous.

    Honors the SP2 guard rails (``plugin_sdk.pdf_helpers``):

    - 32 MB request-size cap → reject + warn.
    - 600-page hard cap → reject + warn.
    - 100-page soft cap → warn but still emit (200k-context-model edge).

    Returns ``None`` (and logs WARNING) on read errors or when a guard
    fires; never raises.

    This is the SP2 implementation factored out as a synchronous helper
    so that both the sync dispatcher (``_content_blocks_with_attachments``,
    used everywhere SP2's behavior must be preserved) and the new async
    builder (``_build_anthropic_pdf_block``, which adds the SP3 cache
    path) share a single source of truth for the base64 fallback.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        _log.warning("PDF attachment unreadable: %s (%s)", path, exc)
        return None
    if len(data) > PDF_MAX_BYTES:
        _log.warning(
            "PDF attachment over 32 MB cap; skipping: %s (%d bytes)",
            path,
            len(data),
        )
        return None
    page_count = count_pdf_pages(data)
    if page_count > PDF_HARD_PAGE_LIMIT:
        _log.warning(
            "PDF over 600-page hard limit; skipping: %s (%d pages)",
            path,
            page_count,
        )
        return None
    if page_count > PDF_SOFT_PAGE_LIMIT:
        _log.warning(
            "PDF over 100 pages; may exceed 200k-context-model capacity: "
            "%s (%d pages)",
            path,
            page_count,
        )
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": pdf_to_base64(data),
        },
    }


async def _build_anthropic_pdf_block(
    path: Path,
    *,
    cache: Any | None = None,
    client: Any | None = None,
) -> dict[str, Any] | None:
    """Build an Anthropic ``document`` content block from a PDF path.

    Honors the SP2 guard rails (32 MB / 600-page / 100-page).

    SP2+SP3 integration: when both ``cache`` and ``client`` are provided,
    follows the Files API path:

    - SHA-256 the bytes → check cache → hit returns a ``file_id`` block
    - Cache miss → ``await client.upload(path)`` → cache + return file_id
    - Any failure → log WARNING, fall back to the base64 path so the
      user's request still succeeds (preserves SP2's behavior).

    When ``cache`` or ``client`` is None: identical to the SP2 base64
    path — no cache work, no upload, no extra logging.

    ``cache`` and ``client`` are typed ``Any | None`` rather than the
    concrete ``FilesCache | AnthropicFilesClient`` to keep this module's
    import surface flat (the sibling files load lazily; see
    ``_load_sibling``).
    """
    if cache is not None and client is not None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            # Surface the same diagnostic as the sync path; no point
            # uploading something we couldn't even read.
            _log.warning("PDF attachment unreadable: %s (%s)", path, exc)
            return None
        # Re-apply the same size + page-count guards before any upload —
        # otherwise we'd ship a >32 MB body to Files API only to fail.
        if len(data) > PDF_MAX_BYTES:
            _log.warning(
                "PDF attachment over 32 MB cap; skipping: %s (%d bytes)",
                path,
                len(data),
            )
            return None
        page_count = count_pdf_pages(data)
        if page_count > PDF_HARD_PAGE_LIMIT:
            _log.warning(
                "PDF over 600-page hard limit; skipping: %s (%d pages)",
                path,
                page_count,
            )
            return None
        if page_count > PDF_SOFT_PAGE_LIMIT:
            _log.warning(
                "PDF over 100 pages; may exceed 200k-context-model capacity: "
                "%s (%d pages)",
                path,
                page_count,
            )
        try:
            fc_mod = _files_cache_module()
            content_hash = fc_mod.hash_file_bytes(data)
            entry = cache.get(content_hash)
            if entry is not None:
                return {
                    "type": "document",
                    "source": {"type": "file", "file_id": entry.file_id},
                }
            metadata = await client.upload(path)
            cache.put(
                content_hash,
                file_id=metadata.id,
                filename=metadata.filename,
                size_bytes=metadata.size_bytes,
            )
            return {
                "type": "document",
                "source": {"type": "file", "file_id": metadata.id},
            }
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            _log.warning(
                "Files API caching failed for %s (%s); falling back to base64",
                path,
                exc,
            )
            # Fall through to base64 path. ``data`` was already read +
            # validated above, but rebuilding via the sync helper keeps
            # the fallback identical to SP2 (a single source of truth).

    return _build_pdf_base64_block_sync(path)


def _resolve_anthropic_files_cache_enabled(runtime) -> bool:
    """Return True iff Files API caching is opted in.

    Resolution order:
    1. ``runtime.custom["anthropic_files_cache"]`` (explicit programmatic — wins)
    2. ``OPENCOMPUTER_ANTHROPIC_FILES_CACHE`` env var (truthy values: 1/true/yes/on)
    3. False (default OFF)

    Mirrors ``_resolve_anthropic_skills`` shape (SP4) for consistency.
    """
    if runtime is not None:
        explicit = (getattr(runtime, "custom", {}) or {}).get(
            "anthropic_files_cache"
        )
        if explicit is not None:
            return bool(explicit)
    env = os.environ.get("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", "").strip().lower()
    return env in ("1", "true", "yes", "on")


def _content_blocks_with_attachments(
    *, text: str, attachment_paths: list[str]
) -> list[dict[str, Any]]:
    """Build Anthropic content array combining text + media attachments.

    Dispatches per-attachment based on MIME type:

    - ``application/pdf`` (or ``.pdf`` extension) → ``document`` block
      with base64 source. 32 MB / 600-page guard rails apply.
    - ``image/png|jpeg|gif|webp`` → ``image`` block (5 MB cap).
    - other → log WARNING, skip.

    Order: media blocks first, then text — matches what Claude Desktop
    sends and what humans expect ("here are the files, here's my
    question about them").

    Never raises — bad attachments are dropped with a WARNING log so a
    corrupt or oversized file doesn't kill the turn.

    Synchronous: SP2's behavior preserved verbatim for callers that
    don't opt into the SP3 Files API cache. The opt-in path uses
    :func:`_content_blocks_with_attachments_async` instead, which
    threads ``cache`` + ``client`` to the async PDF builder.
    """
    blocks: list[dict[str, Any]] = []
    for path_str in attachment_paths:
        path = Path(path_str)
        media_type, _ = mimetypes.guess_type(str(path))
        if media_type == "application/pdf" or path.suffix.lower() == ".pdf":
            block = _build_pdf_base64_block_sync(path)
        elif media_type in _SUPPORTED_IMAGE_MEDIA_TYPES:
            block = _build_anthropic_image_block(path, media_type)
        else:
            _log.warning(
                "attachment has unsupported media type %r; skipping: %s",
                media_type,
                path,
            )
            block = None
        if block is not None:
            blocks.append(block)
    if text:
        blocks.append({"type": "text", "text": text})
    if not blocks:
        # Edge case: every attachment was skipped AND text is empty. Send
        # a single empty text block so Anthropic's API doesn't reject the
        # request for empty content.
        blocks.append({"type": "text", "text": text or ""})
    return blocks


async def _content_blocks_with_attachments_async(
    *,
    text: str,
    attachment_paths: list[str],
    cache: Any | None = None,
    client: Any | None = None,
) -> list[dict[str, Any]]:
    """Async variant of :func:`_content_blocks_with_attachments`.

    Routes PDFs through :func:`_build_anthropic_pdf_block` (async) so
    that an opted-in cache + Files API client can short-circuit to a
    ``file_id`` block on hit, or upload + cache on miss. Image
    attachments still use the sync builder (no I/O upgrade needed —
    images are <5 MB and inlined as base64).

    Same ordering and edge-case behavior as the sync dispatcher.
    """
    blocks: list[dict[str, Any]] = []
    for path_str in attachment_paths:
        path = Path(path_str)
        media_type, _ = mimetypes.guess_type(str(path))
        if media_type == "application/pdf" or path.suffix.lower() == ".pdf":
            block = await _build_anthropic_pdf_block(
                path, cache=cache, client=client
            )
        elif media_type in _SUPPORTED_IMAGE_MEDIA_TYPES:
            block = _build_anthropic_image_block(path, media_type)
        else:
            _log.warning(
                "attachment has unsupported media type %r; skipping: %s",
                media_type,
                path,
            )
            block = None
        if block is not None:
            blocks.append(block)
    if text:
        blocks.append({"type": "text", "text": text})
    if not blocks:
        blocks.append({"type": "text", "text": text or ""})
    return blocks


def _build_anthropic_multimodal_content(
    *, text: str, image_paths: list[str]
) -> list[dict[str, Any]]:
    """Back-compat alias for :func:`_content_blocks_with_attachments`.

    Pre-SP2 callers passed image paths via ``image_paths``; SP2 generalized
    the helper to also handle PDFs. Existing callers (and the
    ``test_cli_ui_image_paste.py`` regression suite) keep working via this
    thin wrapper. New code should use ``_content_blocks_with_attachments``
    directly.
    """
    return _content_blocks_with_attachments(
        text=text, attachment_paths=image_paths
    )


class AnthropicProviderConfig(BaseModel):
    """Pydantic schema for AnthropicProvider construction kwargs.

    Wired onto ``AnthropicProvider.config_schema`` (Task I.6). The
    plugin registry uses this to validate ``provider.config`` at
    ``register_provider`` time and raise ``ValueError`` on shape
    mismatch — catching bad config at plugin load instead of at first
    request.

    Fields mirror the ``__init__`` signature: all three are optional
    because construction also reads from env vars
    (``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``,
    ``ANTHROPIC_AUTH_MODE``).

    ``auth_mode`` accepts the legacy ``"x-api-key"`` spelling AND the
    newer ``"api_key"`` spelling. The construction logic coerces both
    to the same effective behavior (``"x-api-key"`` header mode).
    """

    api_key: str | None = Field(default=None)
    base_url: str | None = Field(default=None)
    auth_mode: Literal["api_key", "x-api-key", "bearer"] = Field(default="api_key")


async def _strip_x_api_key(request: httpx.Request) -> None:
    """httpx event hook: remove x-api-key header before sending.

    Used when talking to proxies that authenticate via Bearer tokens and
    forward x-api-key unchanged to the upstream Anthropic API (which then
    rejects it as invalid). Must run at the last moment so the Anthropic
    SDK's own auth path is undisturbed.
    """
    request.headers.pop("x-api-key", None)


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    default_model = "claude-opus-4-7"
    #: Task I.6 — schema used by the plugin registry to validate
    #: ``self.config`` at ``register_provider`` time.
    config_schema = AnthropicProviderConfig

    _api_key_env: str = "ANTHROPIC_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        auth_mode: str | None = None,
    ) -> None:
        """
        Args:
            api_key: API key / proxy key. Defaults to $ANTHROPIC_API_KEY.
                     Comma-separated value triggers pool mode (PR-A).
            base_url: Override the API endpoint (for proxies like Claude Router).
                      Defaults to $ANTHROPIC_BASE_URL, or None (direct Anthropic).
            auth_mode: "x-api-key" (Anthropic native) or "bearer"
                      (Authorization: Bearer header — for proxies that require it).
                      Defaults to $ANTHROPIC_AUTH_MODE, or "x-api-key".
        """
        # Optional credential pool (PR-A): comma-separated env value triggers pool mode.
        # Single key (no comma) → no pool, behavior IDENTICAL to today (regression-tested).
        api_key_raw = api_key or os.environ.get(self._api_key_env, "")
        if "," in api_key_raw:
            keys = [k.strip() for k in api_key_raw.split(",") if k.strip()]
            self._credential_pool: CredentialPool | None = CredentialPool(keys=keys) if len(keys) > 1 else None
            self._api_key = keys[0] if keys else api_key_raw
        else:
            self._credential_pool = None
            self._api_key = api_key_raw.strip()

        key = self._api_key
        if not key:
            raise RuntimeError(
                "Anthropic API key not set. Export ANTHROPIC_API_KEY or pass api_key."
            )
        base = base_url or os.environ.get("ANTHROPIC_BASE_URL") or None
        mode = (auth_mode or os.environ.get("ANTHROPIC_AUTH_MODE") or "x-api-key").lower()

        # Pre-validate mode with a clear RuntimeError before the pydantic
        # schema turns it into a less-helpful ValidationError. Keeps the
        # existing error message contract that callers rely on.
        if mode not in ("x-api-key", "api_key", "bearer"):
            raise RuntimeError(
                f"Unknown ANTHROPIC_AUTH_MODE: {mode!r} "
                f"(expected 'x-api-key', 'api_key', or 'bearer')"
            )

        # Task I.6: store a validated config snapshot so the plugin
        # registry can re-check it against ``config_schema`` at
        # ``register_provider`` time. The schema is permissive about
        # auth_mode spelling (accepts "x-api-key" and "api_key"), so
        # pass the effective value through.
        self.config = AnthropicProviderConfig(
            api_key=key,
            base_url=base,
            auth_mode=mode,  # type: ignore[arg-type]
        )

        self._base = base
        self._mode = mode
        # Single source of truth for client construction —
        # ``opencomputer.agent.anthropic_client`` handles bearer mode +
        # base_url + x-api-key strip identically for every Anthropic
        # call site (chat, batch, vision, slash commands).
        from opencomputer.agent.anthropic_client import (
            build_anthropic_async_client,
        )
        self.client = build_anthropic_async_client(
            key, base_url=base, auth_mode=mode,
        )
        # Idle-aware TTL switch — track wall-clock between calls so we can
        # bump cache TTL to 1h when a session has been idle long enough
        # that the 5m cache would otherwise have expired.
        self._last_call_ts: float = 0.0

    # ─── capabilities ───────────────────────────────────────────────

    @property
    def capabilities(self):  # type: ignore[override]
        from plugin_sdk import CacheTokens, ProviderCapabilities

        def _extract(usage: Any) -> CacheTokens:
            return CacheTokens(
                read=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                write=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            )

        def _min_tokens(model: str) -> int:
            m = model.lower()
            # Opus + Mythos + Haiku 4.5 share the 4096 minimum per the
            # Anthropic prompt-caching spec.
            if (
                "opus" in m
                or "mythos" in m
                or "haiku-4-5" in m
                or "haiku-4.5" in m
            ):
                return 4096
            if "sonnet-4-6" in m or "sonnet-4.6" in m:
                return 2048
            return 1024

        return ProviderCapabilities(
            requires_reasoning_resend_in_tool_cycle=True,
            reasoning_block_kind="anthropic_thinking",
            extracts_cache_tokens=_extract,
            min_cache_tokens=_min_tokens,
            supports_long_ttl=True,
            # Static fallback for the BaseProvider.supports_native_thinking_for
            # default impl. Overridden below with per-model logic that uses
            # supports_adaptive_thinking() so legacy models (Opus 3.5 etc.)
            # correctly route through the prompt-based fallback.
            supports_native_thinking=True,
        )

    def supports_native_thinking_for(self, model: str) -> bool:
        """Per-model native-thinking decision for Anthropic.

        Modern models (Sonnet 4+, Opus 4+, Haiku 4.5+) emit
        ``thinking_delta`` events natively when extended thinking is
        enabled. Legacy models (Opus 3.5, Sonnet 3.5) do not — for
        those, the agent loop's ``ThinkingTagsParser`` fallback
        activates so users still see a reasoning panel.
        """
        return supports_adaptive_thinking(model)

    # ─── message conversion ─────────────────────────────────────────

    def _to_anthropic_messages(self, messages: list[Message]) -> list[dict[str, Any]]:

        """Convert our canonical Message list to Anthropic's message format."""
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                # system messages are passed separately, not in messages[]
                continue
            if m.role == "assistant" and m.tool_calls:
                content: list[dict[str, Any]] = []
                # If the message carries verbatim reasoning blocks (Anthropic
                # extended thinking with signatures), they MUST be emitted
                # before the tool_use block. The API verifies signatures
                # during the tool-use cycle; missing or out-of-order
                # thinking blocks break reasoning continuity.
                replay = m.reasoning_replay_blocks
                if replay:
                    for blk in replay:
                        # Defensive: only forward thinking blocks we know
                        # how to send. Other shapes (future provider
                        # extensions) are skipped here, not dropped from
                        # the canonical Message.
                        if isinstance(blk, dict) and blk.get("type") == "thinking":
                            content.append(
                                {
                                    "type": "thinking",
                                    "thinking": blk.get("thinking", ""),
                                    "signature": blk.get("signature", ""),
                                }
                            )
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                out.append({"role": "assistant", "content": content})
            elif m.role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
            else:
                # User / assistant text message. If the message carries
                # image attachments, build a multimodal content array
                # with text + base64-encoded images so the model can
                # actually see them.
                if m.attachments:
                    out.append(
                        {
                            "role": m.role,
                            "content": _content_blocks_with_attachments(
                                text=m.content,
                                attachment_paths=m.attachments,
                            ),
                        }
                    )
                else:
                    out.append({"role": m.role, "content": m.content})
        return out

    async def _to_anthropic_messages_async(
        self,
        messages: list[Message],
        *,
        cache: Any | None = None,
        client: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Async sibling of :meth:`_to_anthropic_messages` for the cache path.

        Identical shape and behavior except attachment-bearing messages
        are routed through :func:`_content_blocks_with_attachments_async`,
        which threads ``cache`` + ``client`` to the async PDF builder
        (Files API hit/miss/fallback) when both are provided.

        Used by ``complete()`` / ``stream_complete()`` only when
        :func:`_resolve_anthropic_files_cache_enabled` returns True;
        otherwise the synchronous path is preserved (so the existing
        sync test surface — ``test_anthropic_thinking_resend.py`` etc. —
        keeps working unchanged).
        """
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "assistant" and m.tool_calls:
                content: list[dict[str, Any]] = []
                replay = m.reasoning_replay_blocks
                if replay:
                    for blk in replay:
                        if isinstance(blk, dict) and blk.get("type") == "thinking":
                            content.append(
                                {
                                    "type": "thinking",
                                    "thinking": blk.get("thinking", ""),
                                    "signature": blk.get("signature", ""),
                                }
                            )
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                out.append({"role": "assistant", "content": content})
            elif m.role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
            else:
                if m.attachments:
                    out.append(
                        {
                            "role": m.role,
                            "content": await _content_blocks_with_attachments_async(
                                text=m.content,
                                attachment_paths=m.attachments,
                                cache=cache,
                                client=client,
                            ),
                        }
                    )
                else:
                    out.append({"role": m.role, "content": m.content})
        return out

    def _apply_cache_control(
        self,
        anthropic_messages: list[dict[str, Any]],
        system: str,
        api_tools: list[dict[str, Any]] | None = None,
        *,
        model: str = "",
        idle_seconds: float = 0.0,
    ) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
        """Apply Anthropic prompt caching across system + messages + tools.

        Prepends ``system`` to ``anthropic_messages`` as a synthetic system
        message and routes through ``apply_full_cache_control`` (Item 1,
        2026-05-02), which allocates 4 ephemeral cache_control breakpoints
        as ``tools[-1] + system + last 2 non-system messages`` when tools
        are present, or ``system + last 3 non-system messages`` when not.

        Then extracts system back out as a list of content blocks so it can
        be passed to the SDK's ``system=`` parameter with cache_control
        preserved.

        ``model`` and ``idle_seconds`` drive the size-threshold filter and
        the long-TTL switch via the provider's capabilities. Both default
        safely so callers that haven't been updated still produce today's
        cache layout.

        Returns:
            (system_for_sdk, messages_for_sdk, tools_for_sdk) — system is a
            list of content blocks when there is a system prompt, or the
            original string otherwise; tools is the (possibly empty) list of
            tool dicts with cache_control on the last entry when non-empty.
        """
        from opencomputer.agent.prompt_caching import select_cache_ttl

        unified: list[dict[str, Any]] = []
        if system:
            unified.append({"role": "system", "content": system})
        unified.extend(anthropic_messages)

        caps = self.capabilities
        ttl = select_cache_ttl(
            supports_long_ttl=caps.supports_long_ttl,
            idle_seconds=idle_seconds,
        )
        threshold = caps.min_cache_tokens(model) if model else 0

        cached, cached_tools = apply_full_cache_control(
            unified,
            api_tools,
            cache_ttl=ttl,
            native_anthropic=True,
            min_cache_tokens=threshold,
        )

        if system and cached and cached[0].get("role") == "system":
            sys_content = cached[0].get("content")
            sys_for_sdk: Any = sys_content if isinstance(sys_content, list) else system
            messages_for_sdk = cached[1:]
        else:
            sys_for_sdk = system
            messages_for_sdk = cached

        return sys_for_sdk, messages_for_sdk, cached_tools

    def _parse_response(self, resp: AnthropicMessage) -> ProviderResponse:
        """Convert an Anthropic response back to our canonical Message + metadata."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        thinking_parts: list[str] = []
        replay_blocks: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                # Extended thinking surfaces as ``thinking`` blocks with a
                # ``.thinking`` field carrying the chain. Aggregate across
                # blocks and surface on ProviderResponse.reasoning so the
                # SDK has a provider-agnostic reasoning field populated.
                thinking_text = getattr(block, "thinking", None)
                signature = getattr(block, "signature", None)
                if thinking_text:
                    thinking_parts.append(str(thinking_text))
                # Preserve the verbatim block (with signature) so we can
                # replay it on the next turn during the tool-use cycle.
                # The Anthropic API rejects modified or missing signatures.
                # Skip blocks without a signature — they can't be replayed
                # safely.
                if thinking_text is not None and signature is not None:
                    replay_blocks.append(
                        {
                            "type": "thinking",
                            "thinking": str(thinking_text),
                            "signature": str(signature),
                        }
                    )
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input) if block.input else {},
                    )
                )
        replay = replay_blocks or None
        msg = Message(
            role="assistant",
            content="\n".join(text_parts),
            tool_calls=tool_calls if tool_calls else None,
            reasoning_replay_blocks=replay,
        )
        # Anthropic exposes prompt-cache token counts on usage when the
        # request hit its caching path. Surface them on canonical Usage so
        # cost reporting is provider-agnostic.
        usage = Usage(
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            cache_read_tokens=int(getattr(resp.usage, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(resp.usage, "cache_creation_input_tokens", 0) or 0),
        )
        reasoning = "\n".join(thinking_parts) if thinking_parts else None
        return ProviderResponse(
            message=msg,
            stop_reason=resp.stop_reason or "end_turn",
            usage=usage,
            reasoning=reasoning,
            reasoning_replay_blocks=replay,
        )

    # ─── completion ────────────────────────────────────────────────

    def _build_client_for_key(self, key: str) -> AsyncAnthropic:
        """Build an AsyncAnthropic client for the given key (used in pool rotation)."""
        from opencomputer.agent.anthropic_client import (
            build_anthropic_async_client,
        )
        return build_anthropic_async_client(
            key, base_url=self._base, auth_mode=self._mode,
        )

    def _build_files_cache_pair(
        self, runtime_extras: dict | None
    ) -> tuple[Any | None, Any | None]:
        """Construct (cache, client) iff the SP3 Files API cache is opted in.

        Synthesises a one-shot runtime view from ``runtime_extras`` (a
        flat dict on the provider call boundary) and routes it through
        :func:`_resolve_anthropic_files_cache_enabled`, which reads
        ``runtime.custom["anthropic_files_cache"]``. Returns
        ``(None, None)`` when caching is off — callers then take the
        existing sync attachment path (SP2 base64), preserving today's
        behavior verbatim for users who don't opt in.

        ``FilesCache`` and ``AnthropicFilesClient`` are loaded lazily
        from sibling extension files; any construction error degrades
        to ``(None, None)`` with a warning so the user's request never
        breaks because the cache scaffolding misbehaved.
        """
        try:
            from types import SimpleNamespace
            runtime_view = SimpleNamespace(custom=runtime_extras or {})
            if not _resolve_anthropic_files_cache_enabled(runtime_view):
                return None, None
            from opencomputer.agent.config import _home as _profile_home
            fc_mod = _files_cache_module()
            fcl_mod = _files_client_module()
            cache_path = _profile_home() / fc_mod.CACHE_FILENAME
            cache = fc_mod.FilesCache(cache_path)
            client = fcl_mod.AnthropicFilesClient(api_key=self._api_key)
            return cache, client
        except Exception as exc:  # noqa: BLE001 — fail-open
            _log.warning(
                "Failed to construct Files API cache/client (%s); "
                "falling back to base64 PDF path",
                exc,
            )
            return None, None

    async def _do_complete(
        self,
        key: str,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        runtime_extras: dict | None = None,
        response_schema: dict | None = None,
        site: str = "agent_loop",
    ) -> ProviderResponse:
        """Low-level complete using the given API key (pool-rotation target)."""
        # TS-T7 — short-circuit before the SDK so concurrent sessions
        # don't keep pinging while a 429 cools down.
        _check_rate_limit()

        client = self._build_client_for_key(key) if key != self._api_key else self.client
        # SP2+SP3 integration: opt-in PDF Files API cache. Returns
        # (None, None) when caching is off — preserves the SP2 sync
        # base64 path verbatim for everyone else.
        files_cache, files_api_client = self._build_files_cache_pair(runtime_extras)
        if files_cache is not None:
            anthropic_messages = await self._to_anthropic_messages_async(
                messages, cache=files_cache, client=files_api_client,
            )
        else:
            anthropic_messages = self._to_anthropic_messages(messages)
        # TS-T1 — apply Anthropic prompt caching (system_and_3 strategy).
        # Up to 4 cache_control breakpoints (system + last 3 non-system
        # messages) for ~75% input-token cost reduction on multi-turn
        # conversations.
        # Idle-aware TTL: if this provider's last call was > 4 minutes
        # ago, the 5m cache would have expired before we got back to it.
        # Bump to 1h on Anthropic; safe no-op for providers that don't
        # support it. Time tracked per-provider-instance in monotonic
        # seconds (see __init__). Read defensively because some test
        # paths instantiate via ``__new__`` and skip __init__.
        import time as _time
        _now = _time.monotonic()
        _last = getattr(self, "_last_call_ts", 0.0)
        idle_s = (_now - _last) if _last > 0 else 0.0
        self._last_call_ts = _now
        # Item 1 (2026-05-02): build tools list FIRST so cache_control
        # can be applied to tools[-1] together with the system+messages
        # breakpoints in a single call (no two-call coordination footgun).
        api_tools_pre = _format_tools_for_anthropic(tools)
        sys_for_sdk, api_messages, api_tools = self._apply_cache_control(
            anthropic_messages, system, api_tools_pre,
            model=model, idle_seconds=idle_s,
        )
        # Subsystem A — Effort-driven max_tokens floor lift: high-effort
        # calls on adaptive models need headroom for thinking + tool calls
        # (Doc 5: start at 64k).
        effective_max_tokens = max_tokens
        if (
            runtime_extras
            and runtime_extras.get("reasoning_effort") in ("high", "xhigh", "max")
            and supports_adaptive_thinking(model)
        ):
            effective_max_tokens = max(max_tokens, 64_000)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": effective_max_tokens,
            "messages": api_messages,
        }
        # Opus 4.7+ and Mythos reject temperature/top_p/top_k. Conditional
        # inclusion driven by model_capabilities.
        if supports_temperature(model):
            kwargs["temperature"] = temperature
        if sys_for_sdk:
            kwargs["system"] = sys_for_sdk
        if api_tools:
            kwargs["tools"] = api_tools
        # Tier 2.A — /reasoning + /fast slash commands → API kwargs.
        if runtime_extras:
            from opencomputer.agent.runtime_flags import (
                anthropic_kwargs_from_runtime,
            )
            kwargs.update(
                anthropic_kwargs_from_runtime(
                    model=model,
                    reasoning_effort=runtime_extras.get("reasoning_effort"),
                    service_tier=runtime_extras.get("service_tier"),
                )
            )
        # Subsystem C — structured outputs. Merge response_schema into
        # output_config (which may already hold `effort` from the
        # runtime_flags step above). Anthropic's output_config accepts
        # both `format` and `effort` simultaneously.
        if response_schema is not None:
            existing_output_config = kwargs.get("output_config", {})
            existing_output_config["format"] = {
                "type": "json_schema",
                "schema": response_schema["schema"],
            }
            kwargs["output_config"] = existing_output_config
        # SP4 — Anthropic Skills-via-API opt-in. The runtime knob lives
        # on ``runtime.custom["anthropic_skills"]``; the agent loop only
        # forwards a flat ``runtime_extras`` dict to providers, so we
        # synthesize a minimal runtime-shaped object here. The env var
        # path (``OPENCOMPUTER_ANTHROPIC_SKILLS``) works regardless and
        # is the primary opt-in surface for now.
        from types import SimpleNamespace as _SkillsRuntime
        kwargs = _augment_kwargs_for_skills(
            kwargs=kwargs,
            skill_ids=_resolve_anthropic_skills(_SkillsRuntime(custom=runtime_extras or {})),
        )
        t0 = time.monotonic()
        try:
            resp = await client.messages.create(**kwargs)
        except AnthropicRateLimitError as exc:
            # TS-T7 — record the 429 so other sessions back off, then
            # re-raise so the caller's retry/fallback logic still sees it.
            _record_429(exc)
            raise
        t1 = time.monotonic()
        result = self._parse_response(resp)
        self._emit_llm_event(model=model, usage=result.usage, t0=t0, t1=t1, site=site)
        return result

    def _emit_llm_event(
        self, *, model: str, usage: Usage, t0: float, t1: float, site: str = "agent_loop"
    ) -> None:
        """Emit one LLMCallEvent to the central observability sink.

        Best-effort: a sink failure (disk full, permission denied) must
        not crash the agent loop. Logs at WARNING and continues.

        ``site`` defaults to ``"agent_loop"`` — Phase 4 follow-up will
        thread the actual call site through ``BaseProvider.complete()``
        as a kwarg once the agent loop is no longer contended by
        parallel sessions.
        """
        try:
            record_llm_call(
                LLMCallEvent(
                    ts=datetime.now(UTC),
                    provider="anthropic",
                    model=model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_creation_tokens=usage.cache_write_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    latency_ms=int((t1 - t0) * 1000),
                    cost_usd=compute_cost_usd(
                        provider="anthropic",
                        model=model,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cache_creation_tokens=usage.cache_write_tokens,
                        cache_read_tokens=usage.cache_read_tokens,
                    ),
                    site=site,
                )
            )
        except Exception as exc:  # noqa: BLE001 — telemetry must not break the loop
            _log.warning("LLMCallEvent record failed: %s", exc)

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stream: bool = False,
        runtime_extras: dict | None = None,
        response_schema: dict | None = None,
        site: str = "agent_loop",
    ) -> ProviderResponse:
        if self._credential_pool is None:
            return await self._do_complete(
                self._api_key,
                model=model,
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                runtime_extras=runtime_extras,
                response_schema=response_schema,
                site=site,
            )

        def _is_auth_failure(exc: Exception) -> bool:
            return "401" in str(exc) or "authentication" in str(exc).lower()

        return await self._credential_pool.with_retry(
            lambda key: self._do_complete(
                key,
                model=model,
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                runtime_extras=runtime_extras,
                response_schema=response_schema,
                site=site,
            ),
            is_auth_failure=_is_auth_failure,
        )

    async def _do_stream_complete(
        self,
        key: str,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        runtime_extras: dict | None = None,
        response_schema: dict | None = None,
        site: str = "agent_loop",
    ) -> ProviderResponse:
        """Low-level stream_complete that aggregates into a ProviderResponse (pool target)."""
        # TS-T7 — same cross-session guard as the non-streaming path.
        _check_rate_limit()

        client = self._build_client_for_key(key) if key != self._api_key else self.client
        # SP2+SP3 integration: opt-in PDF Files API cache.
        files_cache, files_api_client = self._build_files_cache_pair(runtime_extras)
        if files_cache is not None:
            anthropic_messages = await self._to_anthropic_messages_async(
                messages, cache=files_cache, client=files_api_client,
            )
        else:
            anthropic_messages = self._to_anthropic_messages(messages)
        # TS-T1 — apply Anthropic prompt caching (system_and_3 strategy).
        # Idle-aware TTL: if this provider's last call was > 4 minutes
        # ago, the 5m cache would have expired before we got back to it.
        # Bump to 1h on Anthropic; safe no-op for providers that don't
        # support it. Time tracked per-provider-instance in monotonic
        # seconds (see __init__). Read defensively because some test
        # paths instantiate via ``__new__`` and skip __init__.
        import time as _time
        _now = _time.monotonic()
        _last = getattr(self, "_last_call_ts", 0.0)
        idle_s = (_now - _last) if _last > 0 else 0.0
        self._last_call_ts = _now
        # Item 1 (2026-05-02): build tools list FIRST so cache_control
        # can be applied to tools[-1] together with the system+messages
        # breakpoints in a single call (no two-call coordination footgun).
        api_tools_pre = _format_tools_for_anthropic(tools)
        sys_for_sdk, api_messages, api_tools = self._apply_cache_control(
            anthropic_messages, system, api_tools_pre,
            model=model, idle_seconds=idle_s,
        )
        # Subsystem A — Effort-driven max_tokens floor lift: high-effort
        # calls on adaptive models need headroom for thinking + tool calls
        # (Doc 5: start at 64k).
        effective_max_tokens = max_tokens
        if (
            runtime_extras
            and runtime_extras.get("reasoning_effort") in ("high", "xhigh", "max")
            and supports_adaptive_thinking(model)
        ):
            effective_max_tokens = max(max_tokens, 64_000)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": effective_max_tokens,
            "messages": api_messages,
        }
        # Opus 4.7+ and Mythos reject temperature/top_p/top_k. Conditional
        # inclusion driven by model_capabilities.
        if supports_temperature(model):
            kwargs["temperature"] = temperature
        if sys_for_sdk:
            kwargs["system"] = sys_for_sdk
        if api_tools:
            kwargs["tools"] = api_tools
        # Tier 2.A — /reasoning + /fast slash commands → API kwargs.
        if runtime_extras:
            from opencomputer.agent.runtime_flags import (
                anthropic_kwargs_from_runtime,
            )
            kwargs.update(
                anthropic_kwargs_from_runtime(
                    model=model,
                    reasoning_effort=runtime_extras.get("reasoning_effort"),
                    service_tier=runtime_extras.get("service_tier"),
                )
            )
        # Subsystem C — structured outputs. Merge response_schema into
        # output_config (which may already hold `effort` from the
        # runtime_flags step above). Anthropic's output_config accepts
        # both `format` and `effort` simultaneously.
        if response_schema is not None:
            existing_output_config = kwargs.get("output_config", {})
            existing_output_config["format"] = {
                "type": "json_schema",
                "schema": response_schema["schema"],
            }
            kwargs["output_config"] = existing_output_config
        # SP4 — Anthropic Skills-via-API opt-in (see _do_complete for context).
        from types import SimpleNamespace as _SkillsRuntime
        kwargs = _augment_kwargs_for_skills(
            kwargs=kwargs,
            skill_ids=_resolve_anthropic_skills(_SkillsRuntime(custom=runtime_extras or {})),
        )
        t0 = time.monotonic()
        try:
            async with client.messages.stream(**kwargs) as stream_ctx:
                final = await stream_ctx.get_final_message()
        except AnthropicRateLimitError as exc:
            _record_429(exc)
            raise
        t1 = time.monotonic()
        result = self._parse_response(final)
        self._emit_llm_event(model=model, usage=result.usage, t0=t0, t1=t1, site=site)
        return result

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        runtime_extras: dict | None = None,
        response_schema: dict | None = None,
        site: str = "agent_loop",
    ) -> AsyncIterator[StreamEvent]:
        """Stream response events via Anthropic's `messages.stream()` context.

        Yields text_delta events as tokens arrive, then a single "done" event
        with the final ProviderResponse (including tool calls if any).
        """
        # SP2+SP3 integration: opt-in PDF Files API cache.
        files_cache, files_api_client = self._build_files_cache_pair(runtime_extras)
        if files_cache is not None:
            anthropic_messages = await self._to_anthropic_messages_async(
                messages, cache=files_cache, client=files_api_client,
            )
        else:
            anthropic_messages = self._to_anthropic_messages(messages)
        # TS-T1 — apply Anthropic prompt caching (system_and_3 strategy).
        # Idle-aware TTL: if this provider's last call was > 4 minutes
        # ago, the 5m cache would have expired before we got back to it.
        # Bump to 1h on Anthropic; safe no-op for providers that don't
        # support it. Time tracked per-provider-instance in monotonic
        # seconds (see __init__). Read defensively because some test
        # paths instantiate via ``__new__`` and skip __init__.
        import time as _time
        _now = _time.monotonic()
        _last = getattr(self, "_last_call_ts", 0.0)
        idle_s = (_now - _last) if _last > 0 else 0.0
        self._last_call_ts = _now
        # Item 1 (2026-05-02): build tools list FIRST so cache_control
        # can be applied to tools[-1] together with the system+messages
        # breakpoints in a single call (no two-call coordination footgun).
        api_tools_pre = _format_tools_for_anthropic(tools)
        sys_for_sdk, api_messages, api_tools = self._apply_cache_control(
            anthropic_messages, system, api_tools_pre,
            model=model, idle_seconds=idle_s,
        )
        # Subsystem A — Effort-driven max_tokens floor lift: high-effort
        # calls on adaptive models need headroom for thinking + tool calls
        # (Doc 5: start at 64k).
        effective_max_tokens = max_tokens
        if (
            runtime_extras
            and runtime_extras.get("reasoning_effort") in ("high", "xhigh", "max")
            and supports_adaptive_thinking(model)
        ):
            effective_max_tokens = max(max_tokens, 64_000)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": effective_max_tokens,
            "messages": api_messages,
        }
        # Opus 4.7+ and Mythos reject temperature/top_p/top_k. Conditional
        # inclusion driven by model_capabilities.
        if supports_temperature(model):
            kwargs["temperature"] = temperature
        if sys_for_sdk:
            kwargs["system"] = sys_for_sdk
        if api_tools:
            kwargs["tools"] = api_tools
        # Tier 2.A — /reasoning + /fast slash commands → API kwargs.
        if runtime_extras:
            from opencomputer.agent.runtime_flags import (
                anthropic_kwargs_from_runtime,
            )
            kwargs.update(
                anthropic_kwargs_from_runtime(
                    model=model,
                    reasoning_effort=runtime_extras.get("reasoning_effort"),
                    service_tier=runtime_extras.get("service_tier"),
                )
            )
        # Subsystem C — structured outputs. Merge response_schema into
        # output_config (which may already hold `effort` from the
        # runtime_flags step above). Anthropic's output_config accepts
        # both `format` and `effort` simultaneously.
        if response_schema is not None:
            existing_output_config = kwargs.get("output_config", {})
            existing_output_config["format"] = {
                "type": "json_schema",
                "schema": response_schema["schema"],
            }
            kwargs["output_config"] = existing_output_config

        if self._credential_pool is not None:
            # Pool path: stream_complete falls back to aggregated response on rotation.
            # Streaming is best-effort; on auth failure we rotate and re-try non-streaming.
            def _is_auth_failure(exc: Exception) -> bool:
                return "401" in str(exc) or "authentication" in str(exc).lower()

            response = await self._credential_pool.with_retry(
                lambda key: self._do_stream_complete(
                    key,
                    model=model,
                    messages=messages,
                    system=system,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    runtime_extras=runtime_extras,
                    response_schema=response_schema,
                ),
                is_auth_failure=_is_auth_failure,
            )
            if response.message.content:
                yield StreamEvent(kind="text_delta", text=response.message.content)
            yield StreamEvent(kind="done", response=response)
            return

        # No pool — native streaming path.
        # SP4 — Anthropic Skills-via-API opt-in (see _do_complete for context).
        from types import SimpleNamespace as _SkillsRuntime
        kwargs = _augment_kwargs_for_skills(
            kwargs=kwargs,
            skill_ids=_resolve_anthropic_skills(_SkillsRuntime(custom=runtime_extras or {})),
        )
        # TS-T7 — short-circuit if a previous 429 hasn't reset.
        _check_rate_limit()
        try:
            async with self.client.messages.stream(**kwargs) as stream:
                # Drop down to the raw event iterator (NOT
                # stream.text_stream) so thinking_delta events surface
                # alongside text_delta events. Each content_block_delta
                # carries a delta whose .type tells us the channel.
                async for event in stream:
                    if getattr(event, "type", None) != "content_block_delta":
                        continue
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    dtype = getattr(delta, "type", None)
                    if dtype == "text_delta":
                        chunk = getattr(delta, "text", "") or ""
                        if chunk:
                            yield StreamEvent(kind="text_delta", text=chunk)
                    elif dtype == "thinking_delta":
                        chunk = getattr(delta, "thinking", "") or ""
                        if chunk:
                            yield StreamEvent(
                                kind="thinking_delta", text=chunk
                            )
                    # Other delta kinds (input_json_delta, signature_delta)
                    # roll up into the final message via get_final_message.
                final = await stream.get_final_message()
        except AnthropicRateLimitError as exc:
            _record_429(exc)
            raise

        yield StreamEvent(kind="done", response=self._parse_response(final))

    async def complete_vision(
        self,
        *,
        model: str,
        image_base64: str,
        mime_type: str,
        prompt: str,
        max_tokens: int = 1024,
    ) -> str:
        """Run a vision completion via the existing chat-completions path.

        Anthropic's Messages API accepts the multimodal content-array
        shape natively — we wrap the image + prompt as a single user
        :class:`Message` and route through ``self.complete()``. The
        response's text content is returned verbatim.
        """
        from plugin_sdk.core import Message

        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": image_base64,
                },
            },
            {"type": "text", "text": prompt},
        ]
        resp = await self.complete(
            model=model,
            messages=[Message(role="user", content=content)],
            max_tokens=max_tokens,
        )
        return resp.message.content if resp and resp.message else ""

    async def submit_batch(self, requests):
        """Submit a batch via Anthropic's ``messages.batches.create``.

        Subsystem E (2026-05-02). 50% cost discount, ~1hr typical
        turnaround, 24h max. Composes with effort (Subsystem B) and
        response_schema (Subsystem C).
        """
        from plugin_sdk.provider_contract import BatchRequest as _Br

        entries: list[dict] = []
        for req in requests:
            assert isinstance(req, _Br)
            params: dict[str, Any] = {
                "model": req.model,
                "max_tokens": req.max_tokens,
                "messages": self._to_anthropic_messages(req.messages),
            }
            if req.system:
                params["system"] = req.system
            if supports_temperature(req.model):
                params["temperature"] = 1.0
            if req.runtime_extras:
                from opencomputer.agent.runtime_flags import (
                    anthropic_kwargs_from_runtime,
                )
                params.update(
                    anthropic_kwargs_from_runtime(
                        model=req.model,
                        reasoning_effort=req.runtime_extras.get("reasoning_effort"),
                        service_tier=req.runtime_extras.get("service_tier"),
                    )
                )
            if req.response_schema is not None:
                output_config = params.get("output_config", {})
                output_config["format"] = {
                    "type": "json_schema",
                    "schema": req.response_schema["schema"],
                }
                params["output_config"] = output_config
            entries.append({"custom_id": req.custom_id, "params": params})

        batch = await self.client.messages.batches.create(requests=entries)
        return batch.id

    async def get_batch_results(self, batch_id: str):
        """Fetch results for a batch.

        Returns one BatchResult per entry. If the batch is still
        processing, returns a single placeholder with
        ``status="processing"`` — caller polls again later.
        """
        from plugin_sdk.provider_contract import BatchResult as _Br

        batch = await self.client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "in_progress":
            return [_Br(custom_id="__pending__", status="processing")]

        out: list = []
        async for entry in await self.client.messages.batches.results(batch_id):
            result_obj = entry.result
            result_type = getattr(result_obj, "type", "errored")
            if result_type == "succeeded":
                response = self._parse_response(result_obj.message)
                out.append(
                    _Br(
                        custom_id=entry.custom_id,
                        status="succeeded",
                        response=response,
                    )
                )
            else:
                err_obj = getattr(result_obj, "error", None)
                err_msg = ""
                if err_obj is not None:
                    err_msg = str(getattr(err_obj, "message", err_obj))
                out.append(
                    _Br(
                        custom_id=entry.custom_id,
                        status=result_type,
                        error=err_msg,
                    )
                )
        return out

    async def count_tokens(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
    ) -> int:
        """Count input tokens via Anthropic's native ``messages.count_tokens`` endpoint.

        Falls back to the heuristic if the SDK call fails (e.g.,
        network error, model not yet supported by the endpoint).
        Subsystem D, 2026-05-02.
        """
        try:
            response = await self.client.messages.count_tokens(
                model=model,
                messages=self._to_anthropic_messages(messages),
                system=system if system else None,
                tools=_format_tools_for_anthropic(tools) or None,
            )
            return int(response.input_tokens)
        except Exception:  # noqa: BLE001 — fall back rather than fail
            from plugin_sdk.provider_contract import _heuristic_token_count
            return _heuristic_token_count(messages, system, tools)


__all__ = ["AnthropicProvider", "AnthropicProviderConfig"]
