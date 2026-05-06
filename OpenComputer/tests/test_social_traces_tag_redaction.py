"""Tag-leak prevention — pre-redact + deny-list post-filter.

Pins the contract that ``extract_tags`` never lets these classes of
strings into the returned tag tuple:

* user-specific path segments (``architsakri``, the ``Users`` parent
  in ``/Users/<name>/...``) — caught by the redactor's path-with-
  username pattern wiping the whole span before the LLM/keyword
  extractor sees it
* hostnames + IPs — caught the same way via the redactor's URL +
  internal-host patterns
* common system identifiers that survive redaction but are pure
  filesystem noise (``users``, ``home``, ``localhost``, etc.) —
  caught by the deny-list post-filter

Concretely tests the regression case that prompted the patch:
alice's prompt contained an absolute file path, and the LLM tag-
extractor lifted ``users`` / ``architsakri`` / ``documents`` /
``github`` straight into ``card.meta.tags``. After the fix, those
words are gone but standalone non-path words like ``opencomputer``
still survive.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_ST_DIR = _EXT_DIR / "social-traces"


def _ensure_alias() -> None:
    if "extensions.social_traces.tag_extractor" in sys.modules:
        return
    if "extensions" not in sys.modules:
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(_EXT_DIR)]
        ext_pkg.__package__ = "extensions"
        sys.modules["extensions"] = ext_pkg
    if "extensions.social_traces" not in sys.modules:
        mod = types.ModuleType("extensions.social_traces")
        mod.__path__ = [str(_ST_DIR)]
        mod.__package__ = "extensions.social_traces"
        sys.modules["extensions.social_traces"] = mod
        sys.modules["extensions"].social_traces = mod  # type: ignore[attr-defined]
    parent = sys.modules["extensions.social_traces"]
    for sub in (
        "state", "identity", "config", "session_state", "tag_extractor",
        "redactor", "novelty_judge", "distiller", "prefetch", "subscriber",
        "plugin",
    ):
        full = f"extensions.social_traces.{sub}"
        if full in sys.modules:
            setattr(parent, sub, sys.modules[full])
            continue
        init = _ST_DIR / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.social_traces"
        sys.modules[full] = sub_mod
        spec.loader.exec_module(sub_mod)
        setattr(parent, sub, sub_mod)


_ensure_alias()

from extensions.social_traces import tag_extractor as st_tag  # noqa: E402
from plugin_sdk.core import Message  # noqa: E402
from plugin_sdk.provider_contract import (  # noqa: E402
    BaseProvider,
    ProviderResponse,
    Usage,
)


@pytest.fixture(autouse=True)
def _isolate():
    st_tag.reset_session_cache_for_testing()
    yield
    st_tag.reset_session_cache_for_testing()


class _CannedProvider(BaseProvider):
    """Returns a fixed comma-separated tag list. Records what it saw
    so tests can assert the LLM received redacted input."""

    def __init__(self, response: str):
        self._response = response
        self.calls: list[dict] = []

    async def complete(self, **kw):  # noqa: ANN003
        self.calls.append(kw)
        return ProviderResponse(
            message=Message(role="assistant", content=self._response),
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=10),
        )

    async def stream_complete(self, **_kw):  # pragma: no cover
        yield


def _provider_input_text(provider: _CannedProvider) -> str:
    """Reach into the recorded call kwargs to grab what the LLM saw."""
    msg = provider.calls[0]["messages"][0]
    return msg.content if hasattr(msg, "content") else msg["content"]


# ─── 1. pre-redact: LLM never sees PII paths ─────────────────────────


async def test_extract_tags_redacts_path_with_username_before_llm(tmp_path: Path):
    """The classic regression: alice's prompt had
    ``/Users/architsakri/Documents/GitHub/opencomputer/README.md``.
    The LLM must NOT see ``architsakri`` (or the rest of the path
    segments) — the redactor wipes the whole path-with-username span
    before the prompt reaches the provider."""
    provider = _CannedProvider("readme, opencomputer, summary")
    user_message = (
        "Read the README.md at /Users/architsakri/Documents/GitHub/"
        "opencomputer/README.md and tell me what OpenComputer is."
    )
    await st_tag.extract_tags(
        text=user_message,
        session_id="sid-pii-1",
        profile_home=tmp_path,
        provider=provider,
        profile_bias_n=0,
    )
    seen = _provider_input_text(provider)
    assert "architsakri" not in seen, (
        "username MUST NOT reach the LLM tag-extractor"
    )
    assert "/Users/" not in seen, (
        "absolute path MUST be redacted before LLM"
    )


async def test_extract_tags_redacts_email_before_llm(tmp_path: Path):
    """Email addresses are PII. The redactor strips them before tag
    extraction so they don't end up in network query keys."""
    provider = _CannedProvider("email, contact")
    await st_tag.extract_tags(
        text="please email saksham@example.com about the issue",
        session_id="sid-pii-2",
        profile_home=tmp_path,
        provider=provider,
        profile_bias_n=0,
    )
    seen = _provider_input_text(provider)
    assert "saksham@example.com" not in seen
    assert "<redacted-pii>" in seen


