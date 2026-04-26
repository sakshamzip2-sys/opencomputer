"""Episodic-memory dreaming — Round 2A P-18 (EXPERIMENTAL).

A "dream" is a background turn that consolidates recent episodic-memory
entries (one row per completed conversation turn) into per-cluster summary
rows so the FTS5 cross-session search stays useful as the corpus grows.

Design constraints (from the round 2A plan, locked decision L6):

* OFF by default. Enabled per-profile via ``MemoryConfig.dreaming_enabled``
  (settable through ``opencomputer memory dream-on``).
* No embeddings — KISS. Cluster heuristic = same date bucket (ISO week)
  AND ≥1 shared topic keyword (tool name, file basename, or noun-ish
  token from the summary). Implementation in :func:`cluster_entries`.
* Uses the **cheap auxiliary model** when configured
  (:attr:`ModelConfig.cheap_model`); otherwise falls back to
  :attr:`ModelConfig.model`. Either way the call is short, low-tokens,
  and bounded.
* Idempotent. ``DreamRunner.run_once`` only operates on rows where
  ``dreamed_into IS NULL`` (the SQL filter is set in
  :meth:`SessionDB.list_undreamed_episodic`).
* Fail-safe. If the LLM call for a cluster raises, the runner retries
  ONCE; if that also fails the cluster is **skipped** without marking
  the originals as dreamed — they'll be retried on the next pass.

This module deliberately exposes both a **synchronous** entrypoint
(``DreamRunner.run_once``) so the CLI can invoke it without an
event-loop dance, and a small set of pure helpers (``cluster_entries``,
``build_cluster_prompt``, ``DEFAULT_FETCH_LIMIT``) so tests can verify
the clustering and prompt-shaping logic without needing a provider.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opencomputer.agent.config import Config
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider

_log = logging.getLogger("opencomputer.agent.dreaming")

#: Default cap on how many undreamed entries a single ``run_once`` reads
#: from the DB before clustering. Keeps the prompt + transaction cost
#: bounded; subsequent calls will sweep the rest.
DEFAULT_FETCH_LIMIT = 50

#: Min entries per cluster before we bother dreaming. Singletons would
#: produce a "summary of one item" turn — wasteful and rarely useful.
MIN_CLUSTER_SIZE = 2

#: Minimum number of shared topic tokens between an entry and a cluster
#: to count as a match. ``1`` would let common words like "step" or
#: "code" smear unrelated themes together; ``2`` keeps clusters tight
#: while still grouping the typical "<verb> <noun>" pair across turns.
MIN_OVERLAP = 2

#: Hard cap on consolidation summary length. Mirrors the
#: :data:`SUMMARY_MAX_CHARS` cap on episodic entries so consolidations
#: don't drown the FTS5 index.
CONSOLIDATION_MAX_CHARS = 480

#: Keyword stopwords stripped before noun-token extraction. Kept small
#: on purpose — the clustering heuristic only needs *any* shared term to
#: group entries, so a tight stoplist is fine.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "has", "have", "i", "in", "is", "it", "its", "of", "on", "or",
        "that", "the", "this", "to", "was", "we", "were", "will", "with",
        "you", "your", "do", "did", "done", "make", "made", "use", "used",
        "tools", "tool", "q", "files", "file", "code", "ran", "run", "fix",
        "fixed", "added", "add", "edit", "wrote", "write",
    }
)

#: Token regex — bare alphanumeric runs ≥3 chars. We don't care about
#: punctuation; the goal is "is this word shared between summaries".
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


# ─── pure helpers (clustering + prompt building) ────────────────────


def _date_bucket(timestamp: float) -> str:
    """Return an ISO-week-bucket string for ``timestamp`` (UTC).

    Format: ``YYYY-Www`` (e.g. ``2026-W17``). Week boundaries chosen to
    give a useful clustering granularity — tightly grouping entries from
    the same chunk of work without smearing across multi-week themes.
    """
    dt = _dt.datetime.fromtimestamp(float(timestamp), tz=_dt.UTC)
    iso_year, iso_week, _iso_day = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _topic_keywords(entry: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Pull two bags of topic tokens out of an episodic row.

    Returns ``(file_basenames, summary_tokens)`` so the clusterer can
    weight them differently:
      * **File basenames** are a strong signal — sharing one file is
        good enough to cluster two turns together.
      * **Summary tokens** are weaker — a single shared word like
        ``"step"`` or ``"docs"`` smears unrelated themes together, so
        they need ≥ :data:`MIN_OVERLAP` matches when no file overlap is
        present.

    ``tools_used`` deliberately doesn't contribute — every Edit-heavy
    turn would otherwise falsely cluster. Tool tokens that leak into
    the summary via ``render_template_summary``'s "[tools: ...]"
    prefix are stripped here so they don't pollute the bag.
    """
    files: set[str] = set()
    file_paths = (entry.get("file_paths") or "").strip()
    if file_paths:
        for p in file_paths.split(","):
            stem = Path(p.strip()).stem.lower()
            if stem:
                files.add(stem)
                # Test files cluster with their source counterparts:
                # `tests/test_auth.py` adds both "test_auth" AND "auth"
                # so the production file `src/auth.py` (basename "auth")
                # joins the same cluster.
                if stem.startswith("test_") and len(stem) > 5:
                    files.add(stem[5:])

    tokens: set[str] = set()
    summary = entry.get("summary") or ""
    # Strip the "[tools: A, B] " prefix (rendered by
    # render_template_summary) so tool names aren't double-counted as
    # topic tokens.
    cleaned = re.sub(r"^\[tools:[^\]]*\]\s*", "", summary, count=1)
    for tok in _TOKEN_RE.findall(cleaned.lower()):
        if tok in _STOPWORDS:
            continue
        tokens.add(tok)
    return files, tokens


