"""Tag extraction — pulls semantic domain tags out of the user's message.

Two layers:

* Keyword fallback (``extract_tags_from_message``): the v0 algorithm.
  Lowercased word extraction with stopword filtering. Cheap, instant,
  noisy. Used as the fallback when LLM extraction is unavailable or
  takes too long, and used directly by the local-file backend's
  scoring formula.
* LLM extractor (``extract_tags_via_provider``): one Haiku call that
  rewrites the user message into 3-5 abstract domain tags
  (``homelab``, ``filesync``, ``rsync`` — not literal user words).
  Async, cost-guarded, soft-timeout. Returns ``None`` on any failure
  so callers can fall back without ceremony.

The high-level orchestrator :func:`extract_tags` glues them together
and adds two operational features:

* **Session-level cache** — first user message in a session pays the
  LLM cost; subsequent prompts in the same session reuse the cached
  tag set. A session is one task; tags shouldn't drift mid-task.
* **Profile-bias accumulator** — every successful extraction appends
  to ``<profile_home>/traces/tag_profile.json`` (a tag → count dict).
  ``tag_profile_top_n`` mixes the profile's most-frequent tags into
  pre-task queries so a sparse user message still pulls relevant
  history (e.g. user types "fix it" — profile says we work on homelab
  + rsync, query becomes ``[homelab, rsync, fix]`` and surfaces
  related traces).

Design constraint: the BEFORE_TASK hook has a 1500ms timeout. Pre-task
LLM tag extraction must fit inside it with budget for the network
query that follows. We use a 800ms soft cap on the LLM call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections import Counter, OrderedDict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_log = logging.getLogger("opencomputer.social_traces.tag_extractor")


# ─── keyword fallback (v0) ──────────────────────────────────────────


_STOPWORDS: frozenset[str] = frozenset({
    "and", "but", "for", "with", "from", "into", "onto", "between",
    "this", "that", "these", "those", "what", "which", "where", "when",
    "while", "have", "has", "had", "does", "did", "will", "would",
    "could", "should", "must", "shall", "been", "being", "are", "were",
    "the", "you", "your", "they", "their", "them", "ours", "yours",
    "please", "help", "want", "need", "needs", "needed", "make",
    "made", "using", "use", "used", "way", "ways", "thing", "things",
    "something", "anything", "everything", "nothing", "much", "many",
    "very", "just", "also", "then", "than", "such", "only", "still",
    "really", "actually", "basically", "quite", "okay", "sure",
    "let", "lets", "let's", "going", "got", "get", "able", "should",
    "want", "wanted", "needs", "look", "looking", "seem", "seems",
    "tell", "told", "give", "gave", "show", "showed", "shown", "say",
    "said", "know", "knew", "known", "think", "thought", "feel",
    "felt", "find", "found", "ask", "asked", "wonder", "wondered",
})

_MIN_TAG_LEN: int = 4
_DEFAULT_MAX_TAGS: int = 8


def extract_tags_from_message(
    text: str,
    *,
    max_tags: int = _DEFAULT_MAX_TAGS,
) -> tuple[str, ...]:
    """v0 keyword extraction — order-stable, deterministic. Used as
    the LLM-fallback path AND as the local-file backend's matcher."""
    if not text or not text.strip():
        return ()

    cleaned = "".join(c.lower() if c.isalnum() else " " for c in text)
    seen: set[str] = set()
    out: list[str] = []
    for word in cleaned.split():
        if len(word) < _MIN_TAG_LEN or word.isdigit() or word in _STOPWORDS:
            continue
        if word in seen:
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= max_tags:
            break
    return tuple(out)


# ─── LLM extraction (Phase 8) ───────────────────────────────────────


_TAG_MODEL = "claude-haiku-4-5"

#: Soft-timeout on the LLM call from the pre-task hook. The hook has a
#: 1500ms budget total; this leaves ~700ms for the network query +
#: scoring + injection formatting. Distiller calls bypass the timeout
#: (post-task path has no latency budget pressure).
PRETASK_LLM_TIMEOUT_S: float = 0.8

#: Max tokens we ask Haiku for. 64 is plenty for "5 hyphen-separated
#: tags on one line" and keeps cost low.
_TAG_MAX_TOKENS: int = 64

_TAG_SYSTEM_PROMPT = (
    "You are a tag-extraction utility for a knowledge-sharing system "
    "between AI agents. Given a user task, return 3-5 abstract domain "
    "tags that capture WHAT KIND OF WORK the task is — not the literal "
    "words. Tags must be:\n"
    "- lowercase\n"
    "- alphanumeric and hyphens only (no spaces, slashes, dots)\n"
    "- 2-30 characters each\n"
    "- specific to the domain, not generic ('rsync' not 'computers'; "
    "'homelab' not 'tech')\n"
    "- comma-separated, ONE LINE, no other prose\n\n"
    "Examples:\n"
    'User: "sync files between homelab boxes" → '
    "homelab, filesync, rsync\n"
    'User: "debug the flaky pytest CI run" → '
    "pytest, ci, debugging\n"
    'User: "set up a nginx reverse proxy with letsencrypt" → '
    "nginx, reverse-proxy, tls, letsencrypt\n\n"
    "Return ONLY the comma-separated tags."
)

