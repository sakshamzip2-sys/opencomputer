"""Phase 7 tests for the redactor + distiller.

Covers (matching ``docs/plans/social-traces-plugin.md`` §10 Phase 7):

* Redactor: every regex pattern with positive + negative cases.
* ``redact()`` pipeline: layer ordering, opt-in toggles, sensitive-
  filter precedence, sentinel handling, ``is_useful_body`` semantics.
* Distiller orchestrator: three-Haiku flow, cost-guard pre-flight,
  redaction sweep on input AND output, schema validation, failure
  paths (no provider / parse failure / sentinel-only output / oversize).
* End-to-end: seed a SessionDB, run ``distill_session`` with a fake
  provider, verify the resulting TraceCard has redacted content and
  passes ``_validate``.

The redactor is the load-bearing privacy layer — failures here ship
sensitive data off-device, so the test surface is wide.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_ST_DIR = _EXT_DIR / "social-traces"


def _ensure_alias() -> None:
    if "extensions.social_traces.redactor" in sys.modules:
        return
    if "extensions" not in sys.modules:
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(_EXT_DIR)]
        ext_pkg.__package__ = "extensions"
        sys.modules["extensions"] = ext_pkg
    if "extensions.social_traces" not in sys.modules:
        mod = types.ModuleType("extensions.social_traces")
        mod.__path__ = [str(_ST_DIR)]
        mod.__package__ = "extensions.social_traces"
        sys.modules["extensions.social_traces"] = mod
        sys.modules["extensions"].social_traces = mod  # type: ignore[attr-defined]
    parent = sys.modules["extensions.social_traces"]
    for sub in (
        "state",
        "identity",
        "config",
        "session_state",
        "tag_extractor",
        "redactor",
        "novelty_judge",
        "distiller",
        "prefetch",
        "subscriber",
    ):
        full_name = f"extensions.social_traces.{sub}"
        if full_name in sys.modules:
            setattr(parent, sub, sys.modules[full_name])
            continue
        init = _ST_DIR / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.social_traces"
        sys.modules[full_name] = sub_mod
        spec.loader.exec_module(sub_mod)
        setattr(parent, sub, sub_mod)


_ensure_alias()

from extensions.social_traces import distiller as st_distiller  # noqa: E402
from extensions.social_traces import redactor as r  # noqa: E402
from plugin_sdk.core import Message  # noqa: E402
from plugin_sdk.provider_contract import (  # noqa: E402
    BaseProvider,
    ProviderResponse,
    Usage,
)
from plugin_sdk.traces import TRACE_API_V1, TraceCard, TraceMeta, TraceStep  # noqa: E402


# ─── redactor: PII layer (always on) ─────────────────────────────────


def test_redact_pii_credit_card():
    out = r.redact_pii("My card is 4111-1111-1111-1111 and expires soon")
    assert "4111" not in out
    assert r.REDACTED_PII in out


def test_redact_pii_ssn():
    out = r.redact_pii("SSN: 123-45-6789 do not share")
    assert "123-45-6789" not in out
    assert r.REDACTED_PII in out


def test_redact_pii_email():
    out = r.redact_pii("Contact admin@example.com for help")
    assert "admin@example.com" not in out


def test_redact_pii_phone():
    out = r.redact_pii("Call me at (555) 123-4567 or 555.123.4567")
    assert "555" not in out


def test_redact_pii_passes_through_clean_text():
    text = "Just a normal sentence about rsync flags"
    assert r.redact_pii(text) == text


def test_redact_pii_handles_empty():
    assert r.redact_pii("") == ""


# ─── redactor: secrets layer ─────────────────────────────────────────


def test_redact_secrets_openai_anthropic():
    out = r.redact_secrets("ANTHROPIC_API_KEY=sk-ant-abc123def456ghi789jkl0")
    assert "sk-ant-abc123def456ghi789jkl0" not in out


def test_redact_secrets_github_token():
    out = r.redact_secrets("git push origin ghp_aBcDeF1234567890aBcDeF1234")
    assert "ghp_aBcDeF1234567890aBcDeF1234" not in out


def test_redact_secrets_google_api_key():
    out = r.redact_secrets("key=AIzaSyABC1234567890DEF1234567890GHIjklm")
    assert "AIzaSyABC1234567890DEF1234567890GHIjklm" not in out


def test_redact_secrets_bearer_token():
    out = r.redact_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cC")
    assert "eyJhbGciOiJIUzI1NiIsInR5cC" not in out


def test_redact_secrets_password_assignment():
    out = r.redact_secrets('password="hunter2-very-long-secret-string-here"')
    assert "hunter2-very-long" not in out


def test_redact_secrets_doesnt_clobber_short_strings():
    """Keys must be ≥20 chars to match — don't hit short identifiers."""
    text = "let x = 'sk-1234'  # too short"
    assert r.redact_secrets(text) == text


