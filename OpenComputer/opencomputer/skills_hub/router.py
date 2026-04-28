"""SkillSourceRouter — fan search/inspect/fetch across multiple sources.

Failures from one source must not block others. Logged + swallowed.
"""

from __future__ import annotations

import logging

from plugin_sdk.skill_source import SkillBundle, SkillMeta, SkillSource

_log = logging.getLogger(__name__)


class SkillSourceRouter:
    def __init__(self, sources: list[SkillSource]) -> None:
        self._sources = list(sources)
        self._by_name = {s.name: s for s in self._sources}

    def search(
        self,
        query: str,
        limit: int = 10,
        source_filter: str | None = None,
    ) -> list[SkillMeta]:
        out: list[SkillMeta] = []
        for src in self._sources:
            if source_filter and src.name != source_filter:
                continue
            try:
                out.extend(src.search(query, limit=limit))
            except Exception as e:  # noqa: BLE001
                _log.warning("source %r raised during search: %s", src.name, e)
        return out[:limit] if limit else out

    def fetch(self, identifier: str) -> SkillBundle | None:
        if "/" not in identifier:
            return None
        source_name, _ = identifier.split("/", 1)
        src = self._by_name.get(source_name)
        if src is None:
            return None
        try:
            return src.fetch(identifier)
        except Exception as e:  # noqa: BLE001
            _log.warning("source %r raised during fetch(%s): %s", src.name, identifier, e)
            return None

    def inspect(self, identifier: str) -> SkillMeta | None:
        if "/" not in identifier:
            return None
        source_name, _ = identifier.split("/", 1)
        src = self._by_name.get(source_name)
        if src is None:
            return None
        try:
            return src.inspect(identifier)
        except Exception as e:  # noqa: BLE001
            _log.warning("source %r raised during inspect(%s): %s", src.name, identifier, e)
            return None

    def list_sources(self) -> list[str]:
        return [s.name for s in self._sources]
