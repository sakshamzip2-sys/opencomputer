"""Handoff inbox — atomic on-disk handoff exchange between profiles.

Layout:
    ~/.opencomputer/<profile-home>/inbox/
        handoff_<UTC>_<from-profile>_<rand>.md      <- pending
    ~/.opencomputer/<profile-home>/inbox/processed/
        handoff_<UTC>_<from-profile>_<rand>.md      <- after consumption

File format (per handoff_*.md):
    ---
    protocol_version: handoff-v2
    source_profile: default
    target_profile: stocks
    generated_at: 2026-05-13T14:32:01Z
    source_session_id: 01HXX...
    trigger: auto
    classifier_confidence: 0.87
    classifier_reason: state-query / greeting detected
    ---
    <markdown body>

Concurrency: writes are atomic via tempfile + ``os.replace`` so a partial
write is never visible to a reader. Readers sort by filename (UTC prefix
guarantees chronological order). The ``processed/`` dir is the archive —
files there are not re-injected.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from opencomputer.agent.handoff.models import HandoffDocument, HandoffMetadata
from opencomputer.agent.handoff.protocol_v2 import PROTOCOL_VERSION

_log = logging.getLogger("opencomputer.agent.handoff.inbox")

# Filename shape: handoff_<UTC>_<from>_<rand6>.md
# UTC is 20 chars ``20260513T143201Z`` (sortable); ``rand`` is 6 hex chars
# to avoid collisions on same-second writes from parallel sessions.
_FILENAME_PATTERN = re.compile(
    r"^handoff_(?P<ts>\d{8}T\d{6}Z)_(?P<from>[A-Za-z0-9_-]+)_[0-9a-f]{6}\.md$"
)

#: Cap on pending-handoff count read in one shot. Anything beyond this is
#: ignored on read and surfaces a WARN — protects against runaway inbox
#: growth (e.g. user disabled auto-archive then auto-swap fired 1000 times).
_MAX_PENDING_READ: int = 50


class InboxIOError(RuntimeError):
    """Raised when the inbox is unwritable or unreadable for a structural
    reason (permissions, disk full, IO error). Callers MUST fail-closed —
    do not swap if the handoff cannot land."""


class HandoffParseError(RuntimeError):
    """A handoff file on disk is malformed (bad frontmatter, unknown
    protocol version). The reader logs WARN and skips the file."""


class HandoffInbox:
    """Per-profile inbox.

    Construct with the TARGET profile's home directory (the directory that
    contains ``inbox/``). The class never resolves profile names — the
    caller maps profile_id → profile_home and passes the Path.
    """

    def __init__(self, profile_home: Path) -> None:
        if not isinstance(profile_home, Path):
            raise TypeError(
                f"profile_home must be Path, got {type(profile_home).__name__}"
            )
        self._home = profile_home

    @property
    def inbox_dir(self) -> Path:
        return self._home / "inbox"

    @property
    def processed_dir(self) -> Path:
        return self._home / "inbox" / "processed"

    # ─── write side ───────────────────────────────────────────────────

    def write(self, doc: HandoffDocument) -> Path:
        """Atomically write ``doc`` to the inbox; return the final Path.

        Validates the document, ensures the directory exists, and uses
        tempfile + ``os.replace`` so partial writes never surface to a
        reader. Raises :class:`InboxIOError` on any IO failure — callers
        fail-closed (do NOT swap if write fails).
        """
        if not isinstance(doc, HandoffDocument):
            raise TypeError(f"expected HandoffDocument, got {type(doc).__name__}")
        if not doc.body.strip():
            raise ValueError("handoff body is empty — refusing to write")
        if doc.metadata.protocol_version != PROTOCOL_VERSION:
            raise ValueError(
                f"protocol_version must be {PROTOCOL_VERSION!r}, "
                f"got {doc.metadata.protocol_version!r}"
            )

        try:
            self.inbox_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise InboxIOError(
                f"cannot create inbox dir {self.inbox_dir}: {e}"
            ) from e

        filename = _build_filename(doc.metadata)
        final_path = self.inbox_dir / filename
        rendered = _render_file_body(doc)

        tmp_path: str | None = None
        try:
            # NB: explicit delete=False + manual close+replace is the
            # canonical atomic-write idiom; a ``with`` block would unlink
            # the file before we can rename it. Disabled SIM115 because
            # the lifetime IS managed (close-replace below).
            tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
                mode="w",
                encoding="utf-8",
                dir=str(self.inbox_dir),
                prefix=".handoff_tmp_",
                suffix=".md",
                delete=False,
            )
            tmp_path = tmp.name
            try:
                tmp.write(rendered)
                tmp.flush()
                os.fsync(tmp.fileno())
            finally:
                tmp.close()
            os.replace(tmp_path, final_path)
        except OSError as e:
            # Best-effort temp cleanup — only valid if NamedTemporaryFile
            # successfully created a file.
            if tmp_path is not None:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError:
                    pass
            raise InboxIOError(
                f"cannot write handoff to {final_path}: {e}"
            ) from e

        _log.info(
            "handoff written: %s (source=%s target=%s trigger=%s)",
            final_path.name,
            doc.metadata.source_profile,
            doc.metadata.target_profile,
            doc.metadata.trigger,
        )
        return final_path

    # ─── read side ────────────────────────────────────────────────────

    def list_pending(self) -> list[Path]:
        """Return pending handoff file paths sorted by UTC timestamp asc.

        Files that don't match the canonical filename pattern are skipped
        with a DEBUG log — they're not handoff files (could be a user's
        own README.md, an editor backup, etc.). The ``processed/`` dir is
        never read.
        """
        if not self.inbox_dir.exists():
            return []
        try:
            entries = list(self.inbox_dir.iterdir())
        except OSError as e:
            _log.warning("cannot list inbox dir %s: %s", self.inbox_dir, e)
            return []

        pending: list[Path] = []
        for p in entries:
            if not p.is_file():
                continue
            m = _FILENAME_PATTERN.match(p.name)
            if not m:
                _log.debug("ignoring non-handoff file in inbox: %s", p.name)
                continue
            pending.append(p)

        pending.sort(key=lambda p: p.name)  # UTC-prefix sort = chrono asc
        if len(pending) > _MAX_PENDING_READ:
            _log.warning(
                "inbox %s has %d pending handoffs (cap %d) — older files "
                "ignored; consider clearing inbox/processed/",
                self.inbox_dir, len(pending), _MAX_PENDING_READ,
            )
            pending = pending[-_MAX_PENDING_READ:]
        return pending

    def read(self, path: Path) -> HandoffDocument:
        """Parse one handoff file. Raises :class:`HandoffParseError` on
        malformed input. IO errors propagate as :class:`InboxIOError`."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise InboxIOError(f"cannot read {path}: {e}") from e

        metadata, body = _parse_file_body(text, source_path=path)
        return HandoffDocument(metadata=metadata, body=body, path=path)

    def mark_processed(self, path: Path) -> Path:
        """Move ``path`` to ``processed/``. Returns the new path.

        Idempotent on already-moved files (no-op if path doesn't exist).
        Raises :class:`InboxIOError` on a real IO failure — but callers
        treat that as non-fatal: the handoff content was already injected,
        worst case it gets re-injected on the next turn.
        """
        if not path.exists():
            return self.processed_dir / path.name
        try:
            self.processed_dir.mkdir(parents=True, exist_ok=True)
            dest = self.processed_dir / path.name
            os.replace(path, dest)
            return dest
        except OSError as e:
            raise InboxIOError(
                f"cannot move {path} to processed dir: {e}"
            ) from e

    def read_and_process_all(self) -> list[HandoffDocument]:
        """One-shot: list pending, read each, archive each. Best-effort —
        parse failures are skipped with a WARN, not raised."""
        docs: list[HandoffDocument] = []
        for p in self.list_pending():
            try:
                doc = self.read(p)
            except HandoffParseError as e:
                _log.warning("skipping malformed handoff %s: %s", p, e)
                continue
            except InboxIOError as e:
                _log.warning("cannot read handoff %s: %s", p, e)
                continue
            docs.append(doc)
            try:
                self.mark_processed(p)
            except InboxIOError as e:
                _log.warning(
                    "consumed handoff %s but archive failed: %s — "
                    "next turn may re-inject", p, e,
                )
        return docs


