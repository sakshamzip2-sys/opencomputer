"""Auto-prune fires (or doesn't) based on SessionConfig at AgentLoop startup."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch


def test_maybe_run_auto_prune_calls_db_when_configured(tmp_path: Path) -> None:
    from opencomputer.agent.config import Config, SessionConfig
    from opencomputer.agent.loop import _maybe_run_auto_prune
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("old", platform="cli", model="m", title="")
    with db._connect() as c:
        c.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 200 * 86400, "old"),
        )
    cfg = Config()
    # Replace the frozen-dataclass field via dataclasses.replace.
    import dataclasses

    cfg = dataclasses.replace(
        cfg,
        session=SessionConfig(
            auto_prune_days=90, auto_prune_untitled_days=7
        ),
    )
    with patch.object(SessionDB, "auto_prune", return_value=0) as mock_prune:
        _maybe_run_auto_prune(db, cfg)
        mock_prune.assert_called_once_with(
            older_than_days=90, untitled_days=7, min_messages=3
        )


def test_maybe_run_auto_prune_skips_when_disabled(tmp_path: Path) -> None:
    from opencomputer.agent.config import Config
    from opencomputer.agent.loop import _maybe_run_auto_prune
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    cfg = Config()  # session defaults: all zeros (disabled)
    with patch.object(SessionDB, "auto_prune") as mock_prune:
        _maybe_run_auto_prune(db, cfg)
        mock_prune.assert_not_called()
