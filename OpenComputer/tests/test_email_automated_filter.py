"""Automated-sender filter for the Email adapter (PR 3c.1).

Covers the four detection mechanisms in :func:`_is_automated_sender`:

- Local-part patterns (``noreply``, ``no-reply``, ``donotreply``,
  ``do-not-reply``, ``postmaster``, ``mailer-daemon``, ``bounce``,
  ``bounces``) — case-insensitive.
- ``Precedence: bulk|list|junk`` header.
- ``Auto-Submitted`` / ``X-Auto-Response-Suppress`` / ``List-Unsubscribe``
  / ``List-Id`` header presence.
- Negative case: a normal human-authored mail passes through.

We also assert the integrated path: when an automated message is
encountered in :meth:`EmailAdapter._email_to_event`, the call returns
``None`` AND emits an INFO log line of the form
``"dropping automated mail: <addr> (reason=<reason>)"``.
"""

from __future__ import annotations

import importlib.util
import logging
from email.message import EmailMessage
from pathlib import Path

import pytest


def _load():
    spec = importlib.util.spec_from_file_location(
        "email_adapter_automated_filter_test",
        Path(__file__).resolve().parent.parent / "extensions" / "email" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load()


# ---------------------------------------------------------------------------
# noreply / postmaster / bounce local-parts
# ---------------------------------------------------------------------------


class TestNoreplyLocalParts:
    @pytest.mark.parametrize(
        "addr",
        [
            "noreply@example.com",
            "no-reply@example.com",
            "donotreply@example.com",
            "do-not-reply@example.com",
            "postmaster@example.com",
            "mailer-daemon@example.com",
            "bounce@example.com",
            "bounces@example.com",
        ],
    )
    def test_each_pattern_matched(self, mod, addr) -> None:
        assert mod._is_automated_sender(addr, {}) is True

    def test_case_insensitive(self, mod) -> None:
        # Postmaster + uppercase shouldn't sneak past.
        assert mod._is_automated_sender("Postmaster@Example.COM", {}) is True
        assert mod._is_automated_sender("NOREPLY@example.com", {}) is True
        assert mod._is_automated_sender("MAILER-DAEMON@x.org", {}) is True


# ---------------------------------------------------------------------------
# Precedence header (bulk/list/junk)
# ---------------------------------------------------------------------------


class TestPrecedenceHeader:
    @pytest.mark.parametrize("value", ["bulk", "list", "junk", "BULK", "Junk"])
    def test_precedence_drops(self, mod, value) -> None:
        assert (
            mod._is_automated_sender(
                "alice@example.com", {"Precedence": value}
            )
            is True
        )

    def test_precedence_other_does_not_drop(self, mod) -> None:
        # "first-class" / "urgent" / arbitrary strings shouldn't trigger.
        assert (
            mod._is_automated_sender(
                "alice@example.com", {"Precedence": "first-class"}
            )
            is False
        )


# ---------------------------------------------------------------------------
# Header-presence triggers
# ---------------------------------------------------------------------------


class TestAutomatedHeaders:
    def test_auto_submitted_drops(self, mod) -> None:
        assert (
            mod._is_automated_sender(
                "alice@example.com", {"Auto-Submitted": "auto-replied"}
            )
            is True
        )

    def test_x_auto_response_suppress_drops(self, mod) -> None:
        assert (
            mod._is_automated_sender(
                "alice@example.com", {"X-Auto-Response-Suppress": "All"}
            )
            is True
        )

    def test_list_unsubscribe_drops(self, mod) -> None:
        assert (
            mod._is_automated_sender(
                "alice@example.com",
                {"List-Unsubscribe": "<mailto:unsub@example.com>"},
            )
            is True
        )

    def test_list_id_drops(self, mod) -> None:
        assert (
            mod._is_automated_sender(
                "alice@example.com", {"List-Id": "<promo.example.com>"}
            )
            is True
        )

    def test_case_insensitive_header_names(self, mod) -> None:
        # IMAP / RFC 5322 header names are case-insensitive — a server
        # delivering "auto-submitted" lowercased must still be caught.
        assert (
            mod._is_automated_sender(
                "alice@example.com", {"auto-submitted": "auto-replied"}
            )
            is True
        )


# ---------------------------------------------------------------------------
# Negative case — human mail passes through
# ---------------------------------------------------------------------------


class TestNormalMail:
    def test_normal_mail_not_dropped(self, mod) -> None:
        # Real-world headers from a personal correspondent: From, Subject,
        # Date, Message-ID, Content-Type. None of these are in the
        # automated-detection set.
        headers = {
            "From": "Alice <alice@example.com>",
            "Subject": "lunch tomorrow?",
            "Date": "Mon, 28 Apr 2026 12:34:56 +0000",
            "Message-ID": "<abc123@example.com>",
            "Content-Type": "text/plain; charset=utf-8",
        }
        assert mod._is_automated_sender("alice@example.com", headers) is False


# ---------------------------------------------------------------------------
# Integration: _email_to_event drops automated + emits INFO log
# ---------------------------------------------------------------------------


class TestEmailToEventDrops:
    def _make_adapter(self, mod):
        return mod.EmailAdapter(
            config={
                "imap_host": "imap.test.local",
                "username": "agent@test.local",
                "password": "x",
                "poll_interval_seconds": 60,
            }
        )

    def test_noreply_drops_with_log(self, mod, caplog) -> None:
        adapter = self._make_adapter(mod)
        msg = EmailMessage()
        msg["From"] = "noreply@vendor.com"
        msg["Subject"] = "Your receipt"
        msg.set_content("Thanks for your order.")
        with caplog.at_level(logging.INFO, logger="opencomputer.ext.email"):
            event = adapter._email_to_event(msg)
        assert event is None
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "dropping automated mail" in joined
        assert "noreply@vendor.com" in joined
        assert "reason=noreply-pattern" in joined

    def test_precedence_bulk_drops(self, mod, caplog) -> None:
        adapter = self._make_adapter(mod)
        msg = EmailMessage()
        msg["From"] = "newsletter@vendor.com"
        msg["Subject"] = "Weekly update"
        msg["Precedence"] = "bulk"
        msg.set_content("Read me!")
        with caplog.at_level(logging.INFO, logger="opencomputer.ext.email"):
            event = adapter._email_to_event(msg)
        assert event is None
        assert any("reason=precedence:bulk" in r.getMessage() for r in caplog.records)

    def test_auto_submitted_drops(self, mod, caplog) -> None:
        adapter = self._make_adapter(mod)
        msg = EmailMessage()
        msg["From"] = "auto@example.com"
        msg["Subject"] = "Out of office"
        msg["Auto-Submitted"] = "auto-replied"
        msg.set_content("I'm OOO.")
        with caplog.at_level(logging.INFO, logger="opencomputer.ext.email"):
            event = adapter._email_to_event(msg)
        assert event is None
        assert any("reason=header:auto-submitted" in r.getMessage() for r in caplog.records)

    def test_normal_human_mail_emits_event(self, mod) -> None:
        adapter = self._make_adapter(mod)
        msg = EmailMessage()
        msg["From"] = "Alice <alice@example.com>"
        msg["Subject"] = "lunch tomorrow?"
        msg.set_content("Want to grab lunch tomorrow?")
        event = adapter._email_to_event(msg)
        assert event is not None
        assert event.user_id == "alice@example.com"
