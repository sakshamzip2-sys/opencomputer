"""Persistence — translate Layer 0/1/2 outputs into F4 user-model edges.

Mirrors :class:`opencomputer.user_model.importer.MotifImporter` shape;
each writer is idempotent via ``UserModelStore.upsert_node``. Node
persistence here does not tag edge provenance — the F4↔Honcho
cycle-prevention path applies only when edges are inserted (e.g., by
the motif importer). MVP persistence writes Identity / Goal / Preference
/ Attribute nodes only.

The Layer 2 writers below are intentionally aggregating, not
per-row: dumping every recent-file path or every browser visit into
its own node would blow up the graph and leak content. Instead each
writer collapses raw rows into ``top-N by frequency`` patterns
(``active_dir: ...``, ``frequent_domain: ...``) before upsert. The
``NodeKind`` enum is closed (identity / attribute / relationship /
goal / preference); we lean on ``attribute`` for everything observed-
about-the-user since adding a kind is a breaking SDK change.
"""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

from opencomputer.profile_bootstrap.browser_history import BrowserVisitSummary
from opencomputer.profile_bootstrap.calendar_reader import CalendarEventSummary
from opencomputer.profile_bootstrap.identity_reflex import IdentityFacts
from opencomputer.profile_bootstrap.recent_scan import (
    GitCommitSummary,
    RecentFileSummary,
)
from opencomputer.user_model.store import UserModelStore


def _evidence_to_confidence(count: int) -> float:
    """Map evidence count to a [0.5, 0.95] confidence band.

    Single observation → 0.5 (default node prior). Saturates near 0.95
    by ~50 observations so we never claim certainty from frequency
    alone — explicit user statements (Layer 1) keep their 1.0 lead.
    Logarithmic shape so going from 1 to 5 occurrences moves
    confidence more than going from 45 to 50.
    """
    if count <= 1:
        return 0.5
    # log-shape: clamps to ~0.95 around count=50
    import math
    raw = 0.5 + 0.45 * (math.log1p(count - 1) / math.log1p(49))
    return max(0.5, min(0.95, raw))


def write_identity_to_graph(
    facts: IdentityFacts,
    *,
    store: UserModelStore | None = None,
) -> int:
    """Persist :class:`IdentityFacts` as Identity nodes.

    Returns the number of nodes written/upserted (excluding edges).
    Idempotent — repeated calls re-upsert without duplicating.
    """
    s = store if store is not None else UserModelStore()
    written = 0
    if facts.name and facts.name.strip():
        s.upsert_node(kind="identity", value=f"name: {facts.name.strip()}", confidence=1.0)
        written += 1
    for email in facts.emails:
        e = email.strip()
        if not e:
            continue
        s.upsert_node(kind="identity", value=f"email: {e}", confidence=1.0)
        written += 1
    for phone in facts.phones:
        p = phone.strip()
        if not p:
            continue
        s.upsert_node(kind="identity", value=f"phone: {p}", confidence=1.0)
        written += 1
    if facts.github_handle and facts.github_handle.strip():
        s.upsert_node(kind="identity", value=f"github: {facts.github_handle.strip()}", confidence=1.0)
        written += 1
    if facts.city and facts.city.strip():
        s.upsert_node(kind="identity", value=f"city: {facts.city.strip()}", confidence=1.0)
        written += 1
    return written


def write_interview_answers_to_graph(
    answers: dict[str, str],
    *,
    store: UserModelStore | None = None,
) -> int:
    """Persist Layer 1 quick-interview answers as Preference + Goal nodes.

    Each answer is stored as a node with a question-keyed prefix so the
    raw answer is recoverable. Confidence is 1.0 (user-explicit).
    Returns the number of nodes upserted.
    """
    s = store if store is not None else UserModelStore()
    # Quick-interview answer keys → F4 NodeKind. NodeKind is a closed
    # literal (identity/attribute/relationship/goal/preference); we map
    # `current_concerns` to "goal" rather than adding a new "concern" kind
    # because adding a NodeKind member is a breaking SDK change.
    kind_map = {
        "current_focus": "goal",
        "current_concerns": "goal",
        "tone_preference": "preference",
        "do_not": "preference",
        "context": "attribute",
    }
    written = 0
    for question_key, answer in answers.items():
        a = (answer or "").strip()
        if not a:
            continue
        kind = kind_map.get(question_key, "attribute")
        s.upsert_node(
            kind=kind,
            value=f"{question_key}: {a}",
            confidence=1.0,
        )
        written += 1
    return written


# ─── Layer 2 writers (2026-04-28) ────────────────────────────────────


def _project_root_for_path(path: str, *, home: str) -> str | None:
    """Collapse a file path to its project-root signature.

    The signature is the **two segments above** the user's home dir
    (so ``~/Vscode/claude/OpenComputer/foo/bar.py`` collapses to
    ``Vscode/claude/OpenComputer``). This keeps individual filenames
    out of the graph while still preserving "you work in three repos"
    structure.

    Returns None if the path doesn't sit under home — those are skipped
    so we don't accidentally publish system-path or temp-dir noise.
    """
    if not path or not home:
        return None
    if not path.startswith(home.rstrip("/")):
        return None
    rel = path[len(home.rstrip("/")):].lstrip("/")
    parts = rel.split("/")
    if len(parts) < 2:
        return None
    # Take up to the first 3 segments so single-level home dirs like
    # ``Vscode/claude`` stay distinct from deep ones like
    # ``Vscode/claude/OpenComputer``.
    return "/".join(parts[: min(3, len(parts) - 1)])