_TAG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$")


def _parse_tag_response(text: str, *, max_tags: int) -> tuple[str, ...]:
    """Parse Haiku's comma-separated tag list, scrubbing each entry.
    Drops anything that fails the wire-format constraints (lowercase,
    2-30 chars, alnum+hyphen). Returns up to ``max_tags`` valid tags
    in original order."""
    if not text:
        return ()
    # Strip markdown code fences if Haiku decided to be fancy.
    cleaned = text.strip().replace("```", "").strip()
    raw_tags = [t.strip().lower() for t in cleaned.split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for tag in raw_tags:
        if not tag or tag in seen:
            continue
        if not _TAG_PATTERN.match(tag):
            continue
        out.append(tag)
        seen.add(tag)
        if len(out) >= max_tags:
            break
    return tuple(out)


def _budget_allows(decision: Any) -> bool:
    """Match the dataclass-or-bool shape that ``cost_guard.check_budget``
    can return (mirrors :mod:`distiller`)."""
    if isinstance(decision, bool):
        return decision
    if hasattr(decision, "allow"):
        return bool(decision.allow)
    return bool(decision)


async def extract_tags_via_provider(
    text: str,
    *,
    provider: Any,
    cost_guard: Any | None = None,
    max_tags: int = 5,
    timeout_s: float | None = PRETASK_LLM_TIMEOUT_S,
) -> tuple[str, ...] | None:
    """One Haiku call that converts a user message into domain tags.

    Returns ``None`` on any failure path:

    * provider missing (caller disabled LLM extraction)
    * cost-guard denial
    * provider exception
    * ``timeout_s`` exceeded (caller falls back to keyword)
    * malformed response (no parseable tags after scrubbing)

    Caller is expected to handle ``None`` by falling back to
    :func:`extract_tags_from_message`. ``timeout_s=None`` disables the
    timeout (used by the post-task distiller path which isn't latency-
    bound).
    """
    if provider is None or not text or not text.strip():
        return None

    if cost_guard is not None:
        try:
            decision = cost_guard.check_budget(
                "anthropic", projected_cost_usd=0.001,
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "social-traces: tag-extract cost_guard raised — degrading",
                exc_info=True,
            )
            return None
        if not _budget_allows(decision):
            _log.debug("social-traces: tag-extract skipped — cost_guard denied")
            return None

    # Lazy provider Message import — keeps the module importable even
    # when plugin_sdk isn't on the path (extension boundary tests).
    from plugin_sdk.core import Message

    async def _call() -> str | None:
        try:
            response = await provider.complete(
                model=_TAG_MODEL,
                messages=[Message(role="user", content=text)],
                system=_TAG_SYSTEM_PROMPT,
                max_tokens=_TAG_MAX_TOKENS,
                temperature=0.0,
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "social-traces: tag-extract provider.complete raised",
                exc_info=True,
            )
            return None
        try:
            if hasattr(response, "message"):
                content = response.message.content
                if isinstance(content, list):
                    return "".join(b.get("text", "") for b in content if isinstance(b, dict))
                return str(content)
        except Exception:  # noqa: BLE001
            pass
        return None

    try:
        if timeout_s is None:
            raw = await _call()
        else:
            raw = await asyncio.wait_for(_call(), timeout=timeout_s)
    except (asyncio.TimeoutError, TimeoutError):
        _log.debug(
            "social-traces: tag-extract timed out after %.2fs — falling back",
            timeout_s,
        )
        return None

    if cost_guard is not None and raw is not None:
        try:
            cost_guard.record_usage("anthropic", actual_cost_usd=0.001)
        except Exception:  # noqa: BLE001
            pass

    if raw is None:
        return None
    parsed = _parse_tag_response(raw, max_tags=max_tags)
    return parsed or None


# ─── session-level cache ─────────────────────────────────────────────


_session_cache: OrderedDict[str, tuple[str, ...]] = OrderedDict()
_session_cache_lock = threading.RLock()
_SESSION_CACHE_MAX = 256


def cache_tags_for_session(session_id: str, tags: tuple[str, ...]) -> None:
    """Store ``tags`` under ``session_id`` so subsequent prompts in
    the same session reuse the same tag set without paying the LLM
    cost again. LRU eviction caps memory."""
    if not session_id:
        return
    with _session_cache_lock:
        if session_id in _session_cache:
            _session_cache.move_to_end(session_id)
        _session_cache[session_id] = tags
        while len(_session_cache) > _SESSION_CACHE_MAX:
            _session_cache.popitem(last=False)


def cached_tags_for_session(session_id: str) -> tuple[str, ...] | None:
    """Look up cached tags. Returns ``None`` if no cache entry."""
    if not session_id:
        return None
    with _session_cache_lock:
        cached = _session_cache.get(session_id)
        if cached is not None:
            _session_cache.move_to_end(session_id)
        return cached


def reset_session_cache_for_testing() -> None:
    """Clear the in-process cache. Tests use this between cases."""
    with _session_cache_lock:
        _session_cache.clear()


# ─── per-profile tag accumulator ─────────────────────────────────────


_TAG_PROFILE_FILENAME = "tag_profile.json"


def _tag_profile_path(profile_home: Path) -> Path:
    return profile_home / "traces" / _TAG_PROFILE_FILENAME


def append_to_tag_profile(
    profile_home: Path, tags: Iterable[str],
) -> None:
    """Record a successful tag extraction into the profile's lifetime
    accumulator. Counts are weighted by occurrence so frequently-seen
    tags rank higher in :func:`tag_profile_top_n`.

    Disk format: one JSON file ``{tag: count, ...}``. Best-effort —
    failures log at DEBUG and are otherwise silent (this is bias
    enrichment, not load-bearing)."""
    tags_tuple = tuple(t for t in tags if t)
    if not tags_tuple:
        return
    path = _tag_profile_path(profile_home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {}
        else:
            raw = {}
    except (OSError, json.JSONDecodeError):
        _log.debug("social-traces: tag_profile read failed — starting fresh")
        raw = {}

    for tag in tags_tuple:
        raw[tag] = int(raw.get(tag, 0)) + 1

    try:
        path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    except OSError:
        _log.debug("social-traces: tag_profile write failed", exc_info=True)


def tag_profile_top_n(profile_home: Path, *, n: int = 5) -> tuple[str, ...]:
    """Return the ``n`` most-frequent tags in the profile's lifetime
    history, descending by count. Empty tuple if the file doesn't
    exist yet (first run on a fresh profile)."""
    path = _tag_profile_path(profile_home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return ()
    except (OSError, FileNotFoundError, json.JSONDecodeError):
        return ()
    counter = Counter()
    for tag, count in raw.items():
        if isinstance(tag, str) and isinstance(count, int):
            counter[tag] = count
    return tuple(t for t, _ in counter.most_common(n))


# ─── orchestrator ────────────────────────────────────────────────────


async def extract_tags(
    *,
    text: str,
    session_id: str | None,
    profile_home: Path | None,
    provider: Any | None = None,
    cost_guard: Any | None = None,
    max_tags: int = _DEFAULT_MAX_TAGS,
    profile_bias_n: int = 3,
    timeout_s: float | None = PRETASK_LLM_TIMEOUT_S,
) -> tuple[str, ...]:
    """Full extraction pipeline. Always returns a tuple — never raises.

    Order of operations:

    1. Session cache lookup. Hit → return verbatim (no profile-bias
       remix; the cached tags already reflect what we decided).
    2. LLM extraction (if ``provider`` available).
    3. Keyword extraction as fallback when LLM fails / times out /
       returns nothing.
    4. Mix in up to ``profile_bias_n`` top tags from the profile
       accumulator (if ``profile_home`` provided + file exists),
       deduplicated, capped at ``max_tags`` total.
    5. Cache + accumulate (so the next session message hits the cache
       and the profile gets richer).

    The LLM-or-keyword step always runs first. Profile bias is layered
    ON TOP — it doesn't replace the per-message tags, it expands them
    so a sparse message like "fix it" still gets matched against the
    agent's typical work area.
    """
    if session_id:
        cached = cached_tags_for_session(session_id)
        if cached is not None:
            return cached

    primary: tuple[str, ...] = ()
    if provider is not None:
        try:
            llm_tags = await extract_tags_via_provider(
                text,
                provider=provider,
                cost_guard=cost_guard,
                max_tags=max_tags,
                timeout_s=timeout_s,
            )
        except Exception:  # noqa: BLE001 — never raise into hooks
            _log.warning(
                "social-traces: extract_tags_via_provider raised — falling back",
                exc_info=True,
            )
            llm_tags = None
        if llm_tags:
            primary = llm_tags

    if not primary:
        primary = extract_tags_from_message(text, max_tags=max_tags)

    # Profile bias: pull top-N tags the agent typically deals with;
    # append any not already in `primary` until we hit max_tags.
    enriched = list(primary)
    if profile_home is not None and profile_bias_n > 0:
        bias = tag_profile_top_n(profile_home, n=profile_bias_n)
        seen = set(enriched)
        for tag in bias:
            if tag in seen:
                continue
            enriched.append(tag)
            seen.add(tag)
            if len(enriched) >= max_tags:
                break

    final = tuple(enriched[:max_tags])

    if session_id:
        cache_tags_for_session(session_id, final)
    if profile_home is not None and primary:
        # Only learn from the LLM/keyword output, not from the profile
        # bias we just mixed in (otherwise the most-frequent tag
        # accelerates exponentially).
        append_to_tag_profile(profile_home, primary)

    return final


__all__ = [
    "PRETASK_LLM_TIMEOUT_S",
    "append_to_tag_profile",
    "cache_tags_for_session",
    "cached_tags_for_session",
    "extract_tags",
    "extract_tags_from_message",
    "extract_tags_via_provider",
    "reset_session_cache_for_testing",
    "tag_profile_top_n",
]