@dataclass(slots=True)
class _Cluster:
    """An in-progress cluster being built by :func:`cluster_entries`."""

    bucket: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    files: set[str] = field(default_factory=set)
    tokens: set[str] = field(default_factory=set)


def cluster_entries(entries: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group episodic entries by (date-bucket, topic-keyword overlap).

    Algorithm (single-pass, deterministic on input order):
      1. Walk entries oldest→newest (caller's responsibility).
      2. For each entry, compute its date bucket + (file basenames,
         summary tokens) bags.
      3. Look for an existing cluster in the same bucket that matches
         via EITHER:
           * ≥ 1 shared file basename (strong signal), OR
           * ≥ :data:`MIN_OVERLAP` shared summary tokens (weak signal,
             needs more evidence so common words like ``"step"`` or
             ``"docs"`` don't smear unrelated themes).
         First match wins.
      4. If found, append + union both bags. Otherwise start a new cluster.

    Returns a list of clusters, each a list of entry dicts. Empty input
    → empty list. Singleton clusters (1 entry) ARE returned — the runner
    filters by :data:`MIN_CLUSTER_SIZE` before calling the model.
    """
    out: list[_Cluster] = []
    for entry in entries:
        bucket = _date_bucket(entry["timestamp"])
        files, tokens = _topic_keywords(entry)
        chosen: _Cluster | None = None
        for cluster in out:
            if cluster.bucket != bucket:
                continue
            # Empty-on-both-sides: degenerate, treat as match within
            # the bucket so content-free rows don't spawn singletons.
            if not (cluster.files or cluster.tokens) or not (files or tokens):
                chosen = cluster
                break
            file_overlap = len(cluster.files & files)
            token_overlap = len(cluster.tokens & tokens)
            if file_overlap >= 1 or token_overlap >= MIN_OVERLAP:
                chosen = cluster
                break
        if chosen is None:
            chosen = _Cluster(bucket=bucket)
            out.append(chosen)
        chosen.entries.append(entry)
        chosen.files |= files
        chosen.tokens |= tokens
    return [c.entries for c in out]


def build_cluster_prompt(cluster: Sequence[dict[str, Any]]) -> str:
    """Render the user-side prompt for one consolidation turn.

    Format follows the plan (P-18):
      "Here are <count> related episodic memories. Summarize the key
       themes + facts in <= 5 bullets. Be concise."
    Followed by a numbered list of summary lines.
    """
    lines = [
        f"Here are {len(cluster)} related episodic memories from a single "
        f"working session. Summarize the key themes + facts in <= 5 short "
        f"bullets. Be concise; one bullet per theme; no preamble.",
        "",
    ]
    for i, e in enumerate(cluster, 1):
        body = (e.get("summary") or "").strip().replace("\n", " ")
        lines.append(f"{i}. {body}")
    return "\n".join(lines)


def _aggregate_meta(cluster: Sequence[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Merge tool names + file paths across a cluster (deduped, order-preserving)."""
    seen_tools: dict[str, None] = {}
    seen_files: dict[str, None] = {}
    for e in cluster:
        tools = (e.get("tools_used") or "").strip()
        if tools:
            for t in tools.split(","):
                t = t.strip()
                if t and t not in seen_tools:
                    seen_tools[t] = None
        files = (e.get("file_paths") or "").strip()
        if files:
            for p in files.split(","):
                p = p.strip()
                if p and p not in seen_files:
                    seen_files[p] = None
    return list(seen_tools.keys()), list(seen_files.keys())


# ─── runner ────────────────────────────────────────────────────────


@dataclass(slots=True)
class DreamReport:
    """Summary of one ``DreamRunner.run_once`` invocation.

    Surfaced by the CLI so users can verify what dreaming did. Counts
    rather than payloads — keeps the report cheap to log.
    """

    fetched: int = 0
    clusters_total: int = 0
    consolidations_written: int = 0
    clusters_skipped_small: int = 0
    clusters_failed: int = 0


@dataclass(slots=True)
class DreamRunner:
    """Coordinates one dreaming pass.

    The runner is intentionally lightweight: it owns no async loop, no
    background scheduler, no persistence beyond the SessionDB it was
    constructed with. The CLI calls ``run_once`` synchronously; an
    external scheduler (cron / launchd / systemd) drives the cadence
    when ``MemoryConfig.dreaming_enabled`` is True.

    Construct via :meth:`from_config` to wire up the standard provider
    + DB; tests may pass a mock provider directly.
    """

    config: Config
    db: SessionDB
    provider: BaseProvider
    fetch_limit: int = DEFAULT_FETCH_LIMIT

    @classmethod
    def from_config(
        cls,
        config: Config,
        provider: BaseProvider,
        *,
        fetch_limit: int = DEFAULT_FETCH_LIMIT,
    ) -> DreamRunner:
        """Convenience constructor — wires the SessionDB from ``config.session.db_path``."""
        return cls(
            config=config,
            db=SessionDB(config.session.db_path),
            provider=provider,
            fetch_limit=fetch_limit,
        )

    # ─── public entrypoint ─────────────────────────────────────────

    def run_once(self, session_id: str | None = None) -> DreamReport:
        """Synchronously execute one dreaming pass.

        Steps (per the plan):
          1. Read up to ``fetch_limit`` undreamed episodic rows
             (oldest-first; optionally scoped to ``session_id``).
          2. Cluster by date bucket + topic-keyword overlap.
          3. For each cluster ≥ :data:`MIN_CLUSTER_SIZE` entries, call
             the configured (cheap or main) model and persist a
             consolidation row, marking originals as ``dreamed_into``.
          4. Return a :class:`DreamReport` describing the outcome.

        Empty store → no-op (returns a zero-counted report).
        Provider errors per-cluster → retry once, then skip that cluster
        (originals untouched). Other clusters still process.
        """
        return asyncio.run(self._run_async(session_id))

    # ─── async core (kept private; CLI uses sync wrapper) ─────────

    async def _run_async(self, session_id: str | None) -> DreamReport:
        report = DreamReport()
        rows = self.db.list_undreamed_episodic(
            session_id=session_id, limit=self.fetch_limit
        )
        report.fetched = len(rows)
        if not rows:
            _log.debug("dream-now: no undreamed episodic entries; skipping")
            return report

        clusters = cluster_entries(rows)
        report.clusters_total = len(clusters)
        _log.info(
            "dream-now: fetched=%d clusters=%d (limit=%d, session_id=%s)",
            report.fetched,
            report.clusters_total,
            self.fetch_limit,
            session_id or "<all>",
        )

        # Pick the consolidation model. Cheap-route preferred when
        # configured; the dreaming workload is short and bounded so the
        # capability gap penalty of a cheap model doesn't bite here.
        model_id = self.config.model.cheap_model or self.config.model.model

        for cluster in clusters:
            if len(cluster) < MIN_CLUSTER_SIZE:
                report.clusters_skipped_small += 1
                continue
            try:
                consolidation = await self._summarize_cluster(cluster, model=model_id)
            except Exception as exc:  # noqa: BLE001 — runner must survive any provider error
                _log.warning(
                    "dream-now: cluster summarization failed after retry; "
                    "skipping cluster (size=%d): %s",
                    len(cluster),
                    exc,
                )
                report.clusters_failed += 1
                continue

            # Resolve which session to attribute the consolidation to.
            # All entries in a cluster aren't guaranteed to share a
            # session (cross-session clusters are possible if the
            # caller didn't scope to a single session_id). We attribute
            # to the most-frequent session in the cluster — falling
            # back to the first entry if all are unique.
            target_session = _pick_session_id(cluster)

            tools_agg, files_agg = _aggregate_meta(cluster)
            source_ids = [int(e["id"]) for e in cluster]
            self.db.record_dream_consolidation(
                session_id=target_session,
                summary=consolidation,
                source_event_ids=source_ids,
                tools_used=tools_agg or None,
                file_paths=files_agg or None,
            )
            report.consolidations_written += 1

        _log.info(
            "dream-now: written=%d skipped=%d failed=%d",
            report.consolidations_written,
            report.clusters_skipped_small,
            report.clusters_failed,
        )
        return report

    # ─── cluster summarization (one LLM call per cluster) ─────────

    async def _summarize_cluster(
        self, cluster: Sequence[dict[str, Any]], *, model: str
    ) -> str:
        """Build the prompt, call the provider with ONE retry on failure.

        The prompt + max_tokens are small on purpose — dreaming is meant
        to be cheap. The provider is given no tools so it can only emit
        text.
        """
        prompt = build_cluster_prompt(cluster)
        messages = [Message(role="user", content=prompt)]
        system = (
            "You are an episodic-memory consolidator. Produce a tight "
            "Markdown bullet list (<= 5 bullets, no headings, no "
            "preamble) summarizing the THEMES and concrete FACTS the "
            "user worked on. Treat each input line as the gist of one "
            "completed conversation turn."
        )
        last_err: Exception | None = None
        for attempt in (1, 2):
            try:
                resp = await self.provider.complete(
                    model=model,
                    messages=messages,
                    system=system,
                    tools=None,
                    max_tokens=512,
                    temperature=0.2,
                    stream=False,
                )
                text = (resp.message.content or "").strip()
                if not text:
                    raise RuntimeError("provider returned empty content")
                # Clamp to keep FTS5 useful.
                if len(text) > CONSOLIDATION_MAX_CHARS:
                    text = text[: CONSOLIDATION_MAX_CHARS - 1] + "…"
                return text
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                _log.warning(
                    "dream-now: cluster summary attempt %d/%d failed: %s",
                    attempt,
                    2,
                    exc,
                )
        # Both attempts failed — propagate so caller can skip the cluster.
        assert last_err is not None
        raise last_err


# ─── helpers ───────────────────────────────────────────────────────


def _pick_session_id(cluster: Sequence[dict[str, Any]]) -> str:
    """Choose a session id to attribute the consolidation to.

    Picks the most-frequent ``session_id`` in the cluster so the
    consolidation lands in the conversation it overwhelmingly belongs
    to. Ties broken by taking the first (oldest) entry's session.
    """
    counts: dict[str, int] = {}
    first: str = str(cluster[0].get("session_id"))
    for e in cluster:
        sid = str(e.get("session_id"))
        counts[sid] = counts.get(sid, 0) + 1
    # Sort by (count desc, then first-seen index) — Python sort is
    # stable so we just pick the max-count entry, falling back to the
    # first encountered when tied.
    best_sid = first
    best_count = counts.get(first, 0)
    for sid, c in counts.items():
        if c > best_count:
            best_sid = sid
            best_count = c
    return best_sid


# ─── factory used by CLI ───────────────────────────────────────────


def build_runner_from_active_config(
    *,
    config: Config,
    provider_factory: Callable[[str], BaseProvider],
) -> DreamRunner:
    """Construct a :class:`DreamRunner` for the active config.

    ``provider_factory`` is a callable taking the configured provider
    name and returning a constructed :class:`BaseProvider` instance.
    Centralised here so the CLI doesn't have to know about plugin
    discovery + the dreaming module both.
    """
    provider = provider_factory(config.model.provider)
    return DreamRunner.from_config(config, provider)


__all__ = [
    "CONSOLIDATION_MAX_CHARS",
    "DEFAULT_FETCH_LIMIT",
    "DreamReport",
    "DreamRunner",
    "MIN_CLUSTER_SIZE",
    "build_cluster_prompt",
    "build_runner_from_active_config",
    "cluster_entries",
]