# ─── redactor: paths (opt-in) ────────────────────────────────────────


def test_redact_paths_unix_user_home():
    out = r.redact_paths("Reading /Users/saksham/Documents/secret.txt now")
    assert "/Users/saksham" not in out
    assert r.REDACTED_PATH in out


def test_redact_paths_tilde():
    out = r.redact_paths("Found ~/Downloads/leak.json on the disk")
    assert "~/Downloads" not in out


def test_redact_paths_var_log():
    out = r.redact_paths("Tail /var/log/auth.log for failures")
    assert "/var/log/auth.log" not in out


def test_redact_paths_windows():
    out = r.redact_paths(
        r"Saved to C:\Users\Bob\Documents\secret.txt successfully"
    )
    assert r"C:\Users\Bob" not in out


def test_redact_paths_passes_through_relative_paths():
    """Relative paths like ``src/foo.py`` are NOT redacted — they
    rarely contain user identity, and aggressive matching kills
    legitimate code references in traces."""
    text = "edit src/foo.py and run tests/test_foo.py"
    assert r.redact_paths(text) == text


# ─── redactor: hostnames + IPs (opt-in) ──────────────────────────────


def test_redact_hostnames_url():
    out = r.redact_hostnames("Visit https://nas.local/share for the file")
    assert "nas.local" not in out
    assert r.REDACTED_HOST in out


def test_redact_hostnames_internal_bare():
    out = r.redact_hostnames("ssh into my macbook.lan with password")
    assert "macbook.lan" not in out


def test_redact_hostnames_ipv4():
    out = r.redact_hostnames("Connect to 192.168.1.42 first, then 8.8.8.8")
    assert "192.168.1.42" not in out
    assert "8.8.8.8" not in out


def test_redact_hostnames_ipv6():
    out = r.redact_hostnames("Reach fe80::1234:5678:9abc:def0 via mesh")
    assert "fe80::1234:5678:9abc:def0" not in out


def test_redact_hostnames_passes_public_domain():
    """We don't redact ``github.com`` — public domains aren't
    identifying, and clobbering them would make most traces
    useless."""
    text = "git push to github.com:user/repo.git completed"
    out = r.redact_hostnames(text)
    # github.com must survive the bare-host filter (only 2 labels).
    assert "github.com" in out


# ─── redactor: pipeline + caller filter ──────────────────────────────


def test_redact_pipeline_applies_all_layers():
    text = (
        "User /Users/saksham (admin@example.com, 555-123-4567) "
        "uploaded /var/log/foo.log to https://nas.local with "
        "password=hunter2-supersecret-token-string"
    )
    out = r.redact(text)
    assert "saksham" not in out
    assert "admin@example.com" not in out
    assert "/var/log/foo.log" not in out
    assert "nas.local" not in out
    assert "hunter2-supersecret-token-string" not in out


def test_redact_path_layer_off_keeps_paths():
    out = r.redact("/Users/saksham/file.txt is the source", redact_paths_layer=False)
    assert "/Users/saksham/file.txt" in out


def test_redact_hostname_layer_off_keeps_urls():
    out = r.redact("Visit https://nas.local/share now", redact_hostnames_layer=False)
    assert "https://nas.local/share" in out


def test_redact_caller_filter_collapses_whole_body():
    def filter_(text: str) -> bool:
        return "internal-codename" in text

    out = r.redact("This mentions internal-codename in passing", sensitive_filter=filter_)
    assert out == r.REDACTED


def test_redact_caller_filter_raises_redacts():
    def filter_(text: str) -> bool:
        raise RuntimeError("filter bug")

    out = r.redact("any text", sensitive_filter=filter_)
    assert out == r.REDACTED


