"""When OPENCOMPUTER_HEADLESS=1 and ``systemd.journal`` is importable,
configure() must add a JournalHandler to the opencomputer logger."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_journald_handler_added_when_headless_and_systemd_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HEADLESS", "1")
    fake_journal_mod = MagicMock()
    fake_journal_mod.JournalHandler = MagicMock(return_value=logging.NullHandler())
    monkeypatch.setitem(sys.modules, "systemd", MagicMock(journal=fake_journal_mod))
    monkeypatch.setitem(sys.modules, "systemd.journal", fake_journal_mod)

    # Strip any existing JournalHandler from prior test runs (logging
    # state is process-global). The configure() function de-dupes by
    # class name, so a stale handler would mask the test signal.
    oc_logger = logging.getLogger("opencomputer")
    for h in list(oc_logger.handlers):
        if type(h).__name__ == "JournalHandler":
            oc_logger.removeHandler(h)

    from opencomputer.observability import logging_config
    logging_config.configure(home=tmp_path)

    assert fake_journal_mod.JournalHandler.called, "JournalHandler not constructed"


def test_journald_handler_silently_skipped_when_systemd_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Pi headless without python3-systemd should NOT crash; just skip journald."""
    monkeypatch.setenv("OPENCOMPUTER_HEADLESS", "1")
    monkeypatch.setitem(sys.modules, "systemd", None)
    monkeypatch.setitem(sys.modules, "systemd.journal", None)

    from opencomputer.observability import logging_config
    # Must not raise.
    logging_config.configure(home=tmp_path)


def test_journald_handler_NOT_added_when_not_headless(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Even if python3-systemd is installed, interactive runs shouldn't
    use journald — they want stderr/file."""
    monkeypatch.setenv("OPENCOMPUTER_HEADLESS", "0")
    fake_journal_mod = MagicMock()
    fake_journal_mod.JournalHandler = MagicMock(return_value=logging.NullHandler())
    monkeypatch.setitem(sys.modules, "systemd", MagicMock(journal=fake_journal_mod))
    monkeypatch.setitem(sys.modules, "systemd.journal", fake_journal_mod)

    from opencomputer.observability import logging_config
    logging_config.configure(home=tmp_path)
    assert not fake_journal_mod.JournalHandler.called, (
        "JournalHandler attached in non-headless mode"
    )
