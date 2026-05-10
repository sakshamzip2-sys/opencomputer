"""Phase 8 — LLM tag extractor + session cache + profile accumulator.

Coverage:

* ``extract_tags_via_provider`` — happy path, malformed response,
  cost-guard denial, provider exception, timeout, no-provider degrade.
* Session-level cache — first call hits provider, second call (same
  session) reuses, different session forces re-extraction.
* Profile accumulator — append round-trips, top-N orders by frequency,
  fresh profile returns empty.
* Orchestrator ``extract_tags`` — wires cache + LLM + keyword
  fallback + profile bias correctly.
* Prefetch path — ``build_query_async`` produces LLM tags when
  provider is available, falls back to keyword otherwise.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

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


from extensions.social_traces import prefetch as st_prefetch  # noqa: E402
from extensions.social_traces import tag_extractor as st_tag  # noqa: E402

from plugin_sdk.core import Message  # noqa: E402
from plugin_sdk.provider_contract import (  # noqa: E402
    BaseProvider,
    ProviderResponse,
    Usage,
)


@pytest.fixture(autouse=True)
def _isolate():
    """Reset the in-memory session cache between tests."""
    st_tag.reset_session_cache_for_testing()
    yield
    st_tag.reset_session_cache_for_testing()


# ─── helper providers ────────────────────────────────────────────────


class _ScriptedProvider(BaseProvider):
    """Returns the next canned response on each ``complete`` call,
    recording calls. Mirrors the pattern in test_social_traces_phase6."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, **kw):  # noqa: ANN003
        self.calls.append(kw)
        if not self._responses:
            raise AssertionError("ScriptedProvider exhausted")
        text = self._responses.pop(0)
        return ProviderResponse(
            message=Message(role="assistant", content=text),
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=10),
        )

    async def stream_complete(self, **_kw):  # pragma: no cover
        yield


class _RaisingProvider(BaseProvider):
    async def complete(self, **_kw):
        raise RuntimeError("simulated provider failure")

    async def stream_complete(self, **_kw):  # pragma: no cover
        yield


class _SlowProvider(BaseProvider):
    """Sleeps longer than any reasonable timeout — used to exercise
    the asyncio.wait_for path."""

    async def complete(self, **_kw):
        await asyncio.sleep(5.0)
        raise AssertionError("never reached if timeout works")

    async def stream_complete(self, **_kw):  # pragma: no cover
        yield


# ─── extract_tags_via_provider ───────────────────────────────────────


async def test_extract_via_provider_happy_path():
    provider = _ScriptedProvider(["homelab, filesync, rsync"])
    tags = await st_tag.extract_tags_via_provider(
        "sync homelab files", provider=provider, max_tags=5,
    )
    assert tags == ("homelab", "filesync", "rsync")
    assert len(provider.calls) == 1


async def test_extract_via_provider_strips_markdown_fences():
    """Haiku might fence the response — the parser handles it."""
    provider = _ScriptedProvider(["```\nhomelab, rsync\n```"])
    tags = await st_tag.extract_tags_via_provider(
        "x", provider=provider, max_tags=5,
    )
    assert tags == ("homelab", "rsync")


async def test_extract_via_provider_drops_invalid_tags():
    """Tags failing the wire-format constraints (uppercase, spaces,
    etc) get scrubbed; valid ones survive."""
    provider = _ScriptedProvider(
        ["UPPER, has space, valid-tag, x, also-valid, !!!"],
    )
    tags = await st_tag.extract_tags_via_provider(
        "x", provider=provider, max_tags=5,
    )
    # "UPPER" → reject (uppercase); "has space" → reject; "valid-tag"
    # → keep; "x" → reject (length 1 < 2); "also-valid" → keep; "!!!"
    # → reject.
    assert "valid-tag" in tags
    assert "also-valid" in tags
    assert all(t.islower() for t in tags)


async def test_extract_via_provider_no_provider_returns_none():
    tags = await st_tag.extract_tags_via_provider(
        "x", provider=None,
    )
    assert tags is None


async def test_extract_via_provider_empty_input_returns_none():
    provider = _ScriptedProvider(["should not be reached"])
    assert await st_tag.extract_tags_via_provider("", provider=provider) is None
    assert await st_tag.extract_tags_via_provider("   ", provider=provider) is None
    assert provider.calls == []


async def test_extract_via_provider_provider_exception_returns_none():
    tags = await st_tag.extract_tags_via_provider(
        "x", provider=_RaisingProvider(),
    )
    assert tags is None


async def test_extract_via_provider_timeout_returns_none():
    """A provider that sleeps past the timeout returns None — the
    caller falls back to keyword extraction."""
    tags = await st_tag.extract_tags_via_provider(
        "x", provider=_SlowProvider(), timeout_s=0.05,
    )
    assert tags is None