# ─── helpers ──────────────────────────────────────────────────────────


def _utc_stamp(dt: datetime | None = None) -> str:
    """ISO-8601 UTC ``YYYYMMDDTHHMMSSZ`` — sortable, filename-safe."""
    dt = dt or datetime.now(tz=UTC)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _build_filename(meta: HandoffMetadata) -> str:
    ts_compact = _compact_iso(meta.generated_at)
    safe_from = _sanitize_token(meta.source_profile)
    rand = secrets.token_hex(3)
    return f"handoff_{ts_compact}_{safe_from}_{rand}.md"


def _compact_iso(iso: str) -> str:
    """Convert ``2026-05-13T14:32:01Z`` → ``20260513T143201Z`` for filenames."""
    return iso.replace("-", "").replace(":", "")


_TOKEN_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _sanitize_token(s: str) -> str:
    """Filename-safe profile name: letters, digits, underscore, hyphen."""
    out = _TOKEN_SAFE.sub("_", s).strip("_")
    return out or "unknown"


def _render_file_body(doc: HandoffDocument) -> str:
    m = doc.metadata
    fm_lines = [
        "---",
        f"protocol_version: {m.protocol_version}",
        f"source_profile: {m.source_profile}",
        f"target_profile: {m.target_profile}",
        f"generated_at: {m.generated_at}",
        f"source_session_id: {m.source_session_id}",
        f"trigger: {m.trigger}",
    ]
    if m.classifier_confidence is not None:
        fm_lines.append(f"classifier_confidence: {m.classifier_confidence:.3f}")
    if m.classifier_reason:
        safe_reason = m.classifier_reason.replace("\n", " ")[:200]
        fm_lines.append(f"classifier_reason: {safe_reason}")
    fm_lines.append("---")
    return "\n".join(fm_lines) + "\n\n" + doc.body.strip() + "\n"


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<fm>.+?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)