def test_redact_caller_filter_clean_text_passes_through():
    def filter_(text: str) -> bool:
        return False

    out = r.redact("safe text", sensitive_filter=filter_)
    assert out == "safe text"


def test_redact_empty_input():
    assert r.redact("") == ""
    assert r.redact("   ") == "   "


# ─── redactor: is_useful_body ────────────────────────────────────────


def test_is_useful_body_real_content():
    assert r.is_useful_body(
        "This is a meaningful sentence describing what happened.",
    )


def test_is_useful_body_sentinel_only():
    text = f"{r.REDACTED_PII} {r.REDACTED_PATH} {r.REDACTED_HOST}"
    assert r.is_useful_body(text) is False


def test_is_useful_body_empty():
    assert r.is_useful_body("") is False
    assert r.is_useful_body("   ") is False


def test_is_useful_body_too_short():
    assert r.is_useful_body("hi", min_chars=20) is False


def test_is_useful_body_useful_with_some_redactions():
    text = (
        f"The agent successfully ran rsync against {r.REDACTED_PATH} "
        "and the result was clean."
    )
    assert r.is_useful_body(text) is True


# ─── distiller: helpers ──────────────────────────────────────────────


def test_normalize_tags_filters_short_long():
    out = st_distiller._normalize_tags(("ok", "x", "okay-tag", "x" * 50))
    # "x" too short, "x"*50 too long
    assert "x" not in out
    assert all(2 <= len(t) <= 30 for t in out)


def test_normalize_tags_lowercases_and_replaces_specials():
    out = st_distiller._normalize_tags(("Home Lab", "FILE_SYNC"))
    assert all(t == t.lower() for t in out)
    assert all(c == "-" or c.isalnum() for tag in out for c in tag)


def test_normalize_tags_dedupes():
    out = st_distiller._normalize_tags(("homelab", "homelab", "filesync"))
    assert out.count("homelab") == 1


def test_normalize_tags_caps_count():
    raw = tuple(f"tag{i:02d}" for i in range(20))
    out = st_distiller._normalize_tags(raw)
    assert len(out) <= 10


def test_parse_steps_json_bare_list():
    text = '[{"tool": "Bash", "args_summary": "ls", "result_summary": "ok"}]'
    parsed = st_distiller._parse_steps_json(text)
    assert parsed == [
        {"tool": "Bash", "args_summary": "ls", "result_summary": "ok"}
    ]


def test_parse_steps_json_with_markdown_fence():
    text = (
        "```json\n"
        '[{"tool": "Read", "args_summary": "file", "result_summary": "ok"}]\n'
        "```"
    )
    parsed = st_distiller._parse_steps_json(text)
    assert parsed is not None and parsed[0]["tool"] == "Read"


def test_parse_steps_json_with_prose_around():
    text = "Sure, here are the steps:\n[{\"tool\": \"Bash\"}]\nDone."
    parsed = st_distiller._parse_steps_json(text)
    assert parsed is not None and parsed[0]["tool"] == "Bash"


def test_parse_steps_json_invalid_returns_none():
    assert st_distiller._parse_steps_json("not json at all") is None
    assert st_distiller._parse_steps_json("") is None


# ─── distiller: validation ───────────────────────────────────────────


def _make_valid_card(**overrides) -> TraceCard:
    base = dict(
        schema_version=TRACE_API_V1,
        intent="Sync files between two homelab boxes via rsync.",
        meta=TraceMeta(
            tags=("homelab", "filesync"),
            outcome="success",
            token_cost=500,
            loop_count=2,
            harness_version="0.1.0",
            submitter_hash="0" * 64,
        ),
        steps=(
            TraceStep(
                tool_name="Bash",
                arguments_summary="rsync -a src/ dst/",
                result_summary="0 errors",
                duration_ms=1500,
            ),
        ),
        distilled_insight=(
            "Use rsync --checksum on LAN to avoid clock-skew issues."
        ),
        created_at="2026-05-06T12:00:00Z",
    )
    base.update(overrides)
    return TraceCard(**base)


def test_validate_accepts_valid_card():
    assert st_distiller._validate(_make_valid_card()) is True


