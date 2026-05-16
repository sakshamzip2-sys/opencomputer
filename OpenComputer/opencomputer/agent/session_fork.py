"""Shared session-fork helper.

Pure helper extracted from ``cli_session.py::session_fork`` so both the
CLI path and the ``/branch`` slash command share one implementation.

Cloning a session means:

1. Create a fresh row in ``sessions`` with a new uuid hex id.
2. Copy the source session's platform / model into the new row.
3. Set the new row's title (caller-supplied, or
   ``"<source title> (fork)"``).
4. Copy every message from source into the new session via the
   batch-append path.

No I/O beyond the ``SessionDB`` handle — keeps the function easy to
test and lets both call sites (CLI Rich output / slash result string)
format the response themselves.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

TITLE_MAX_LEN = 200
"""Same cap as :class:`TitleCommand`. Keep the two call paths consistent."""


class SourceSessionNotFoundError(KeyError):
    """Raised when ``source_id`` doesn't resolve to a session row.

    Subclasses :class:`KeyError` so callers can catch with either type.
    """


@dataclass(frozen=True)
class ForkResult:
    """What a successful fork produces.

    Fields
    ------
    new_session_id:
        The freshly minted session id (uuid hex, no dashes).
    new_title:
        Final title written to the new session.
    messages_copied:
        Count of messages copied from source. ``0`` is legal — a
        brand-new chat with no turns can still be forked.
    """

    new_session_id: str
    new_title: str
    messages_copied: int


def _resolve_new_title(
    source_title: str | None,
    user_title: str | None,
    fallback_title: str | None = None,
) -> str:
    """Pick the title for the forked session.

    Rules, in priority order:

    * If the user passed a non-empty title, use it (truncated to
      ``TITLE_MAX_LEN``).
    * Else if source had a title, use ``"<source title> (fork)"``.
    * Else if the caller supplied a non-empty ``fallback_title``, use it
      (truncated). The dashboard ``/fork`` endpoint passes its
      ``"Fork of <id8>"`` placeholder here.
    * Else default to ``"(fork)"``.

    Whitespace-only input (user title or fallback) is treated as no
    input.
    """
    user_clean = (user_title or "").strip()
    if user_clean:
        return user_clean[:TITLE_MAX_LEN]

    src_clean = (source_title or "").strip()
    if src_clean:
        return f"{src_clean} (fork)"[:TITLE_MAX_LEN]

    fb_clean = (fallback_title or "").strip()
    if fb_clean:
        return fb_clean[:TITLE_MAX_LEN]

    return "(fork)"


def fork_session(
    db: Any,
    source_id: str,
    *,
    title: str | None = None,
    record_parent: bool = False,
    fallback_title: str | None = None,
) -> ForkResult:
    """Clone the message history of ``source_id`` into a new session.

    Parameters
    ----------
    db:
        A ``SessionDB`` handle (duck-typed; tests can pass a fake).
        Must expose ``get_session``, ``get_messages``, ``create_session``,
        and ``append_messages_batch``.
    source_id:
        Session id to clone from. Must exist; raises
        :class:`SourceSessionNotFoundError` if not.
    title:
        Caller-supplied title for the new session. ``None`` / empty
        string falls back to ``"<source title> (fork)"`` (see
        :func:`_resolve_new_title`). Titles longer than
        :data:`TITLE_MAX_LEN` are silently truncated; callers that want
        to *reject* over-length titles instead must validate before
        calling.
    record_parent:
        When ``True``, pass ``parent_session_id=source_id`` to
        ``create_session`` so the resume picker / ``oc sessions tree``
        can group the new session under its source. Defaults to
        ``False`` to preserve the historical ``oc session fork`` CLI
        behaviour, which did not record lineage. The ``/branch`` slash
        opts in (Phase H, 2026-05-11).
    fallback_title:
        Placeholder title used only when there is neither a
        caller-supplied ``title`` nor a source title. ``None`` (the
        default) falls back to the bare ``"(fork)"``. The dashboard
        ``/fork`` endpoint passes ``"Fork of <id8>"`` here so its
        untitled-source forks keep their historical label.

    Returns
    -------
    ForkResult
        Carries the new session id, the resolved title, and how many
        messages were copied.

    Raises
    ------
    SourceSessionNotFoundError
        When ``source_id`` doesn't resolve to a known session.
    """
    src = db.get_session(source_id)
    if src is None:
        raise SourceSessionNotFoundError(source_id)

    msgs = db.get_messages(source_id)

    new_id = uuid.uuid4().hex
    new_title = _resolve_new_title(src.get("title"), title, fallback_title)

    create_kwargs: dict[str, Any] = {
        "platform": src.get("platform", "") or "cli",
        "model": src.get("model", "") or "",
        "title": new_title,
    }
    if record_parent:
        # Phase H (2026-05-11) — lineage propagation for the fork-tree
        # UI. Only the slash command opts in; the CLI keeps the
        # pre-Phase-H behaviour for backwards compatibility.
        create_kwargs["parent_session_id"] = source_id

    db.create_session(new_id, **create_kwargs)

    if msgs:
        db.append_messages_batch(new_id, msgs)

    return ForkResult(
        new_session_id=new_id,
        new_title=new_title,
        messages_copied=len(msgs),
    )


__all__ = [
    "ForkResult",
    "SourceSessionNotFoundError",
    "TITLE_MAX_LEN",
    "fork_session",
]