async def test_extract_via_provider_cost_guard_denial_returns_none():
    class _DenyGuard:
        def check_budget(self, *_a, **_kw):
            return False

        def record_usage(self, *_a, **_kw):
            return None

    provider = _ScriptedProvider(["should not run"])
    tags = await st_tag.extract_tags_via_provider(
        "x", provider=provider, cost_guard=_DenyGuard(),
    )
    assert tags is None
    assert provider.calls == []


async def test_extract_via_provider_records_usage_on_success():
    """When the call succeeds, ``cost_guard.record_usage`` is invoked
    so cumulative cost tracking works."""
    recorded: list = []

    class _Guard:
        def check_budget(self, *_a, **_kw):
            return True

        def record_usage(self, *args, **kwargs):
            recorded.append((args, kwargs))

    await st_tag.extract_tags_via_provider(
        "x",
        provider=_ScriptedProvider(["a, b, c"]),
        cost_guard=_Guard(),
    )
    assert len(recorded) == 1


async def test_extract_via_provider_returns_none_on_unparseable_response():
    """Provider returns gibberish → parser yields no valid tags →
    we return None so caller falls back."""
    provider = _ScriptedProvider(["!!! this isn't parseable !!! @@@"])
    tags = await st_tag.extract_tags_via_provider("x", provider=provider)
    assert tags is None


# ─── session cache ───────────────────────────────────────────────────


def test_session_cache_round_trip():
    st_tag.cache_tags_for_session("sid-1", ("a", "b"))
    assert st_tag.cached_tags_for_session("sid-1") == ("a", "b")


def test_session_cache_missing_returns_none():
    assert st_tag.cached_tags_for_session("never-set") is None


def test_session_cache_empty_session_id_is_noop():
    st_tag.cache_tags_for_session("", ("a", "b"))
    assert st_tag.cached_tags_for_session("") is None


def test_session_cache_overwrite():
    st_tag.cache_tags_for_session("sid", ("a",))
    st_tag.cache_tags_for_session("sid", ("b", "c"))
    assert st_tag.cached_tags_for_session("sid") == ("b", "c")


# ─── profile accumulator ─────────────────────────────────────────────


def test_tag_profile_round_trip(tmp_path: Path):
    st_tag.append_to_tag_profile(tmp_path, ("homelab", "rsync"))
    st_tag.append_to_tag_profile(tmp_path, ("homelab", "ci"))
    top = st_tag.tag_profile_top_n(tmp_path, n=10)
    # homelab appears twice, rsync + ci once each. homelab ranks first.
    assert top[0] == "homelab"
    assert set(top) == {"homelab", "rsync", "ci"}


def test_tag_profile_empty_returns_empty(tmp_path: Path):
    assert st_tag.tag_profile_top_n(tmp_path, n=5) == ()


def test_tag_profile_top_n_orders_by_frequency(tmp_path: Path):
    for _ in range(5):
        st_tag.append_to_tag_profile(tmp_path, ("frequent",))
    for _ in range(2):
        st_tag.append_to_tag_profile(tmp_path, ("medium",))
    st_tag.append_to_tag_profile(tmp_path, ("rare",))
    top = st_tag.tag_profile_top_n(tmp_path, n=3)
    assert top == ("frequent", "medium", "rare")


def test_tag_profile_persists_across_calls(tmp_path: Path):
    """The accumulator is disk-backed — a second process / fresh
    function call reads the same data."""
    st_tag.append_to_tag_profile(tmp_path, ("a", "b"))
    # Verify on disk.
    raw = json.loads((tmp_path / "traces" / "tag_profile.json").read_text())
    assert raw == {"a": 1, "b": 1}


def test_tag_profile_tolerates_corrupted_file(tmp_path: Path):
    """A malformed tag_profile.json shouldn't break the appender —
    it overwrites with a fresh count."""
    (tmp_path / "traces").mkdir()
    (tmp_path / "traces" / "tag_profile.json").write_text("not valid json")
    st_tag.append_to_tag_profile(tmp_path, ("recover",))
    raw = json.loads((tmp_path / "traces" / "tag_profile.json").read_text())
    assert raw == {"recover": 1}


# ─── extract_tags orchestrator ───────────────────────────────────────


async def test_orchestrator_session_cache_hit_skips_provider(tmp_path: Path):
    """First call hits provider; second call with same session_id
    returns from cache without touching provider."""
    provider = _ScriptedProvider(["homelab, rsync"])
    tags1 = await st_tag.extract_tags(
        text="sync files",
        session_id="sid-cache",
        profile_home=tmp_path,
        provider=provider,
        profile_bias_n=0,  # no bias to keep assertion clean
    )
    assert "homelab" in tags1
    assert len(provider.calls) == 1

    tags2 = await st_tag.extract_tags(
        text="totally different message",
        session_id="sid-cache",  # SAME session
        profile_home=tmp_path,
        provider=provider,
        profile_bias_n=0,
    )
    # Cache hit — same tags, no new provider call.
    assert tags2 == tags1
    assert len(provider.calls) == 1