def test_validate_rejects_short_intent():
    assert st_distiller._validate(_make_valid_card(intent="x")) is False


def test_validate_rejects_oversize_insight():
    assert st_distiller._validate(
        _make_valid_card(distilled_insight="x" * 3000)
    ) is False


def test_validate_rejects_empty_tags():
    bad = _make_valid_card(
        meta=TraceMeta(
            tags=(),
            outcome="success",
            token_cost=0,
            loop_count=0,
            harness_version="0",
            submitter_hash="0" * 64,
        )
    )
    assert st_distiller._validate(bad) is False


def test_validate_rejects_empty_steps():
    assert st_distiller._validate(_make_valid_card(steps=())) is False


def test_validate_rejects_short_submitter_hash():
    bad = _make_valid_card(
        meta=TraceMeta(
            tags=("homelab",),
            outcome="success",
            token_cost=0,
            loop_count=0,
            harness_version="0",
            submitter_hash="abc",  # too short
        )
    )
    assert st_distiller._validate(bad) is False


# ─── distiller: per-call helpers ─────────────────────────────────────


class _FakeProvider(BaseProvider):
    """Provider that returns canned responses in order, one per call."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, **kw):
        self.calls.append(kw)
        text = self._responses.pop(0) if self._responses else ""
        return ProviderResponse(
            message=Message(role="assistant", content=text),
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=20),
        )

    async def stream_complete(self, **kw):  # pragma: no cover
        yield


async def test_distill_intent_returns_redacted_text():
    provider = _FakeProvider(
        ["The user wants to sync files at /Users/saksham/work to a NAS."]
    )
    out = await st_distiller._distill_intent(
        provider=provider,
        cost_guard=None,
        user_message="please help me sync /Users/saksham/work",
        redact_paths_layer=True,
        redact_hostnames_layer=True,
        sensitive_filter=None,
        model="claude-haiku-4-5",
    )
    assert out is not None
    assert "/Users/saksham" not in out
    assert r.REDACTED_PATH in out


async def test_distill_intent_skips_when_user_message_redacted_out():
    """If the user message is entirely sensitive, we never even call
    the provider."""
    def _filter(text: str) -> bool:
        return True  # always sensitive

    provider = _FakeProvider(["should not be called"])
    out = await st_distiller._distill_intent(
        provider=provider,
        cost_guard=None,
        user_message="anything goes here",
        redact_paths_layer=True,
        redact_hostnames_layer=True,
        sensitive_filter=_filter,
        model="x",
    )
    assert out is None
    assert provider.calls == []


async def test_distill_steps_parses_json_list():
    provider = _FakeProvider([
        '[{"tool": "Bash", "args_summary": "rsync -a src dst", '
        '"result_summary": "0 errors"}, '
        '{"tool": "Read", "args_summary": "manifest.txt", '
        '"result_summary": "42 entries"}]'
    ])
    steps = await st_distiller._distill_steps(
        provider=provider,
        cost_guard=None,
        user_message="sync",
        transcript="[user] sync\n[assistant] done",
        redact_paths_layer=True,
        redact_hostnames_layer=True,
        sensitive_filter=None,
        model="x",
    )
    assert steps is not None
    assert len(steps) == 2
    assert steps[0].tool_name == "Bash"
    assert steps[1].tool_name == "Read"


async def test_distill_steps_returns_none_on_parse_failure():
    provider = _FakeProvider(["I don't think there are any steps to share"])
    steps = await st_distiller._distill_steps(
        provider=provider, cost_guard=None,
        user_message="x", transcript="x",
        redact_paths_layer=True, redact_hostnames_layer=True,
        sensitive_filter=None, model="x",
    )
    assert steps is None


async def test_distill_insight_redacts_output():
    """LLM may emit a path even though the prompt asks it not to —
    the post-call redactor sweep catches it."""
    provider = _FakeProvider([
        "When syncing files, rsync's --checksum flag is more reliable "
        "than --update on /Users/saksham/work because of clock skew."
    ])
    out = await st_distiller._distill_insight(
        provider=provider, cost_guard=None,
        intent="sync",
        transcript="[user] sync\n[assistant] done",
        redact_paths_layer=True, redact_hostnames_layer=True,
        sensitive_filter=None, model="x",
    )
    assert out is not None
    assert "/Users/saksham" not in out
    assert "rsync" in out  # legitimate domain word survives


# ─── distiller: cost-guard pre-flight ────────────────────────────────


class _FakeCostGuard:
    def __init__(self, *, allowed: bool = True):
        self.allowed = allowed
        self.checks: list[Any] = []
        self.usages: list[Any] = []

    def check_budget(self, provider, projected_cost_usd):
        self.checks.append((provider, projected_cost_usd))
        return self.allowed

    def record_usage(self, provider, *, cost_usd, operation=None):
        self.usages.append((provider, cost_usd, operation))


async def test_distill_intent_skips_when_cost_guard_denies():
    guard = _FakeCostGuard(allowed=False)
    provider = _FakeProvider(["never called"])
    out = await st_distiller._distill_intent(
        provider=provider, cost_guard=guard,
        user_message="some safe text",
        redact_paths_layer=True, redact_hostnames_layer=True,
        sensitive_filter=None, model="x",
    )
    assert out is None
    assert provider.calls == []
    # Guard was checked but no usage recorded.
    assert len(guard.checks) == 1
    assert guard.usages == []


async def test_distill_intent_records_usage_after_call():
    guard = _FakeCostGuard(allowed=True)
    provider = _FakeProvider(["A clean intent sentence about syncing."])
    await st_distiller._distill_intent(
        provider=provider, cost_guard=guard,
        user_message="sync stuff",
        redact_paths_layer=True, redact_hostnames_layer=True,
        sensitive_filter=None, model="x",
    )
    assert len(guard.usages) == 1


# ─── distiller: orchestrator end-to-end ──────────────────────────────


async def test_distill_session_no_provider_returns_none(tmp_path: Path):
    out = await st_distiller.distill_session(
        session_id="sid",
        profile_home=tmp_path,
        submitter_hash="0" * 64,
        provider=None,
    )
    assert out is None


async def test_distill_session_no_user_message_returns_none(tmp_path: Path):
    """Empty SessionDB → can't distill, return None silently."""
    out = await st_distiller.distill_session(
        session_id="ghost",
        profile_home=tmp_path,
        submitter_hash="0" * 64,
        provider=_FakeProvider([]),
    )
    assert out is None


