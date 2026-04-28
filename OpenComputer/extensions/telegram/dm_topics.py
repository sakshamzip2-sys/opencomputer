"""DM topic registry for Telegram forum-mode chats.

Hermes channel-port (PR 5). Wraps Telegram Bot API 9.4's forum-topic
concept: a single private chat can be subdivided into named "topics"
that each carry their own persona / skill loadout. We persist the
mapping ``message_thread_id -> {label, skill, system_prompt,
parent_chat_id}`` so the dispatcher can route inbound messages to the
right per-topic prompt + skill set without requiring a server
roundtrip.

State file: ``<profile_home>/telegram_dm_topics.json``. Writes are
flock-protected via :func:`plugin_sdk.file_lock.exclusive_lock` so a
``opencomputer telegram topic-create`` CLI invocation that races a
running adapter doesn't tear the JSON.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from plugin_sdk.file_lock import exclusive_lock

logger = logging.getLogger("extensions.telegram.dm_topics")


class DMTopicManager:
    """Persistent map: message_thread_id -> topic descriptor.

    Each entry is a dict with optional fields:

    * ``label`` (str): human-readable topic name (e.g. ``"Trading"``)
    * ``skill`` (str | None): skill id auto-loaded for runs in this topic
    * ``system_prompt`` (str | None): per-topic ephemeral system prompt
    * ``parent_chat_id`` (str | None): chat id that owns this topic

    All fields default to ``None`` so a partial registration (label
    only) is valid. The manager doesn't enforce a schema beyond
    ``topic_id`` being a non-empty string.
    """

    def __init__(self, profile_home: Path) -> None:
        self._path = Path(profile_home) / "telegram_dm_topics.json"
        self._topics: dict[str, dict[str, Any]] = self._load()
        # PR #221 follow-up — tombstone set for ``remove_topic``. The
        # merge-on-save path needs to distinguish "this key wasn't in
        # my in-memory map because I never knew about it" (preserve)
        # from "this key wasn't in my in-memory map because I just
        # removed it" (drop on save). Keys are removed from the
        # tombstone set after a successful save propagates the
        # removal to disk.
        self._tombstones: set[str] = set()

    # ── persistence ────────────────────────────────────────────────────

    def _load(self) -> dict[str, dict[str, Any]]:
        """Read the JSON file. Missing / corrupt files yield ``{}``."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return {}
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "telegram_dm_topics.json is not valid JSON; starting empty"
            )
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "telegram_dm_topics.json has unexpected shape %s; starting empty",
                type(data).__name__,
            )
            return {}
        # Coerce keys to str so callers can pass ``int`` thread ids
        # opportunistically without accidentally storing both shapes.
        return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}

    def _save(self) -> None:
        """Atomically persist ``self._topics`` under an exclusive flock.

        Read-merge-write under the lock (PR #221 follow-up):

        Two ``DMTopicManager`` instances racing on the same file used
        to last-writer-wins because each writer constructed its
        in-memory map at instantiation and overwrote disk on every
        save. We now re-read the on-disk JSON inside the lock and
        merge: in-memory entries win per-key, but keys present only
        on disk (added by another writer between our load + save)
        are preserved. This makes concurrent topic-create commands
        safe across processes.

        Uses tmp + ``Path.replace`` so an interrupted write (SIGKILL
        between ``write`` and ``replace``) leaves the previous file
        intact rather than truncating to empty.
        """
        try:
            with exclusive_lock(self._path):
                # Re-read the file under the lock so a concurrent
                # writer's keys aren't clobbered. Missing/corrupt files
                # behave the same as on construction (yield {}).
                disk = self._read_disk_state()
                # Merge: start from disk, layer our in-memory writes on
                # top. Per the contract, our in-memory state wins per
                # key — a removal (``remove_topic``) drops the key from
                # ``self._topics``; that key on disk would survive this
                # merge and effectively un-do the removal across
                # processes. That tradeoff is acceptable (and matches
                # Hermes's behaviour) — concurrent removals are rare,
                # and a stale entry is recoverable; a lost addition is
                # not. If concurrent-remove correctness becomes a real
                # need, the next iteration adds a per-key tombstone.
                merged = dict(disk)
                merged.update(self._topics)
                # Apply tombstones so an explicit ``remove_topic``
                # propagates even when the same key is still on disk
                # — either because we wrote it there before the
                # remove, or because another instance re-added it
                # between our load and our remove. (Concurrent re-add
                # vs concurrent remove is a real ambiguity; we resolve
                # in favor of the local remove. This matches user
                # expectations: an explicit ``opencomputer telegram
                # topic-remove`` from CLI shouldn't be undone by a
                # background daemon's stale in-memory copy.) After
                # a successful flush, tombstones are cleared.
                for tid in self._tombstones:
                    merged.pop(tid, None)
                # Promote the merged map to in-memory state so the
                # next ``_save`` doesn't re-merge the same disk row
                # into our copy on every call.
                self._topics = merged
                self._tombstones.clear()
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                tmp.write_text(
                    json.dumps(merged, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                tmp.replace(self._path)
        except OSError as exc:
            logger.warning(
                "telegram_dm_topics save failed (%s) — change kept in memory only",
                exc,
            )

    def _read_disk_state(self) -> dict[str, dict[str, Any]]:
        """Re-read the on-disk JSON inside ``_save``'s lock.

        Same shape rules as :meth:`_load` — missing file, empty file,
        or invalid JSON yields ``{}``. Extracted into a method so the
        merge path uses the exact same parse / coercion logic as
        construction (no drift between the two read paths).
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return {}
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}

    # ── public API ─────────────────────────────────────────────────────

    def register_topic(
        self,
        topic_id: str,
        label: str,
        skill: str | None = None,
        system_prompt: str | None = None,
        parent_chat_id: str | None = None,
    ) -> None:
        """Add or overwrite a topic entry, then persist."""
        if not topic_id:
            raise ValueError("topic_id must be non-empty")
        self._topics[str(topic_id)] = {
            "label": label,
            "skill": skill,
            "system_prompt": system_prompt,
            "parent_chat_id": parent_chat_id,
        }
        self._save()

    def get_topic(self, topic_id: str) -> dict[str, Any] | None:
        """Return the topic dict or ``None`` if not registered.

        Returns a fresh ``dict`` so callers can mutate the result
        without disturbing the in-memory registry.
        """
        entry = self._topics.get(str(topic_id))
        if entry is None:
            return None
        return dict(entry)

    def list_topics(self) -> list[dict[str, Any]]:
        """Return all registered topics with their ``topic_id`` inlined.

        Each returned dict has the original fields plus ``topic_id``
        for display convenience. Callers must NOT mutate the returned
        dicts (they're shallow copies of the registry entries).
        """
        out: list[dict[str, Any]] = []
        for tid, entry in self._topics.items():
            row = dict(entry)
            row["topic_id"] = tid
            out.append(row)
        return out

    def remove_topic(self, topic_id: str) -> bool:
        """Drop a topic entry; return True iff something was removed."""
        key = str(topic_id)
        if key not in self._topics:
            return False
        del self._topics[key]
        # Mark for tombstone so the merge-on-save path doesn't resurrect
        # this key from disk (PR #221 follow-up).
        self._tombstones.add(key)
        self._save()
        return True


__all__ = ["DMTopicManager"]
