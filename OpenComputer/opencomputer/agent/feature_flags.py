"""Phase 2 v0: persistent feature flags.

Lives at ``~/.opencomputer/<profile>/feature_flags.json``. NOT
``runtime_flags`` (which is in-memory and evaporates on restart) — this
substrate persists across process bounces.

Used for the policy-engine kill switch and tunable thresholds. Defaults
match the spec; user-edited values override on read.

Atomic writes via temp-file + rename. JSON parse failures fall back to
defaults (so a corrupt flag file never bricks the process).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


DEFAULT_POLICY_FLAGS: dict[str, Any] = {
    "enabled": True,
    "auto_approve_after_n_safe_decisions": 10,
    "daily_change_budget": 3,
    "min_eligible_turns_for_revert": 10,
    "revert_threshold_sigma": 1.0,
    "decay_factor_per_day": 0.95,
    "minimum_deviation_threshold": 0.10,
    # v0.5 — Task A: digest collapses per-event Telegram pings into a
    # single daily DM at digest_hour_local. Set digest_mode=False to
    # keep per-event pings (v0 behaviour).
    "digest_mode": True,
    "digest_hour_local": 9,
}

#: v0.5 — Task D: data retention defaults (top-level ``data_retention``
#: key, NOT nested under policy_engine).
DEFAULT_DATA_RETENTION: dict[str, Any] = {
    "turn_outcomes_days": 90,
}


class FeatureFlags:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def read_all(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"policy_engine": dict(DEFAULT_POLICY_FLAGS)}
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            _logger.warning(
                "feature_flags read failed: %s; returning defaults", e
            )
            return {"policy_engine": dict(DEFAULT_POLICY_FLAGS)}
        # Ensure policy_engine key exists with defaults filled in
        if "policy_engine" not in data:
            data["policy_engine"] = dict(DEFAULT_POLICY_FLAGS)
        else:
            for k, v in DEFAULT_POLICY_FLAGS.items():
                data["policy_engine"].setdefault(k, v)
        return data

    def read(self, dotted_key: str, default: Any = None) -> Any:
        flags = self.read_all()
        node: Any = flags
        parts = dotted_key.split(".")
        for p in parts:
            if not isinstance(node, dict) or p not in node:
                # Fall back to spec defaults for known top-level keys
                if dotted_key.startswith("policy_engine."):
                    leaf = parts[-1]
                    return DEFAULT_POLICY_FLAGS.get(leaf, default)
                if dotted_key.startswith("data_retention."):
                    leaf = parts[-1]
                    return DEFAULT_DATA_RETENTION.get(leaf, default)
                return default
            node = node[p]
        return node

    def write(self, dotted_key: str, value: Any) -> None:
        flags = self.read_all()
        node = flags
        parts = dotted_key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
        self._atomic_write(flags)
        _logger.info("feature_flag write: %s = %r", dotted_key, value)

    def _atomic_write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write to temp in same dir then rename (atomic on POSIX/NTFS).
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(self._path.parent),
            delete=False,
            prefix=".feature_flags.",
            suffix=".tmp",
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, self._path)