async def test_orchestrator_falls_back_to_keyword_when_provider_fails(
    tmp_path: Path,
):
    """Provider raises → orchestrator returns keyword-extracted tags
    rather than empty."""
    tags = await st_tag.extract_tags(
        text="fix the homelab filesync issue",
        session_id="sid-x",
        profile_home=tmp_path,
        provider=_RaisingProvider(),
        profile_bias_n=0,
    )
    # Keyword extraction matches the message words.
    assert "homelab" in tags
    assert "filesync" in tags


async def test_orchestrator_no_provider_uses_keyword(tmp_path: Path):
    tags = await st_tag.extract_tags(
        text="fix the rsync issue",
        session_id="sid",
        profile_home=tmp_path,
        provider=None,
        profile_bias_n=0,
    )
    assert "rsync" in tags


async def test_orchestrator_profile_bias_layered_on_top(tmp_path: Path):
    """Profile bias adds tags that ARE in the lifetime accumulator
    but NOT in the per-message extraction."""
    # Seed the accumulator.
    st_tag.append_to_tag_profile(tmp_path, ("homelab",) * 5)
    st_tag.append_to_tag_profile(tmp_path, ("rsync",) * 3)

    # User message that doesn't mention either keyword AND no LLM.
    tags = await st_tag.extract_tags(
        text="please make this work it broke again",
        session_id="sid-bias",
        profile_home=tmp_path,
        provider=None,
        profile_bias_n=2,
    )
    # The keyword extractor pulls non-stopwords; let's just assert
    # the profile-bias tags were appended.
    assert "homelab" in tags
    assert "rsync" in tags


async def test_orchestrator_persists_to_profile_after_extraction(tmp_path: Path):
    """Tags extracted from a real message land in the accumulator."""
    await st_tag.extract_tags(
        text="working on homelab filesync rsync stuff",
        session_id="sid-persist",
        profile_home=tmp_path,
        provider=None,
        profile_bias_n=0,
    )
    top = st_tag.tag_profile_top_n(tmp_path, n=10)
    assert "homelab" in top
    assert "filesync" in top


async def test_orchestrator_no_double_count_from_profile_bias(tmp_path: Path):
    """The profile bias tags are NOT re-appended — only the LLM/keyword
    output is, otherwise frequent tags would accelerate exponentially."""
    st_tag.append_to_tag_profile(tmp_path, ("homelab",) * 10)
    # User message has no homelab in it.
    await st_tag.extract_tags(
        text="completely unrelated topic about cooking pasta dinners",
        session_id="sid-cook",
        profile_home=tmp_path,
        provider=None,
        profile_bias_n=3,
    )
    # homelab count must still be 10 — the bias-mixed homelab wasn't
    # re-counted.
    raw = json.loads((tmp_path / "traces" / "tag_profile.json").read_text())
    assert raw["homelab"] == 10


# ─── prefetch.build_query_async ──────────────────────────────────────


async def test_build_query_async_uses_provider(tmp_path: Path):
    provider = _ScriptedProvider(["homelab, rsync, filesync"])
    intent, tags = await st_prefetch.build_query_async(
        "sync homelab files between machines",
        session_id="sid-bq",
        profile_home=tmp_path,
        provider=provider,
    )
    assert intent.startswith("sync homelab")
    assert "homelab" in tags
    assert "rsync" in tags
    assert "filesync" in tags


async def test_build_query_async_no_provider_keyword_path(tmp_path: Path):
    intent, tags = await st_prefetch.build_query_async(
        "homelab debugging session",
        session_id=None,
        profile_home=None,
        provider=None,
    )
    # Falls through to keyword extraction.
    assert "homelab" in tags
    assert "debugging" in tags


async def test_build_query_async_truncates_long_intent(tmp_path: Path):
    long = "x" * 2000
    intent, _ = await st_prefetch.build_query_async(
        long, session_id=None, profile_home=tmp_path, provider=None,
    )
    assert len(intent) == 500
    assert intent.endswith("...")


def test_build_query_sync_unchanged():
    """The sync ``build_query`` (kept for back-compat) still does
    keyword-only extraction with no LLM, no async, no cache."""
    intent, tags = st_prefetch.build_query("homelab debugging stuff")
    assert "homelab" in tags
    assert "debugging" in tags


# ─── parser tightness ────────────────────────────────────────────────


def test_parse_tag_response_dedupes():
    parsed = st_tag._parse_tag_response("a, b, a, c", max_tags=10)
    assert parsed == ("a", "b", "c")


def test_parse_tag_response_respects_max():
    parsed = st_tag._parse_tag_response("a, b, c, d, e, f", max_tags=3)
    assert parsed == ("a", "b", "c")


def test_parse_tag_response_empty_input():
    assert st_tag._parse_tag_response("", max_tags=5) == ()
    assert st_tag._parse_tag_response("   ", max_tags=5) == ()
