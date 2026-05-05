"""Local-file backend for the trace network — Phase 3.

Implements :class:`plugin_sdk.TraceNetworkClient` over JSON files under
``<profile_home>/traces/{inbox,outbox}/``. The dev stub that lets the
plugin run end-to-end without OpenHub being deployed.

Layout
------
::

    <profile_home>/traces/
    ├── inbox/                 # "approved" traces — what queries return
    │   └── <trace_id>.json    # one TraceCard per file
    └── outbox/                # pending submissions — what submit() writes
        └── <queue_id>.json    # one queued submission per file

The shape mirrors what OpenHub will return / accept over HTTP: query
reads from ``inbox/`` (simulating "approved + ranked" results), submit
writes to ``outbox/`` (simulating "queued for admin review").

A single dev machine can simulate multiple agents by:

1. Profile A runs a session, emits a TraceCard → lands in
   ``profile_a/traces/outbox/``.
2. Operator (you) reviews the JSON, copies it into
   ``profile_b/traces/inbox/``.
3. Profile B runs a session — pre-task query finds the trace and
   injects it.

That's the local equivalent of the full Mac → Pi → server flow
described in :file:`openhub-mvp.md` §12.

Scoring
-------
Local query uses a deliberately simple scoring formula so the dev
experience matches the production one approximately, without
re-implementing the full curation engine:

* Tag overlap (count of input tags also in the trace): primary signal.
* Intent token overlap (lowercased word intersection): secondary
  signal, weighted at 0.5.
* Outcome: ``success`` traces are weighted up.

Top-K traces (ordered by combined score, descending) are returned. The
plugin's :mod:`extensions.social_traces.prefetch` module applies its
own relevance threshold on top — server gives candidates, client
decides which to use. Phase 4 wires that consumer side.

Failure modes
-------------
Per the :class:`TraceNetworkClient` contract:

* :meth:`query` and :meth:`health` MUST honour ``timeout_s`` and never
  raise on transient failures (return empty / False).
* :meth:`submit` MUST NOT raise on transient failures — return
  ``SubmitReceipt(accepted=False, reason=...)`` so the caller's outbox
  retry path takes over.

For the local backend most "transient" failures are filesystem hiccups
(permission errors on read, disk full on write). We handle them the
same way: log + return empty / False / accepted=False.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

from plugin_sdk.traces import (
    QueryResult,
    SubmitReceipt,
    TraceCard,
    TraceMeta,
    TraceNetworkClient,
    TraceStep,
)

_log = logging.getLogger("opencomputer.social_traces.client.local_file")

INBOX_DIRNAME = "inbox"
OUTBOX_DIRNAME = "outbox"


# ─── serialization helpers (mirror tests/plugin_sdk/test_traces.py) ──


def trace_card_to_dict(card: TraceCard) -> dict:
    """Convert a TraceCard to a plain JSON-serializable dict.

    Uses :func:`dataclasses.asdict` which recursively converts nested
    dataclasses (TraceMeta, TraceStep). Tuples become lists in JSON; we
    re-tuplise them on the way back in :func:`trace_card_from_dict`.
    """
    return dataclasses.asdict(card)


def trace_card_from_dict(raw: dict) -> TraceCard:
    """Reconstruct a TraceCard from JSON-deserialized data.

    Tolerates the JSON-tuple-as-list quirk by passing through
    :func:`tuple` for fields that are tuples in the dataclass.

    Returns the canonical, frozen TraceCard. Raises ``KeyError`` /
    ``TypeError`` if the dict is missing required fields — caller is
    expected to handle malformed entries (e.g. skip and log).
    """
    meta_raw = raw["meta"]
    meta = TraceMeta(
        tags=tuple(meta_raw["tags"]),
        outcome=meta_raw["outcome"],
        token_cost=int(meta_raw["token_cost"]),
        loop_count=int(meta_raw["loop_count"]),
        harness_version=meta_raw["harness_version"],
        submitter_hash=meta_raw["submitter_hash"],
    )
    steps = tuple(
        TraceStep(
            tool_name=s["tool_name"],
            arguments_summary=s["arguments_summary"],
            result_summary=s["result_summary"],
            duration_ms=int(s["duration_ms"]),
        )
        for s in raw["steps"]
    )
    return TraceCard(
        schema_version=raw["schema_version"],
        intent=raw["intent"],
        meta=meta,
        steps=steps,
        distilled_insight=raw["distilled_insight"],
        created_at=raw["created_at"],
        id=raw.get("id"),
        status=raw.get("status"),
        score=raw.get("score"),
    )


# ─── scoring (used only by the local backend) ────────────────────────


_OUTCOME_WEIGHT = {"success": 1.0, "partial": 0.5, "failed": 0.1}


def _tokenize(text: str) -> set[str]:
    """Lowercase, strip non-alpha, drop short noise words, return a set
    of tokens.

    Cheap and good-enough for dev-stub scoring. Real production matching
    lives server-side in OpenHub's curation engine.
    """
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in text)
    return {w for w in cleaned.split() if len(w) >= 3}


def score_trace(
    card: TraceCard,
    *,
    intent: str,
    tags: tuple[str, ...],
) -> float:
    """Compute a relevance score for ``card`` against an ``(intent, tags)`` query.

    Higher is better. Used to rank results returned by
    :meth:`LocalFileTraceNetworkClient.query`. Mirrors (loosely) what
    OpenHub will compute server-side once the curation engine lands.

    Returns ``0.0`` when there is NO tag or word overlap — outcome alone
    must not qualify a trace for return (otherwise every success trace
    matches every query). Outcome is a tiebreaker among already-relevant
    matches.
    """
    input_tag_set = {t.lower() for t in tags}
    card_tag_set = {t.lower() for t in card.meta.tags}
    tag_overlap = len(input_tag_set & card_tag_set)

    intent_tokens = _tokenize(intent)
    card_intent_tokens = _tokenize(card.intent)
    word_overlap = len(intent_tokens & card_intent_tokens)

    relevance = float(tag_overlap) + 0.5 * float(word_overlap)
    if relevance <= 0.0:
        return 0.0

    outcome_weight = _OUTCOME_WEIGHT.get(card.meta.outcome, 0.0)
    return relevance + outcome_weight


# ─── client ──────────────────────────────────────────────────────────


class LocalFileTraceNetworkClient(TraceNetworkClient):
    """File-backed :class:`TraceNetworkClient`.

    All filesystem I/O is wrapped in :func:`asyncio.to_thread` so the
    backend honours its async contract and doesn't block the event loop
    when called from a real running agent.

    Construction is side-effect-free — directories are created lazily
    on first write so a fresh profile that never enables the plugin
    leaves no traces of social-traces (pun intended).
    """

    def __init__(self, *, profile_home: Path) -> None:
        self._profile_home = Path(profile_home)
        self._traces_root = self._profile_home / "traces"
        self._inbox = self._traces_root / INBOX_DIRNAME
        self._outbox = self._traces_root / OUTBOX_DIRNAME

    # ─── public API (TraceNetworkClient contract) ───────────────────

    async def query(
        self,
        intent: str,
        tags: tuple[str, ...],
        *,
        limit: int = 3,
        timeout_s: float = 1.0,
    ) -> QueryResult:
        """Scan ``inbox/`` and return the top-K matching traces.

        Empty inbox → empty result. Filesystem error → empty result
        (logged at WARNING). Timeout → empty result (logged at INFO).
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._query_sync, intent, tags, limit),
                timeout=timeout_s,
            )
        except TimeoutError:
            _log.info(
                "social-traces: local query timed out after %.2fs — "
                "returning empty",
                timeout_s,
            )
            return QueryResult()
        except Exception:  # noqa: BLE001 — never raise from a network call
            _log.warning(
                "social-traces: local query raised — returning empty",
                exc_info=True,
            )
            return QueryResult()

    async def submit(self, card: TraceCard) -> SubmitReceipt:
        """Write ``card`` to ``outbox/`` for the operator to inspect.

        Returns ``accepted=True`` on success, ``accepted=False`` with a
        reason on filesystem failure. Never raises.
        """
        try:
            queue_id = await asyncio.to_thread(self._submit_sync, card)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "social-traces: local submit failed — %s", exc, exc_info=True
            )
            return SubmitReceipt(
                accepted=False, reason=f"local-file submit failed: {exc}"
            )
        return SubmitReceipt(accepted=True, queue_id=queue_id)

    async def health(self, *, timeout_s: float = 1.0) -> bool:
        """Return True iff the traces directory is accessible + writable.

        Cheap, sync-ish check wrapped in to_thread for the contract.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._health_sync),
                timeout=timeout_s,
            )
        except TimeoutError:
            return False
        except Exception:  # noqa: BLE001
            return False

    # ─── inbox-management helpers (used by CLI) ──────────────────────

    def list_inbox(self) -> list[tuple[str, TraceCard]]:
        """Return ``[(filename_stem, card), ...]`` for every readable
        TraceCard in the inbox. Skips malformed entries.

        Used by the ``oc traces inbox list`` CLI verb. Stable iteration
        order — sorted by filename for predictable test output.
        """
        if not self._inbox.exists():
            return []
        out: list[tuple[str, TraceCard]] = []
        for path in sorted(self._inbox.glob("*.json")):
            card = self._load_one(path)
            if card is None:
                continue
            out.append((path.stem, card))
        return out

    def show_inbox(self, ident: str) -> TraceCard | None:
        """Return the TraceCard whose ``id`` or filename stem matches
        ``ident``. ``None`` if not found or unreadable."""
        match = self._resolve_inbox_path(ident)
        if match is None:
            return None
        return self._load_one(match)

    def add_to_inbox(self, source: Path) -> Path:
        """Copy ``source`` (a TraceCard JSON file) into the inbox.

        Validates the JSON parses as a TraceCard before copying — a bad
        file should fail fast at CLI time rather than at query time.

        Returns the destination path. Raises if validation fails.
        """
        text = source.read_text(encoding="utf-8")
        raw = json.loads(text)
        # Validate by reconstruction — raises if malformed.
        card = trace_card_from_dict(raw)

        self._inbox.mkdir(parents=True, exist_ok=True)
        # Use the card's ``id`` if present, otherwise the source filename
        # stem, otherwise a fresh uuid. Mirrors how a network response
        # would always carry an id.
        stem = card.id or source.stem or secrets.token_hex(8)
        dest = self._inbox / f"{stem}.json"
        # Re-serialize through the canonical encoder so the on-disk shape
        # is normalized regardless of the source file's formatting.
        dest.write_text(
            json.dumps(trace_card_to_dict(card), indent=2),
            encoding="utf-8",
        )
        return dest

    def remove_from_inbox(self, ident: str) -> bool:
        """Delete the trace identified by ``ident`` (id or filename
        stem). Returns True if removed, False if not found."""
        match = self._resolve_inbox_path(ident)
        if match is None:
            return False
        try:
            match.unlink()
        except OSError:
            return False
        return True

    def list_outbox(self) -> list[tuple[str, TraceCard]]:
        """Return pending submissions. Same shape as :meth:`list_inbox`."""
        if not self._outbox.exists():
            return []
        out: list[tuple[str, TraceCard]] = []
        for path in sorted(self._outbox.glob("*.json")):
            card = self._load_one(path)
            if card is None:
                continue
            out.append((path.stem, card))
        return out

    # ─── sync internals (run inside asyncio.to_thread) ───────────────

    def _query_sync(
        self, intent: str, tags: tuple[str, ...], limit: int
    ) -> QueryResult:
        if not self._inbox.exists():
            return QueryResult()

        scored: list[tuple[float, TraceCard]] = []
        for path in self._inbox.glob("*.json"):
            card = self._load_one(path)
            if card is None:
                continue
            score = score_trace(card, intent=intent, tags=tags)
            if score <= 0.0:
                continue  # nothing in common — skip
            scored.append((score, card))

        scored.sort(key=lambda pair: pair[0], reverse=True)

        # Stamp the per-trace score onto the returned card. Mirrors what
        # OpenHub does server-side — the prefetch path's relevance gate
        # reads ``card.score`` to decide whether to inject. Without this
        # stamp the gate has no signal and would have to re-score
        # client-side (re-implementing the curation engine in two
        # places).
        top: list[TraceCard] = []
        for score, card in scored[:limit]:
            top.append(
                TraceCard(
                    schema_version=card.schema_version,
                    intent=card.intent,
                    meta=card.meta,
                    steps=card.steps,
                    distilled_insight=card.distilled_insight,
                    created_at=card.created_at,
                    id=card.id,
                    status=card.status,
                    score=score,
                )
            )

        return QueryResult(
            traces=tuple(top),
            query_id=secrets.token_hex(8),
            served_from="network",  # consistent with HTTP backend response
        )

    def _submit_sync(self, card: TraceCard) -> str:
        self._outbox.mkdir(parents=True, exist_ok=True)
        queue_id = secrets.token_hex(12)
        # Stamp ``id`` and ``status`` so the on-disk shape matches what
        # OpenHub would return — useful when the operator promotes the
        # outbox file into another profile's inbox.
        stamped = TraceCard(
            schema_version=card.schema_version,
            intent=card.intent,
            meta=card.meta,
            steps=card.steps,
            distilled_insight=card.distilled_insight,
            created_at=card.created_at or datetime.now(UTC).isoformat(),
            id=card.id or queue_id,
            status="pending",
            score=None,
        )
        path = self._outbox / f"{queue_id}.json"
        path.write_text(
            json.dumps(trace_card_to_dict(stamped), indent=2),
            encoding="utf-8",
        )
        return queue_id

    def _health_sync(self) -> bool:
        try:
            self._traces_root.mkdir(parents=True, exist_ok=True)
            # Probe write access without leaving a permanent file.
            probe = self._traces_root / f".health-{os.getpid()}-{int(time.time()*1000)}"
            probe.write_text("ok")
            probe.unlink()
        except OSError:
            return False
        return True

    def _resolve_inbox_path(self, ident: str) -> Path | None:
        """Return the inbox JSON path for ``ident`` (matched by filename
        stem first, then by ``id`` field if no filename match)."""
        if not self._inbox.exists():
            return None
        # Filename-stem match — fast path.
        direct = self._inbox / f"{ident}.json"
        if direct.exists():
            return direct
        # Scan + match by id field.
        for path in self._inbox.glob("*.json"):
            card = self._load_one(path)
            if card is not None and card.id == ident:
                return path
        return None

    def _load_one(self, path: Path) -> TraceCard | None:
        """Read one JSON file and reconstruct a TraceCard. Returns None
        on any error so callers can skip + continue."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            _log.debug(
                "social-traces: skipping unreadable trace at %s", path,
                exc_info=True,
            )
            return None
        try:
            return trace_card_from_dict(raw)
        except (KeyError, TypeError, ValueError):
            _log.warning(
                "social-traces: skipping malformed trace at %s", path,
                exc_info=True,
            )
            return None


__all__ = [
    "INBOX_DIRNAME",
    "LocalFileTraceNetworkClient",
    "OUTBOX_DIRNAME",
    "score_trace",
    "trace_card_from_dict",
    "trace_card_to_dict",
]