def _parse_file_body(text: str, *, source_path: Path) -> tuple[HandoffMetadata, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise HandoffParseError(
            f"{source_path.name}: no YAML frontmatter delimiters found"
        )
    fm_raw = m.group("fm")
    body = m.group("body").strip()
    if not body:
        raise HandoffParseError(f"{source_path.name}: empty body after frontmatter")

    fields: dict[str, str] = {}
    for line_num, line in enumerate(fm_raw.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise HandoffParseError(
                f"{source_path.name}: malformed frontmatter at line {line_num}: "
                f"{line!r}"
            )
        k, v = line.split(":", 1)
        fields[k.strip()] = v.strip()

    proto = fields.get("protocol_version", "")
    if proto != PROTOCOL_VERSION:
        raise HandoffParseError(
            f"{source_path.name}: unknown protocol_version {proto!r} "
            f"(expected {PROTOCOL_VERSION!r})"
        )

    required = ("source_profile", "target_profile", "generated_at",
                "source_session_id", "trigger")
    missing = [k for k in required if not fields.get(k)]
    if missing:
        raise HandoffParseError(
            f"{source_path.name}: missing required frontmatter fields: {missing}"
        )

    trigger = fields["trigger"]
    if trigger not in ("auto", "manual", "cli"):
        raise HandoffParseError(
            f"{source_path.name}: invalid trigger {trigger!r}"
        )

    confidence_raw = fields.get("classifier_confidence", "").strip()
    confidence: float | None = None
    if confidence_raw:
        try:
            confidence = float(confidence_raw)
        except ValueError:
            raise HandoffParseError(
                f"{source_path.name}: classifier_confidence is not a float: "
                f"{confidence_raw!r}"
            )
        if not (0.0 <= confidence <= 1.0):
            raise HandoffParseError(
                f"{source_path.name}: classifier_confidence out of range "
                f"[0,1]: {confidence}"
            )

    meta = HandoffMetadata(
        protocol_version=PROTOCOL_VERSION,
        source_profile=fields["source_profile"],
        target_profile=fields["target_profile"],
        generated_at=fields["generated_at"],
        source_session_id=fields["source_session_id"],
        trigger=trigger,  # type: ignore[arg-type]  — narrowed above
        classifier_confidence=confidence,
        classifier_reason=fields.get("classifier_reason") or None,
    )
    return meta, body


__all__ = [
    "HandoffInbox",
    "HandoffParseError",
    "InboxIOError",
]