async def test_distill_session_full_round_trip(tmp_path: Path):
    """Seed a SessionDB, run the orchestrator with three canned LLM
    responses, verify the resulting TraceCard validates and contains
    redacted content."""
    # Seed SessionDB.
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("e2e-sid", platform="cli", model="x")
    db.append_message(
        "e2e-sid",
        Message(
            role="user",
            content="please help me sync homelab filesync between machines",
        ),
    )
    db.append_message(
        "e2e-sid",
        Message(role="assistant", content="I'll use rsync."),
    )
    db.append_message(
        "e2e-sid",
        Message(role="tool", content="rsync ran with 0 errors"),
    )

    # Three canned responses for intent / steps / insight.
    provider = _FakeProvider([
        "User wanted to sync files between two homelab machines via rsync.",
        '[{"tool": "Bash", "args_summary": "rsync -a src dst", '
        '"result_summary": "0 errors"}]',
        "Use rsync --checksum on LAN homelab setups to bypass clock-skew "
        "synchronisation problems entirely.",
    ])

    card = await st_distiller.distill_session(
        session_id="e2e-sid",
        profile_home=tmp_path,
        submitter_hash="0123456789abcdef" * 4,  # 64 hex chars
        provider=provider,
        harness_version="0.1.0",
    )

    assert card is not None
    assert card.schema_version == TRACE_API_V1
    assert "homelab" in (card.intent or "").lower()
    assert len(card.steps) == 1
    assert card.steps[0].tool_name == "Bash"
    assert "rsync" in card.distilled_insight.lower()
    assert card.meta.outcome == "success"
    assert card.meta.tags  # tags extracted from user message
    # Validates against schema.
    assert st_distiller._validate(card) is True
    # All three LLM calls were made.
    assert len(provider.calls) == 3


