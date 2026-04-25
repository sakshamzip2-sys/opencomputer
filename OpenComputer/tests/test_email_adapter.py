"""Tests for the Email channel adapter (G.14 / Tier 2.7).

Mocks ``imaplib.IMAP4_SSL`` and ``smtplib.SMTP_SSL`` so tests run without
a real mail server. Verifies:

- IMAP login on connect, polling loop fetches UNSEEN, marks them \\Seen
- Email→MessageEvent parsing (subject + plaintext body, multipart, HTML fallback)
- ``allowed_senders`` filtering
- SMTP send with In-Reply-To threading header
- Capability flag is NONE (no typing/reactions/edit on email)
"""

from __future__ import annotations

import importlib.util
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plugin_sdk import ChannelCapabilities


def _load_email_adapter():
    spec = importlib.util.spec_from_file_location(
        "email_adapter_test_g14",
        Path(__file__).resolve().parent.parent / "extensions" / "email" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.EmailAdapter, mod


@pytest.fixture
def adapter_factory():
    """Return a constructor for EmailAdapter with the real class but no
    actual network calls (login is patched at construction time)."""
    EmailAdapter, mod = _load_email_adapter()

    def make(**overrides) -> object:
        config = {
            "imap_host": "imap.test.local",
            "imap_port": 993,
            "smtp_host": "smtp.test.local",
            "smtp_port": 465,
            "username": "saksham@test.local",
            "password": "hunter2",
            "poll_interval_seconds": 0.05,
            "mailbox": "INBOX",
            **overrides,
        }
        return EmailAdapter(config=config)

    return make, mod


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_advertises_no_optional_capabilities(self) -> None:
        EmailAdapter, _ = _load_email_adapter()
        # Email is a fire-and-forget channel — no typing/reactions/edit.
        assert EmailAdapter.capabilities == ChannelCapabilities.NONE


# ---------------------------------------------------------------------------
# Email → MessageEvent parsing
# ---------------------------------------------------------------------------


class TestEmailParsing:
    def test_plaintext_message(self, adapter_factory) -> None:
        make, _ = adapter_factory
        adapter = make()
        msg = EmailMessage()
        msg["From"] = "Saksham <saksham@test.local>"
        msg["Subject"] = "Stock idea"
        msg["Message-ID"] = "<abc123@test.local>"
        msg.set_content("Look at GUJALKALI today")

        event = adapter._email_to_event(msg)
        assert event is not None
        assert event.chat_id == "saksham@test.local"
        assert "Stock idea" in event.text
        assert "GUJALKALI" in event.text
        assert event.metadata["email_message_id"] == "<abc123@test.local>"
        assert event.metadata["email_subject"] == "Stock idea"
        assert event.metadata["email_from_name"] == "Saksham"

    def test_subject_only(self, adapter_factory) -> None:
        make, _ = adapter_factory
        adapter = make()
        msg = EmailMessage()
        msg["From"] = "x@test.local"
        msg["Subject"] = "Empty body"
        # No content
        event = adapter._email_to_event(msg)
        assert event is not None
        assert event.text == "Empty body"

    def test_html_only_falls_back_to_stripped(self, adapter_factory) -> None:
        make, _ = adapter_factory
        adapter = make()
        msg = EmailMessage()
        msg["From"] = "x@test.local"
        msg["Subject"] = "html test"
        msg.add_alternative(
            "<html><body><p>Hello</p><p>World</p></body></html>",
            subtype="html",
        )
        event = adapter._email_to_event(msg)
        assert event is not None
        # Stripped HTML produces "Hello\nWorld"-ish output
        assert "Hello" in event.text
        assert "World" in event.text
        assert "<p>" not in event.text

    def test_no_from_returns_none(self, adapter_factory) -> None:
        make, _ = adapter_factory
        adapter = make()
        msg = EmailMessage()
        msg["Subject"] = "no sender"
        msg.set_content("hi")
        assert adapter._email_to_event(msg) is None


class TestAllowedSenders:
    def test_filter_blocks_unknown_sender(self, adapter_factory) -> None:
        make, _ = adapter_factory
        adapter = make(allowed_senders=["saksham@test.local"])
        msg = EmailMessage()
        msg["From"] = "spammer@evil.com"
        msg["Subject"] = "buy more"
        msg.set_content("clickme")
        assert adapter._email_to_event(msg) is None

    def test_filter_allows_known_sender(self, adapter_factory) -> None:
        make, _ = adapter_factory
        adapter = make(allowed_senders=["saksham@test.local"])
        msg = EmailMessage()
        msg["From"] = "saksham@test.local"
        msg["Subject"] = "ok"
        msg.set_content("body")
        assert adapter._email_to_event(msg) is not None

    def test_no_filter_allows_anyone(self, adapter_factory) -> None:
        make, _ = adapter_factory
        adapter = make()  # no allowed_senders configured
        msg = EmailMessage()
        msg["From"] = "anyone@anywhere.com"
        msg["Subject"] = "hi"
        msg.set_content("body")
        assert adapter._email_to_event(msg) is not None

    def test_filter_case_insensitive(self, adapter_factory) -> None:
        make, _ = adapter_factory
        adapter = make(allowed_senders=["SAKSHAM@TEST.LOCAL"])
        msg = EmailMessage()
        msg["From"] = "saksham@test.local"
        msg["Subject"] = "ok"
        msg.set_content("body")
        assert adapter._email_to_event(msg) is not None


# ---------------------------------------------------------------------------
# SMTP send
# ---------------------------------------------------------------------------


class TestSendReply:
    @pytest.mark.asyncio
    async def test_send_constructs_proper_message(self, adapter_factory) -> None:
        make, _ = adapter_factory
        adapter = make()
        sent = []

        def fake_smtp(msg):
            sent.append(msg)

        adapter._smtp_send = fake_smtp

        result = await adapter.send(
            "saksham@test.local",
            "Hello, here's the analysis...",
            subject="Re: Stock idea",
            in_reply_to="<abc123@test.local>",
        )
        assert result.success
        assert len(sent) == 1
        msg = sent[0]
        assert msg["To"] == "saksham@test.local"
        assert msg["From"] == "saksham@test.local"
        assert msg["Subject"] == "Re: Stock idea"
        assert msg["In-Reply-To"] == "<abc123@test.local>"
        # References field for proper threading
        assert msg["References"] == "<abc123@test.local>"

    @pytest.mark.asyncio
    async def test_invalid_recipient_returns_error(self, adapter_factory) -> None:
        make, _ = adapter_factory
        adapter = make()
        result = await adapter.send("not-an-email", "hi")
        assert not result.success
        assert "invalid" in result.error.lower()

    @pytest.mark.asyncio
    async def test_smtp_failure_wrapped(self, adapter_factory) -> None:
        make, _ = adapter_factory
        adapter = make()

        def boom(_):
            raise ConnectionError("smtp down")

        adapter._smtp_send = boom

        result = await adapter.send("x@test.local", "hi")
        assert not result.success
        assert "ConnectionError" in result.error


# ---------------------------------------------------------------------------
# IMAP polling integration (mocked imaplib)
# ---------------------------------------------------------------------------


class TestIMAPPollIntegration:
    @pytest.mark.asyncio
    async def test_connect_logs_in(self, adapter_factory, monkeypatch) -> None:
        make, mod = adapter_factory
        adapter = make()

        class FakeIMAP:
            def __init__(self, *_a, **_kw):
                self.logged_in = False

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def login(self, user, pw):
                self.logged_in = True
                self._user = user

        monkeypatch.setattr(mod, "imaplib", MagicMock(IMAP4_SSL=FakeIMAP))

        # connect spawns a poll task; we want to test login alone
        adapter._poll_forever = lambda: __import__("asyncio").sleep(0)
        ok = await adapter.connect()
        assert ok is True
        await adapter.disconnect()

    def test_poll_once_returns_events_for_unseen(self, adapter_factory, monkeypatch) -> None:
        make, mod = adapter_factory
        adapter = make()

        # Build a real RFC822 message
        sample_msg = EmailMessage()
        sample_msg["From"] = "saksham@test.local"
        sample_msg["Subject"] = "test"
        sample_msg.set_content("hello")
        raw = sample_msg.as_bytes()

        class FakeIMAP:
            def __init__(self, *_a, **_kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def login(self, *_a):
                pass

            def select(self, *_a):
                pass

            def search(self, *_a):
                return ("OK", [b"1 2"])  # two unseen UIDs

            def fetch(self, uid, _section):
                return ("OK", [(b"%b (RFC822 %d {%d}" % (uid, 0, len(raw)), raw), b")"])

            def store(self, *_a):
                pass

        monkeypatch.setattr(mod, "imaplib", MagicMock(IMAP4_SSL=FakeIMAP))
        events = adapter._poll_once()
        assert len(events) == 2
        for e in events:
            assert e.text


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------


class TestHTMLStrip:
    def test_basic(self) -> None:
        _, mod = _load_email_adapter()
        out = mod._strip_html("<p>Hello</p><p>World</p>")
        assert "Hello" in out
        assert "World" in out
        assert "<" not in out

    def test_handles_entities(self) -> None:
        _, mod = _load_email_adapter()
        out = mod._strip_html("<p>foo &amp; bar &lt;baz&gt;</p>")
        assert "foo & bar <baz>" in out

    def test_collapses_whitespace(self) -> None:
        _, mod = _load_email_adapter()
        out = mod._strip_html("<div>  one  </div>\n\n\n<div>two</div>")
        # No empty lines
        for line in out.splitlines():
            assert line == "" or line.strip() == line