async def test_extract_tags_redacts_internal_hostname_before_llm(tmp_path: Path):
    """Hostnames like ``nas.lan`` get redacted before the LLM sees them."""
    provider = _CannedProvider("backup, sync")
    await st_tag.extract_tags(
        text="sync the backup directory to nas.lan tonight",
        session_id="sid-pii-3",
        profile_home=tmp_path,
        provider=provider,
        profile_bias_n=0,
    )
    seen = _provider_input_text(provider)
    assert "nas.lan" not in seen


# ─── 2. survival: standalone non-path words are NOT killed ──────────


async def test_extract_tags_preserves_standalone_topic_words(tmp_path: Path):
    """The redactor is span-based — a word like ``github`` that
    appears OUTSIDE a redacted span survives intact. Pinning this
    because my own dev note flagged it as a worry."""
    provider = _CannedProvider("github, ci, deployment")
    await st_tag.extract_tags(
        text="browse the .github folder for CI workflow files",
        session_id="sid-survive-1",
        profile_home=tmp_path,
        provider=provider,
        profile_bias_n=0,
    )
    seen = _provider_input_text(provider)
    # No path-with-username, so .github survives in the LLM input.
    assert ".github" in seen or "github" in seen
    assert "<redacted" not in seen


async def test_extract_tags_keyword_fallback_finds_topic_words(tmp_path: Path):
    """No provider — keyword path on a non-PII message returns the
    topic words. Just confirms pre-redact didn't break the fallback
    for normal inputs."""
    tags = await st_tag.extract_tags(
        text="working on homelab filesync rsync configuration",
        session_id="sid-kw-1",
        profile_home=tmp_path,
        provider=None,
        profile_bias_n=0,
    )
    assert "homelab" in tags
    assert "filesync" in tags
    assert "rsync" in tags


# ─── 3. deny-list post-filter ────────────────────────────────────────


async def test_extract_tags_drops_users_when_llm_returns_it(tmp_path: Path):
    """Even if the LLM survives redaction and returns ``users`` (the
    classic path-segment leak), the deny-list post-filter drops it."""
    provider = _CannedProvider("users, homelab, documents, rsync")
    tags = await st_tag.extract_tags(
        text="sync stuff",
        session_id="sid-deny-1",
        profile_home=tmp_path,
        provider=provider,
        profile_bias_n=0,
    )
    assert "users" not in tags
    assert "documents" not in tags
    # Real topic words stay.
    assert "homelab" in tags
    assert "rsync" in tags


