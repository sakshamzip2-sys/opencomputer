"""Cross-session message mirroring.

PR-2 Task B6 of the messaging-gateway parity plan. When a message is
sent to a platform (via cron, ``messages_send``, or
:class:`DeliveryRouter`), :func:`mirror_to_session` appends a
``mirror=True`` entry to the recipient session's transcript so the
receiving-side agent has context for what was sent on its behalf.

OpenComputer derives session ids deterministically from
``(platform, chat_id [, thread_hint])`` via
:func:`opencomputer.gateway.dispatch.session_id_for`, so we don't need
the JSON sessions index Hermes maintains; the candidate session id is
computable from the inputs and we verify with
:meth:`SessionDB.get_session`.

Best-effort: every IO operation is wrapped in try/except. The function
returns ``True`` on success, ``False`` on miss / ambiguity / IO error.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from plugin_sdk.core import Message

logger = logging.getLogger("opencomputer.gateway.mirror")


def _profile_home() -> Path:
    """Resolve the active profile's home dir, the same way the CLI does.

    Lazily imports ``opencomputer.agent.config._home`` so this module
    stays cheap to import in test contexts that don't construct an
    agent.
    """
    try:
        from opencomputer.agent.config import _home
        return _home()
    except Exception:  # noqa: BLE001 — fall back to env
        import os
        base = Path(
            os.environ.get(
                "OPENCOMPUTER_HOME",
                str(Path.home() / ".opencomputer"),
            ),
        )
        base.mkdir(parents=True, exist_ok=True)
        return base


def _candidate_thread_hints(
    thread_id: str | None,
    user_id: str | None,
) -> list[str | None]:
    """Build the ordered list of thread_hints to probe.

    Priority (first match wins):
      1. ``thread_id`` if provided (caller knows the thread)
      2. ``user:{user_id}`` if provided (group-chat user-namespaced)
      3. ``None`` (the base chat session)
    """
    hints: list[str | None] = []
    if thread_id:
        hints.append(thread_id)
    if user_id:
        hints.append(f"user:{user_id}")
    hints.append(None)
    return hints


def mirror_to_session(
    platform: str,
    chat_id: str,
    message_text: str,
    source_label: str = "cli",
    thread_id: str | None = None,
    user_id: str | None = None,
) -> bool:
    """Append a delivery-mirror message to the matching session.

    Finds the deterministic session id for the given
    ``(platform, chat_id [, thread_hint])`` triple, checks that a
    ``sessions`` row exists, and then appends a JSONL line plus a
    SQLite ``messages`` row with ``role="assistant"``.

    Returns ``True`` if mirrored, ``False`` if no matching session,
    ambiguous matches, or an IO error.
    """
    try:
        # Local imports — keep delivery import path fast and avoid
        # circular issues if SessionDB is constructed in test fixtures
        # before this module is imported.
        from opencomputer.agent.config import default_config
        from opencomputer.agent.state import SessionDB
        from opencomputer.gateway.dispatch import session_id_for

        try:
            cfg = default_config()
            db = SessionDB(cfg.session.db_path)
        except Exception:  # noqa: BLE001
            db = SessionDB(_profile_home() / "sessions.db")

        candidate_session_id = _resolve_session_id(
            db, platform, str(chat_id), thread_id, user_id, session_id_for,
        )
        if candidate_session_id is None:
            logger.debug(
                "mirror: no session for %s:%s thread=%r user=%r",
                platform, chat_id, thread_id, user_id,
            )
            return False

        mirror_payload = {
            "role": "assistant",
            "content": message_text,
            "timestamp": datetime.now().isoformat(),
            "mirror": True,
            "mirror_source": source_label,
        }

        jsonl_ok = _append_jsonl(candidate_session_id, mirror_payload)
        sqlite_ok = _append_sqlite(db, candidate_session_id, message_text)

        # Treat success as "at least one of the writes landed." A failed
        # JSONL write while the SQLite row succeeded is still useful
        # context for the recipient agent.
        return jsonl_ok or sqlite_ok
    except Exception as e:  # noqa: BLE001
        logger.debug(
            "mirror: top-level swallow for %s:%s thread=%r user=%r: %s",
            platform, chat_id, thread_id, user_id, e,
        )
        return False


def _resolve_session_id(
    db,
    platform: str,
    chat_id: str,
    thread_id: str | None,
    user_id: str | None,
    session_id_for_fn,
) -> str | None:
    """Pick the right session id, honoring thread_id / user_id semantics.

    Returns ``None`` when:
    * No candidate hint produces an existing session, OR
    * No hint was given but multiple thread variants exist for the chat
      and no base-chat session does (we don't guess which thread to
      mirror into).
    """
    hints = _candidate_thread_hints(thread_id, user_id)
    for hint in hints:
        sid = session_id_for_fn(platform, chat_id, hint)
        try:
            row = db.get_session(sid)
        except Exception:  # noqa: BLE001
            row = None
        if row is not None:
            return sid

    # Base/explicit candidates didn't exist. If the caller was strict
    # (provided thread_id or user_id) we already gave them a clear miss.
    # If they were ambiguous (no hint), check whether at least one
    # thread variant of this chat exists — if multiple, return None so
    # we don't silently mirror into an arbitrary thread.
    if thread_id is None and user_id is None:
        try:
            rows = db.list_sessions(limit=200)
        except Exception:  # noqa: BLE001
            rows = []
        # We cannot reverse the hash; instead infer "matches" by
        # platform plus the deterministic id matching one of the
        # candidate hints we'd ever generate. Heuristic: count how many
        # of those rows share this platform — if 2+ and none equal the
        # bare-chat sid, return None.
        bare_sid = session_id_for_fn(platform, chat_id)
        same_plat_rows = [
            r for r in rows
            if (r.get("platform") or "").lower() == platform.lower()
        ]
        if len(same_plat_rows) >= 2 and not any(
            r.get("id") == bare_sid for r in same_plat_rows
        ):
            return None
    return None


def _append_jsonl(session_id: str, payload: dict) -> bool:
    """Append the mirror payload to ``<profile>/sessions/<sid>.jsonl``."""
    try:
        path = _profile_home() / "sessions" / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("mirror: jsonl write failed for %s: %s", session_id, e)
        return False


def _append_sqlite(db, session_id: str, message_text: str) -> bool:
    """Append a SQLite ``messages`` row tagged role=assistant.

    OpenComputer's ``messages`` table has no ``mirror`` column, so we
    mark provenance only in the JSONL payload. The SQLite row stays a
    plain assistant message — sufficient for ``oc sessions show`` to
    surface the cross-session context.
    """
    try:
        db.append_message(
            session_id,
            Message(role="assistant", content=message_text),
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("mirror: sqlite write failed for %s: %s", session_id, e)
        return False


__all__ = ["mirror_to_session"]