async def test_distill_session_intent_failure_aborts_pipeline(tmp_path: Path):
    """When the first call (intent) fails, the steps + insight calls
    must NOT be made. The pipeline aborts cleanly."""
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("abort-sid", platform="cli", model="x")
    db.append_message(
        "abort-sid", Message(role="user", content="some task")
    )

    provider = _FakeProvider(["", "should-not-be-called", "neither"])
    card = await st_distiller.distill_session(
        session_id="abort-sid",
        profile_home=tmp_path,
        submitter_hash="0" * 64,
        provider=provider,
    )
    assert card is None
    # Only one call attempted (intent); pipeline bailed.
    assert len(provider.calls) == 1


async def test_distill_session_caller_filter_redacts_whole(tmp_path: Path):
    """A whole-text sensitive-filter match → user message redacted to
    sentinel → intent stage rejects the (now empty) input → pipeline
    returns None without spending LLM calls."""
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("filter-sid", platform="cli", model="x")
    db.append_message(
        "filter-sid", Message(role="user", content="anything")
    )

    def _filter(text: str) -> bool:
        return True

    provider = _FakeProvider(["never"])
    card = await st_distiller.distill_session(
        session_id="filter-sid",
        profile_home=tmp_path,
        submitter_hash="0" * 64,
        provider=provider,
        sensitive_filter=_filter,
    )
    assert card is None
    assert provider.calls == []


async def test_distill_session_fails_validation_returns_none(
    tmp_path: Path, monkeypatch
):
    """Force ``_validate`` to return False — distiller drops the card."""
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("inv-sid", platform="cli", model="x")
    db.append_message(
        "inv-sid",
        Message(
            role="user",
            content="please help with my homelab filesync project today",
        ),
    )

    provider = _FakeProvider([
        "Sync homelab files across machines via rsync.",
        '[{"tool": "Bash", "args_summary": "rsync x y", '
        '"result_summary": "ok"}]',
        "Use rsync --checksum to avoid clock-skew problems on LAN.",
    ])

    monkeypatch.setattr(st_distiller, "_validate", lambda card: False)
    card = await st_distiller.distill_session(
        session_id="inv-sid",
        profile_home=tmp_path,
        submitter_hash="0" * 64,
        provider=provider,
    )
    assert card is None


async def test_distill_session_outcome_failed_when_caller_passes_failed(
    tmp_path: Path,
):
    """The subscriber sources outcome from ``SessionEndEvent.had_errors``
    — the persisted Message rows don't carry the per-tool is_error
    flag, so the event is the source of truth. Distiller just
    threads through the caller-supplied value (with validation).
    """
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("err-sid", platform="cli", model="x")
    db.append_message(
        "err-sid",
        Message(role="user", content="diagnose homelab sync failure today"),
    )
    db.append_message(
        "err-sid",
        Message(role="tool", content="error: connection refused"),
    )

    provider = _FakeProvider([
        "User tried to diagnose a homelab sync failure.",
        '[{"tool": "Bash", "args_summary": "ssh nas", '
        '"result_summary": "connection refused"}]',
        "When the NAS refuses connections, check the route table and "
        "firewall rules on both ends before debugging the protocol layer.",
    ])

    card = await st_distiller.distill_session(
        session_id="err-sid",
        profile_home=tmp_path,
        submitter_hash="0" * 64,
        provider=provider,
        outcome="failed",
    )
    assert card is not None
    assert card.meta.outcome == "failed"


async def test_distill_session_outcome_invalid_value_falls_back(tmp_path: Path):
    """An unknown outcome string defaults to ``success`` so a future
    bus-event addition doesn't ship malformed cards to the network."""
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("ok-sid", platform="cli", model="x")
    db.append_message(
        "ok-sid",
        Message(role="user", content="some homelab filesync task today"),
    )

    provider = _FakeProvider([
        "User wanted to sync homelab filesync setup.",
        '[{"tool": "Bash", "args_summary": "rsync x y", '
        '"result_summary": "ok"}]',
        "Use rsync --checksum to bypass clock-skew problems on LAN setups.",
    ])

    card = await st_distiller.distill_session(
        session_id="ok-sid",
        profile_home=tmp_path,
        submitter_hash="0" * 64,
        provider=provider,
        outcome="bogus-string",
    )
    assert card is not None
    assert card.meta.outcome == "success"  # defaulted