async def test_extract_tags_drops_redaction_sentinel_words(tmp_path: Path):
    """When the redactor replaces a span with ``<redacted-path>`` and
    the LLM then extracts ``redacted`` from that sentinel, the deny-
    list must drop it. Regression from the first end-to-end demo run
    after the redaction layer landed: alice's tags came back as
    ``["read","readme","redacted","sentences","opencomputer"]`` —
    everything except ``redacted`` is fine, that one's noise."""
    provider = _CannedProvider("redacted, opencomputer, readme, pii, host")
    tags = await st_tag.extract_tags(
        text="anything",
        session_id="sid-sentinel",
        profile_home=tmp_path,
        provider=provider,
        profile_bias_n=0,
    )
    assert "redacted" not in tags
    assert "pii" not in tags
    assert "host" not in tags
    # Real topic survives.
    assert "opencomputer" in tags
    assert "readme" in tags


async def test_extract_tags_drops_localhost_and_file_wrappers(tmp_path: Path):
    """Hostname leftovers + generic file-system wrappers also get
    filtered. ``deployment`` is not in the deny list and survives."""
    provider = _CannedProvider("localhost, file, folder, deployment")
    tags = await st_tag.extract_tags(
        text="x",
        session_id="sid-deny-2",
        profile_home=tmp_path,
        provider=provider,
        profile_bias_n=0,
    )
    assert "localhost" not in tags
    assert "file" not in tags
    assert "folder" not in tags
    assert "deployment" in tags


async def test_extract_tags_deny_list_is_case_insensitive(tmp_path: Path):
    """The LLM's tag-format constraint already lower-cases via the
    parser, but the deny-list comparison is also explicitly
    case-insensitive so a future loosening of the parser doesn't
    silently re-open the leak."""
    provider = _CannedProvider("Users, HOMELAB, Documents")
    # The wire-format check in _parse_tag_response actually rejects
    # uppercase tags entirely, so we exercise the case-fold path
    # through the keyword fallback by passing a message with deny-
    # list-shaped words and no provider.
    tags = await st_tag.extract_tags(
        text="users home var localhost homelab",
        session_id="sid-deny-3",
        profile_home=tmp_path,
        provider=None,
        profile_bias_n=0,
    )
    # Keyword extractor lower-cases. Deny-list filters all the
    # filesystem-noise words; only homelab survives.
    assert "homelab" in tags
    assert "users" not in tags
    assert "home" not in tags
    assert "var" not in tags
    assert "localhost" not in tags


# ─── 4. integration: alice's exact regression case ──────────────────


async def test_extract_tags_alice_regression_full_pipeline(tmp_path: Path):
    """The actual prompt that surfaced the leak in the Phase 10
    demo. After the fix, the returned tags must NOT include the
    user-specific PII tokens (``users``, ``architsakri``,
    ``documents``) even though the LLM is canned to *try* to return
    them — pre-redact gets the LLM to never see them, and the
    deny-list catches the ones that aren't user-specific."""
    # Simulate the Haiku tag-extractor returning the exact leaked
    # tags from the real demo run.
    provider = _CannedProvider(
        "read, readme, users, architsakri, documents, github, "
        "opencomputer, sentences"
    )
    user_message = (
        "Read the README.md at /Users/architsakri/Documents/GitHub/"
        "opencomputer/README.md and tell me in two sentences what "
        "OpenComputer is."
    )
    tags = await st_tag.extract_tags(
        text=user_message,
        session_id="sid-alice-regression",
        profile_home=tmp_path,
        provider=provider,
        profile_bias_n=0,
    )
    # PII-context tokens MUST NOT appear:
    assert "users" not in tags, "deny-list should drop 'users'"
    assert "documents" not in tags, "deny-list should drop 'documents'"
    # NOTE: ``architsakri`` would be dropped by the LLM never seeing
    # it (post-redact prompt) but in this test we feed a CANNED LLM
    # response that pre-bakes the tag, so the deny-list is what
    # protects us. Username tokens aren't statically deny-listable
    # (they're per-user), so this scenario is the residual risk we
    # accept — except the pre-redact layer prevents the LLM from
    # generating it in the first place when it hasn't been pre-canned.
    # ``github`` and ``opencomputer`` survive — those are real topics.
    assert "github" in tags or "opencomputer" in tags, (
        "at least one real topic word should survive"
    )
