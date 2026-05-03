"""Adapter run trace recording.

Each run, when ``--trace on`` (or programmatic equivalent) is set, gets
its own directory at ``~/.opencomputer/<profile>/traces/<adapter>-<ts>/``
containing:

  - ``summary.md``  — agent-readable: which steps ran, network calls
                      observed, errors if any.
  - ``events.jsonl`` — ordered events (fetch / fetch_in_page / evaluate
                      / navigate / error). One JSON object per line.

This is the foundation for v0.5's autofix flow (see DEFERRED.md §A) —
that's why we record richly here even though no consumer yet reads
the artifacts in v0.4.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AdapterTrace:
    """In-memory + on-disk record of one adapter run.

    Construct via ``AdapterTrace.start(profile_home, spec)``; events
    are appended via the ``record_*`` methods; ``finish()`` writes
    ``summary.md``.
    """

    root: Path
    started_at: float
    spec_name: str
    events: list[dict[str, Any]] = field(default_factory=list)
    finished_at: float | None = None
    error: str | None = None

    @classmethod
    def start(cls, profile_home: Path, spec_tool_name: str) -> AdapterTrace:
        ts = time.strftime("%Y%m%d-%H%M%S")
        root = Path(profile_home) / "traces" / f"{spec_tool_name}-{ts}"
        root.mkdir(parents=True, exist_ok=True)
        return cls(root=root, started_at=time.time(), spec_name=spec_tool_name)

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.root / "summary.md"

    def _emit(self, kind: str, **fields: Any) -> None:
        evt = {"t": time.time() - self.started_at, "kind": kind, **fields}
        self.events.append(evt)
        try:
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(evt, default=str) + "\n")
        except OSError:
            pass

    def record_fetch(self, *, url: str, method: str, status: int) -> None:
        self._emit("fetch", url=url, method=method, status=status)

    def record_fetch_in_page(
        self, *, url: str, method: str, error: int | None = None
    ) -> None:
        self._emit("fetch_in_page", url=url, method=method, error=error)

    def record_evaluate(self, *, expression: str) -> None:
        self._emit("evaluate", expression=expression[:200])

    def record_navigate(self, *, url: str) -> None:
        self._emit("navigate", url=url)

    def record_error(self, *, message: str) -> None:
        self.error = message
        self._emit("error", message=message)

    def finish(self) -> None:
        self.finished_at = time.time()
        elapsed = self.finished_at - self.started_at
        lines = [
            f"# Adapter trace: {self.spec_name}",
            "",
            f"- Started:  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started_at))}",
            f"- Elapsed:  {elapsed:.2f}s",
            f"- Events:   {len(self.events)}",
            f"- Status:   {'ERROR' if self.error else 'OK'}",
        ]
        if self.error:
            lines += ["", "## Error", "", "```", self.error, "```"]
        if self.events:
            lines += ["", "## Events", ""]
            for evt in self.events:
                kind = evt.get("kind", "?")
                summary = ", ".join(
                    f"{k}={v!r}" for k, v in evt.items() if k not in ("t", "kind")
                )
                lines.append(f"- t+{evt.get('t', 0):.2f}s  {kind}: {summary}")
        try:
            self.summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass


__all__ = ["AdapterTrace"]