def write_recent_files_to_graph(
    files: list[RecentFileSummary],
    *,
    home: str | None = None,
    top_n: int = 8,
    store: UserModelStore | None = None,
) -> int:
    """Persist Layer 2 recent-file scan as ``attribute`` nodes.

    Aggregates by project-root signature (~/Vscode/claude/OpenComputer
    is one node, not 1000 nodes). Top-N most-active dirs upserted with
    confidence proportional to file count. Returns the number of nodes
    upserted.
    """
    s = store if store is not None else UserModelStore()
    if not files:
        return 0
    if home is None:
        from pathlib import Path
        home = str(Path.home())
    counts: Counter[str] = Counter()
    for f in files:
        sig = _project_root_for_path(f.path, home=home)
        if sig:
            counts[sig] += 1
    written = 0
    for sig, count in counts.most_common(top_n):
        s.upsert_node(
            kind="attribute",
            value=f"active_dir: {sig}",
            confidence=_evidence_to_confidence(count),
            metadata={"file_count_7d": count},
        )
        written += 1
    return written


def write_git_log_to_graph(
    commits: list[GitCommitSummary],
    *,
    top_n_repos: int = 10,
    store: UserModelStore | None = None,
) -> int:
    """Persist Layer 2 git-log scan as ``attribute`` + ``identity`` nodes.

    Two signals extracted:

    * Repo paths — top N by commit count → ``attribute`` nodes
      (``"works_on_repo: /path"``).
    * Author emails — distinct authors observed → ``identity`` nodes
      (deduped against any pre-existing identity rows by upsert).

    Returns total node count upserted.
    """
    s = store if store is not None else UserModelStore()
    if not commits:
        return 0
    repo_counts: Counter[str] = Counter()
    emails: set[str] = set()
    for c in commits:
        if c.repo_path:
            repo_counts[c.repo_path] += 1
        if c.author_email and c.author_email.strip():
            emails.add(c.author_email.strip())
    written = 0
    for repo, count in repo_counts.most_common(top_n_repos):
        s.upsert_node(
            kind="attribute",
            value=f"works_on_repo: {repo}",
            confidence=_evidence_to_confidence(count),
            metadata={"commits_7d": count},
        )
        written += 1
    for email in emails:
        s.upsert_node(
            kind="identity",
            value=f"git_author_email: {email}",
            confidence=0.95,  # email-in-commit is strong evidence
        )
        written += 1
    return written


_DOMAIN_RE = re.compile(r"^(?:www\.|m\.)")


def _normalize_domain(url: str) -> str | None:
    """Strip protocol + leading ``www./m.`` so example.com == www.example.com."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return None
    host = (parsed.netloc or "").lower()
    if not host:
        return None
    return _DOMAIN_RE.sub("", host)


def write_browser_history_to_graph(
    visits: list[BrowserVisitSummary],
    *,
    top_n: int = 15,
    min_visits: int = 2,
    store: UserModelStore | None = None,
) -> int:
    """Persist Layer 2 browser visits as ``attribute`` nodes per domain.

    Aggregates by domain (host), drops single-visit rows so we don't
    capture transient ad-redirects, and writes top-N as ``attribute``
    nodes (``"frequent_domain: example.com"``). Returns the number of
    nodes upserted.

    Privacy: page titles and full URLs are intentionally NOT persisted
    — only domain frequency. The raw visits stay in their browser
    history file; the graph holds only the aggregate signal.
    """
    s = store if store is not None else UserModelStore()
    if not visits:
        return 0
    domain_counts: Counter[str] = Counter()
    for v in visits:
        d = _normalize_domain(v.url)
        if d:
            domain_counts[d] += 1
    written = 0
    for domain, count in domain_counts.most_common(top_n):
        if count < min_visits:
            continue
        s.upsert_node(
            kind="attribute",
            value=f"frequent_domain: {domain}",
            confidence=_evidence_to_confidence(count),
            metadata={"visits_7d": count},
        )
        written += 1
    return written


def write_calendar_to_graph(
    events: list[CalendarEventSummary],
    *,
    store: UserModelStore | None = None,
) -> int:
    """Persist Layer 2 calendar events as ``attribute`` nodes.

    Each upcoming event becomes one node tagged with ``upcoming: <title>``.
    The start time goes into ``metadata`` so consumers can rank by
    proximity without parsing the value string. Title-empty events
    (busy blocks, declined invites) are skipped. Returns node count.
    """
    s = store if store is not None else UserModelStore()
    if not events:
        return 0
    written = 0
    for ev in events:
        title = (ev.title or "").strip()
        if not title:
            continue
        s.upsert_node(
            kind="attribute",
            value=f"upcoming: {title}",
            confidence=0.85,  # explicit calendar entry is strong evidence
            metadata={
                "start": ev.start,
                "end": ev.end,
                "calendar": ev.calendar_name,
            },
        )
        written += 1
    return written
