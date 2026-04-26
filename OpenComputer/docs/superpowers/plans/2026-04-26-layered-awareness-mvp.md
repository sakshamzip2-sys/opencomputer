# Layered Awareness MVP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-04-26-layered-awareness-design.md`

**Goal:** Build the MVP of "agent already knows the user" — Layer 0 (Identity Reflex) + Layer 1 (Quick Interview) + Layer 2 (Recent Context Scan) + Layer 4 minimal (browser extension capturing tab events) — wired into the existing F4 user-model graph and surfaced in the agent's system prompt.

**Architecture:** New `opencomputer/profile_bootstrap/` package holds Layers 0-2 logic; new `extensions/browser-bridge/` plugin holds Layer 4. Outputs flow through the existing F2 SignalEvent bus → F4 user-model graph. Agent's `PromptBuilder` gains a `user_facts` slot that pulls top-K nodes from the graph for every system prompt. New `opencomputer profile bootstrap` CLI orchestrates the install-time flow.

**Tech Stack:** Python 3.12+, SQLite (existing F4 store), Ollama subprocess (local LLM extraction; optional fallback to existing provider), PyObjC (macOS Calendar/EventKit), aiohttp (existing gateway server gets a new endpoint), Chrome Extension MV3 (Manifest V3, JavaScript), pytest.

---

## File Structure

| Path | Responsibility |
|---|---|
| `opencomputer/profile_bootstrap/__init__.py` | Package marker |
| `opencomputer/profile_bootstrap/identity_reflex.py` | Layer 0 — read system identity (git, contacts, system user) |
| `opencomputer/profile_bootstrap/persistence.py` | Translate Layer 0/1/2 outputs into F4 user-model nodes/edges |
| `opencomputer/profile_bootstrap/quick_interview.py` | Layer 1 — 5 install-time questions |
| `opencomputer/profile_bootstrap/recent_scan.py` | Layer 2 — files + git scan |
| `opencomputer/profile_bootstrap/calendar_reader.py` | Layer 2 helper — PyObjC EventKit |
| `opencomputer/profile_bootstrap/browser_history.py` | Layer 2 helper — per-browser SQLite readers |
| `opencomputer/profile_bootstrap/orchestrator.py` | Wires Layers 0-2 sequentially, called by CLI |
| `opencomputer/profile_bootstrap/bridge_state.py` | Browser-bridge token/port persistence |
| `opencomputer/cli_profile.py` (modify) | Add `bootstrap` and `bridge` subcommands |
| `opencomputer/agent/prompt_builder.py` (modify) | Pull top-K user-model facts into `PromptContext` |
| `opencomputer/agent/prompts/base.j2` (modify) | Render `{{ user_facts }}` block |
| `opencomputer/agent/consent/capability_taxonomy.py` (modify) | New `ingestion.*` capability claims |
| `extensions/browser-bridge/plugin.json` | Plugin manifest |
| `extensions/browser-bridge/plugin.py` | `register(api)` + listener wiring |
| `extensions/browser-bridge/adapter.py` | aiohttp endpoint receiving browser-extension POSTs |
| `extensions/browser-bridge/extension/manifest.json` | Chrome MV3 manifest |
| `extensions/browser-bridge/extension/background.js` | Browser extension service worker |
| `extensions/browser-bridge/README.md` | Install instructions for the browser extension |
| `tests/test_profile_bootstrap_identity_reflex.py` | Layer 0 tests |
| `tests/test_profile_bootstrap_persistence.py` | Persistence layer tests |
| `tests/test_profile_bootstrap_quick_interview.py` | Layer 1 tests |
| `tests/test_profile_bootstrap_recent_scan.py` | Layer 2 tests |
| `tests/test_profile_bootstrap_calendar_reader.py` | Layer 2 calendar tests |
| `tests/test_profile_bootstrap_browser_history.py` | Layer 2 browser-history tests |
| `tests/test_profile_bootstrap_orchestrator.py` | Orchestrator unit test |
| `tests/test_browser_bridge.py` | Browser-bridge plugin tests |
| `tests/test_cli_profile_bridge.py` | Bridge CLI tests |
| `tests/test_capability_taxonomy_ingestion.py` | New capability claims test |
| `tests/test_prompt_builder_user_facts.py` | Prompt injection test |
| `tests/test_cli_profile_bootstrap.py` | Bootstrap CLI + E2E integration test |

---

## Task 1: Add ingestion.* capability claims to F1 taxonomy

**Files:**
- Modify: `opencomputer/agent/consent/capability_taxonomy.py`
- Test: `tests/test_capability_taxonomy_ingestion.py`

- [ ] **Step 1.1: Write the failing test**

```python
# tests/test_capability_taxonomy_ingestion.py
from opencomputer.agent.consent.capability_taxonomy import F1_CAPABILITIES
from plugin_sdk import ConsentTier


def test_ingestion_capabilities_registered():
    assert F1_CAPABILITIES["ingestion.recent_files"] == ConsentTier.IMPLICIT
    assert F1_CAPABILITIES["ingestion.calendar"] == ConsentTier.EXPLICIT
    assert F1_CAPABILITIES["ingestion.browser_history"] == ConsentTier.EXPLICIT
    assert F1_CAPABILITIES["ingestion.git_log"] == ConsentTier.IMPLICIT
    assert F1_CAPABILITIES["ingestion.messages"] == ConsentTier.EXPLICIT
    assert F1_CAPABILITIES["ingestion.browser_extension"] == ConsentTier.EXPLICIT


def test_ingestion_capabilities_all_present():
    expected = {
        "ingestion.recent_files",
        "ingestion.calendar",
        "ingestion.browser_history",
        "ingestion.git_log",
        "ingestion.messages",
        "ingestion.browser_extension",
    }
    assert expected.issubset(F1_CAPABILITIES.keys())
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `pytest tests/test_capability_taxonomy_ingestion.py -v`
Expected: FAIL with `KeyError: 'ingestion.recent_files'`

- [ ] **Step 1.3: Add capability entries**

In `opencomputer/agent/consent/capability_taxonomy.py`, after the last entry in `F1_CAPABILITIES` and before the closing `}`:

```python
    # MVP — Layered Awareness ingestion sources (2026-04-26).
    # Per-source consent so user can revoke any single ingestion path
    # via `opencomputer consent revoke <id>` without affecting others.
    "ingestion.recent_files": ConsentTier.IMPLICIT,
    "ingestion.git_log": ConsentTier.IMPLICIT,
    "ingestion.calendar": ConsentTier.EXPLICIT,
    "ingestion.browser_history": ConsentTier.EXPLICIT,
    "ingestion.messages": ConsentTier.EXPLICIT,
    "ingestion.browser_extension": ConsentTier.EXPLICIT,
```

- [ ] **Step 1.4: Run test to verify it passes**

Run: `pytest tests/test_capability_taxonomy_ingestion.py -v`
Expected: 2 PASS

- [ ] **Step 1.5: Commit**

```bash
git add opencomputer/agent/consent/capability_taxonomy.py tests/test_capability_taxonomy_ingestion.py
git commit -m "feat(consent): add ingestion.* capability claims for Layered Awareness MVP"
```

---

## Task 2: Layer 0 — IdentityFacts + readers

**Files:**
- Create: `opencomputer/profile_bootstrap/__init__.py`
- Create: `opencomputer/profile_bootstrap/identity_reflex.py`
- Test: `tests/test_profile_bootstrap_identity_reflex.py`

- [ ] **Step 2.1: Create package marker**

Create `opencomputer/profile_bootstrap/__init__.py`:

```python
"""Profile bootstrap — Layered Awareness MVP (Layers 0/1/2/4)."""
```

- [ ] **Step 2.2: Write failing test for IdentityFacts dataclass**

```python
# tests/test_profile_bootstrap_identity_reflex.py
from opencomputer.profile_bootstrap.identity_reflex import IdentityFacts


def test_identity_facts_defaults():
    f = IdentityFacts()
    assert f.name == ""
    assert f.emails == ()
    assert f.github_handle is None


def test_identity_facts_immutable():
    import pytest
    f = IdentityFacts(name="Saksham")
    with pytest.raises(AttributeError):
        f.name = "Other"


def test_identity_facts_with_emails():
    f = IdentityFacts(name="Saksham", emails=("a@b.com", "c@d.com"))
    assert "a@b.com" in f.emails
```

- [ ] **Step 2.3: Run failing test**

Run: `pytest tests/test_profile_bootstrap_identity_reflex.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 2.4: Implement IdentityFacts**

Create `opencomputer/profile_bootstrap/identity_reflex.py`:

```python
"""Layer 0 — Identity Reflex.

Reads what the user has already presented to themselves on the system:
git config, system user, macOS Contacts.app `me` card, browser saved
account email. No consent prompts (every signal is on-disk data the
user authored). Total cost <1s.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IdentityFacts:
    """Output of :func:`gather_identity`. Frozen for safety."""

    name: str = ""
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    github_handle: str | None = None
    city: str | None = None
    primary_language: str = "en_US"
    hostname: str = ""
```

- [ ] **Step 2.5: Verify test passes**

Run: `pytest tests/test_profile_bootstrap_identity_reflex.py -v`
Expected: 3 PASS

- [ ] **Step 2.6: Write failing test for `_read_git_config_emails`**

Append to `tests/test_profile_bootstrap_identity_reflex.py`:

```python
from unittest.mock import patch
from opencomputer.profile_bootstrap.identity_reflex import _read_git_config_emails


def test_read_git_config_emails_returns_email():
    fake_output = "user.email=saksham@example.com\nuser.name=Saksham\n"
    with patch("subprocess.run") as mock:
        mock.return_value.stdout = fake_output
        mock.return_value.returncode = 0
        emails = _read_git_config_emails()
    assert "saksham@example.com" in emails


def test_read_git_config_emails_handles_missing_git():
    with patch("shutil.which", return_value=None):
        emails = _read_git_config_emails()
    assert emails == ()
```

- [ ] **Step 2.7: Run failing test**

Run: `pytest tests/test_profile_bootstrap_identity_reflex.py::test_read_git_config_emails_returns_email -v`
Expected: FAIL `ImportError`

- [ ] **Step 2.8: Implement `_read_git_config_emails`**

Append to `opencomputer/profile_bootstrap/identity_reflex.py`:

```python
def _read_git_config_emails() -> tuple[str, ...]:
    """Read all ``user.email`` values from git's global + system config.

    Returns empty tuple if git is not on PATH or the call fails.
    """
    if shutil.which("git") is None:
        return ()
    try:
        result = subprocess.run(
            ["git", "config", "--list", "--global"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ()
    if result.returncode != 0:
        return ()
    emails: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("user.email="):
            emails.append(line.split("=", 1)[1].strip())
    return tuple(dict.fromkeys(emails))  # de-dup, preserve order
```

- [ ] **Step 2.9: Verify tests pass**

Run: `pytest tests/test_profile_bootstrap_identity_reflex.py -v`
Expected: 5 PASS

- [ ] **Step 2.10: Write failing test for `_read_macos_contacts_me_name`**

Append to test file:

```python
def test_read_macos_contacts_returns_name():
    with patch("subprocess.run") as mock:
        mock.return_value.stdout = "Saksham\n"
        mock.return_value.returncode = 0
        name = _read_macos_contacts_me_name()
    assert name == "Saksham"


def test_read_macos_contacts_returns_none_on_failure():
    with patch("subprocess.run") as mock:
        mock.return_value.returncode = 1
        mock.return_value.stdout = ""
        name = _read_macos_contacts_me_name()
    assert name is None
```

Add the import at the top:
```python
from opencomputer.profile_bootstrap.identity_reflex import _read_macos_contacts_me_name
```

- [ ] **Step 2.11: Implement `_read_macos_contacts_me_name`**

Append to `identity_reflex.py`:

```python
def _read_macos_contacts_me_name() -> str | None:
    """Read the macOS Contacts.app ``me`` card display name.

    Uses AppleScript via ``osascript``. Returns ``None`` on macOS
    without Contacts permissions, on non-macOS, or on script failure.

    The first invocation triggers macOS Privacy & Security dialog
    asking the user to grant Contacts access. We use a 30-second
    timeout (not 3s) so the user has time to respond. Subsequent
    invocations don't prompt.
    """
    if shutil.which("osascript") is None:
        return None
    script = 'tell application "Contacts" to get name of my card'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=30.0,  # generous: first call shows a permission dialog
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None
```

- [ ] **Step 2.12: Verify tests pass**

Run: `pytest tests/test_profile_bootstrap_identity_reflex.py -v`
Expected: 7 PASS

- [ ] **Step 2.13: Write failing test for `gather_identity`**

```python
def test_gather_identity_combines_sources():
    with patch(
        "opencomputer.profile_bootstrap.identity_reflex._read_git_config_emails",
        return_value=("a@b.com",),
    ), patch(
        "opencomputer.profile_bootstrap.identity_reflex._read_macos_contacts_me_name",
        return_value="Saksham",
    ):
        facts = gather_identity()
    assert facts.name == "Saksham"
    assert "a@b.com" in facts.emails
    assert facts.hostname  # set from socket.gethostname()
```

Add import at top:
```python
from opencomputer.profile_bootstrap.identity_reflex import gather_identity
```

- [ ] **Step 2.14: Implement `gather_identity`**

Append to `identity_reflex.py`:

```python
def gather_identity() -> IdentityFacts:
    """Run all Layer 0 readers and return a unified :class:`IdentityFacts`.

    Each reader is independent and best-effort — failures yield empty
    fields rather than raising. The whole call should complete in well
    under one second on a healthy macOS system.
    """
    emails = _read_git_config_emails()
    name = _read_macos_contacts_me_name() or os.environ.get("USER", "")
    return IdentityFacts(
        name=name,
        emails=emails,
        primary_language=os.environ.get("LANG", "en_US").split(".")[0],
        hostname=socket.gethostname(),
    )
```

- [ ] **Step 2.15: Verify tests pass**

Run: `pytest tests/test_profile_bootstrap_identity_reflex.py -v`
Expected: 8 PASS

- [ ] **Step 2.16: Commit**

```bash
git add opencomputer/profile_bootstrap/__init__.py opencomputer/profile_bootstrap/identity_reflex.py tests/test_profile_bootstrap_identity_reflex.py
git commit -m "feat(profile-bootstrap): Layer 0 — Identity Reflex (system + git + Contacts.app)"
```

---

## Task 3: Persistence — write Layer 0/1/2 outputs to user-model graph

**Files:**
- Create: `opencomputer/profile_bootstrap/persistence.py`
- Test: `tests/test_profile_bootstrap_persistence.py`

- [ ] **Step 3.1: Write failing test for `write_identity_to_graph`**

```python
# tests/test_profile_bootstrap_persistence.py
from pathlib import Path

import pytest

from opencomputer.profile_bootstrap.identity_reflex import IdentityFacts
from opencomputer.profile_bootstrap.persistence import write_identity_to_graph
from opencomputer.user_model.store import UserModelStore


@pytest.fixture
def store(tmp_path: Path) -> UserModelStore:
    return UserModelStore(tmp_path / "graph.sqlite")


def test_write_identity_creates_name_node(store):
    facts = IdentityFacts(name="Saksham", emails=("a@b.com",))
    write_identity_to_graph(facts, store=store)
    rows = store.list_nodes(kinds=("identity",))
    names = {n.value for n in rows}
    assert "name: Saksham" in names


def test_write_identity_creates_email_nodes(store):
    facts = IdentityFacts(emails=("a@b.com", "c@d.com"))
    write_identity_to_graph(facts, store=store)
    rows = store.list_nodes(kinds=("identity",))
    emails = {n.value for n in rows}
    assert "email: a@b.com" in emails
    assert "email: c@d.com" in emails


def test_write_identity_idempotent(store):
    facts = IdentityFacts(name="Saksham")
    write_identity_to_graph(facts, store=store)
    write_identity_to_graph(facts, store=store)
    rows = store.list_nodes(kinds=("identity",))
    matching = [n for n in rows if "Saksham" in n.value]
    assert len(matching) == 1  # upsert, not duplicate
```

- [ ] **Step 3.2: Run failing test**

Run: `pytest tests/test_profile_bootstrap_persistence.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3.3: Implement `write_identity_to_graph`**

Create `opencomputer/profile_bootstrap/persistence.py`:

```python
"""Persistence — translate Layer 0/1/2 outputs into F4 user-model edges.

Mirrors :class:`opencomputer.user_model.importer.MotifImporter` shape;
each writer is idempotent via ``UserModelStore.upsert_node``. The
``source`` column on every edge tags provenance for the
F4↔Honcho cycle-prevention path (Phase 4.A schema v2).
"""
from __future__ import annotations

from opencomputer.profile_bootstrap.identity_reflex import IdentityFacts
from opencomputer.user_model.store import UserModelStore


def write_identity_to_graph(
    facts: IdentityFacts,
    *,
    store: UserModelStore | None = None,
) -> int:
    """Persist :class:`IdentityFacts` as Identity nodes.

    Returns the number of nodes written/upserted (excluding edges).
    Idempotent — repeated calls re-upsert without duplicating.
    """
    s = store if store is not None else UserModelStore()
    written = 0
    if facts.name:
        s.upsert_node(kind="identity", value=f"name: {facts.name}", confidence=1.0)
        written += 1
    for email in facts.emails:
        s.upsert_node(kind="identity", value=f"email: {email}", confidence=1.0)
        written += 1
    for phone in facts.phones:
        s.upsert_node(kind="identity", value=f"phone: {phone}", confidence=1.0)
        written += 1
    if facts.github_handle:
        s.upsert_node(kind="identity", value=f"github: {facts.github_handle}", confidence=1.0)
        written += 1
    if facts.city:
        s.upsert_node(kind="identity", value=f"city: {facts.city}", confidence=1.0)
        written += 1
    return written
```

- [ ] **Step 3.4: Verify tests pass**

Run: `pytest tests/test_profile_bootstrap_persistence.py -v`
Expected: 3 PASS

- [ ] **Step 3.5: Add `write_interview_answers_to_graph` (used by Layer 1)**

Append to `tests/test_profile_bootstrap_persistence.py`:

```python
from opencomputer.profile_bootstrap.persistence import write_interview_answers_to_graph


def test_write_interview_creates_preference_nodes(store):
    answers = {
        "current_focus": "Shipping OpenComputer v1.0",
        "tone_preference": "concise and action-first",
        "do_not": "never send emails without confirmation",
    }
    n = write_interview_answers_to_graph(answers, store=store)
    nodes = store.list_nodes()
    values = {x.value for x in nodes}
    assert any("OpenComputer" in v for v in values)
    assert any("concise" in v for v in values)
    assert n >= 3
```

- [ ] **Step 3.6: Implement `write_interview_answers_to_graph`**

Append to `persistence.py`:

```python
def write_interview_answers_to_graph(
    answers: dict[str, str],
    *,
    store: UserModelStore | None = None,
) -> int:
    """Persist Layer 1 quick-interview answers as Preference + Goal nodes.

    Each answer is stored as a node with a question-keyed prefix so the
    raw answer is recoverable. Confidence is 1.0 (user-explicit).
    Returns the number of nodes upserted.
    """
    s = store if store is not None else UserModelStore()
    kind_map = {
        "current_focus": "goal",
        "current_concerns": "goal",
        "tone_preference": "preference",
        "do_not": "preference",
        "context": "attribute",
    }
    written = 0
    for question_key, answer in answers.items():
        if not answer:
            continue
        kind = kind_map.get(question_key, "attribute")
        s.upsert_node(
            kind=kind,
            value=f"{question_key}: {answer}",
            confidence=1.0,
        )
        written += 1
    return written
```

- [ ] **Step 3.7: Verify tests pass**

Run: `pytest tests/test_profile_bootstrap_persistence.py -v`
Expected: 4 PASS

- [ ] **Step 3.8: Commit**

```bash
git add opencomputer/profile_bootstrap/persistence.py tests/test_profile_bootstrap_persistence.py
git commit -m "feat(profile-bootstrap): persistence — write Layer 0/1 outputs to user-model graph"
```

---

## Task 4: Layer 1 — Quick Interview

**Files:**
- Create: `opencomputer/profile_bootstrap/quick_interview.py`
- Test: `tests/test_profile_bootstrap_quick_interview.py`

- [ ] **Step 4.1: Write failing test for question rendering**

```python
# tests/test_profile_bootstrap_quick_interview.py
from opencomputer.profile_bootstrap.identity_reflex import IdentityFacts
from opencomputer.profile_bootstrap.quick_interview import (
    QUICK_INTERVIEW_QUESTIONS,
    render_questions,
    parse_answers,
)


def test_default_question_set_has_five():
    assert len(QUICK_INTERVIEW_QUESTIONS) == 5


def test_render_questions_personalizes_with_name():
    facts = IdentityFacts(name="Saksham")
    rendered = render_questions(facts)
    assert "Saksham" in rendered[0]  # greeting includes name


def test_render_questions_anonymous_when_no_name():
    facts = IdentityFacts()
    rendered = render_questions(facts)
    assert "Hi!" in rendered[0] or "Hello" in rendered[0]


def test_parse_answers_returns_dict():
    raw = ["focus: stocks", "concerns: timing", "concise", "no emails", ""]
    parsed = parse_answers(raw)
    assert parsed["current_focus"] == "focus: stocks"
    assert parsed["tone_preference"] == "concise"
    assert "context" not in parsed or parsed["context"] == ""
```

- [ ] **Step 4.2: Run failing test**

Run: `pytest tests/test_profile_bootstrap_quick_interview.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 4.3: Implement question registry + rendering + parser**

Create `opencomputer/profile_bootstrap/quick_interview.py`:

```python
"""Layer 1 — Quick Interview.

Renders 5 install-time questions personalized using Layer 0 identity,
parses the user's answers back into a structured dict that
:func:`opencomputer.profile_bootstrap.persistence.write_interview_answers_to_graph`
can persist.

The CLI orchestration lives in :mod:`opencomputer.cli_profile`; this
module is testable in isolation.
"""
from __future__ import annotations

from opencomputer.profile_bootstrap.identity_reflex import IdentityFacts

#: Ordered tuple of (key, prompt-template) pairs. Order matters — the
#: CLI presents them sequentially. Adding a new question is fine;
#: removing or reordering breaks the parser contract.
QUICK_INTERVIEW_QUESTIONS: tuple[tuple[str, str], ...] = (
    (
        "current_focus",
        "What are you working on this week? (one sentence is fine)",
    ),
    (
        "current_concerns",
        "Anything on your mind right now I should know?",
    ),
    (
        "tone_preference",
        "How do you prefer responses — concise/action-first or thorough?",
    ),
    (
        "do_not",
        "Anything I should NOT do without asking? (e.g. \"never send emails without confirming\")",
    ),
    (
        "context",
        "Anything else about you that would help me help you?",
    ),
)


def render_questions(facts: IdentityFacts) -> list[str]:
    """Return [greeting, q1, q2, ...] strings ready for the CLI to present."""
    if facts.name:
        greeting = (
            f"Hi {facts.name}! I'm OpenComputer — your local agent.\n"
            "Five quick questions so I can be useful from the get-go:"
        )
    else:
        greeting = (
            "Hi! I'm OpenComputer — your local agent.\n"
            "Five quick questions so I can be useful from the get-go:"
        )
    return [greeting, *(q for _, q in QUICK_INTERVIEW_QUESTIONS)]


def parse_answers(raw_answers: list[str]) -> dict[str, str]:
    """Map raw answer strings (in question order) back to keyed dict.

    Empty answers are dropped. Extra answers beyond the registry are
    discarded — the CLI is the contract enforcer for length.
    """
    parsed: dict[str, str] = {}
    for (key, _), answer in zip(QUICK_INTERVIEW_QUESTIONS, raw_answers):
        cleaned = answer.strip()
        if cleaned:
            parsed[key] = cleaned
    return parsed
```

- [ ] **Step 4.4: Verify tests pass**

Run: `pytest tests/test_profile_bootstrap_quick_interview.py -v`
Expected: 4 PASS

- [ ] **Step 4.5: Commit**

```bash
git add opencomputer/profile_bootstrap/quick_interview.py tests/test_profile_bootstrap_quick_interview.py
git commit -m "feat(profile-bootstrap): Layer 1 — Quick Interview question set + parser"
```

---

## Task 5: Bridge CLI subcommands (`opencomputer profile bridge ...`)

**Audit-driven change:** the original Task 5 (Ollama LLM extractor) was dead code in MVP — the orchestrator never called it. Moved to V2 alongside Background Deepening. This task replaces it: the browser-bridge plugin needs a way to be started/stopped/queried, and the README in Task 11 references commands that must exist.

**Files:**
- Modify: `opencomputer/cli_profile.py`
- Create: `opencomputer/profile_bootstrap/bridge_state.py`
- Test: `tests/test_cli_profile_bridge.py`

- [ ] **Step 5.1: Write failing test for `bridge token` subcommand**

```python
# tests/test_cli_profile_bridge.py
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_profile import profile_app

runner = CliRunner()


def test_bridge_token_creates_and_prints(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(profile_app, ["bridge", "token"])
    assert result.exit_code == 0
    # Token is URL-safe base64-ish, length > 32
    out = result.stdout.strip().splitlines()[-1]
    assert len(out) >= 32


def test_bridge_token_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    first = runner.invoke(profile_app, ["bridge", "token"]).stdout.strip().splitlines()[-1]
    second = runner.invoke(profile_app, ["bridge", "token"]).stdout.strip().splitlines()[-1]
    assert first == second  # second call returns the existing token


def test_bridge_token_rotate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    first = runner.invoke(profile_app, ["bridge", "token"]).stdout.strip().splitlines()[-1]
    second = runner.invoke(
        profile_app, ["bridge", "token", "--rotate"]
    ).stdout.strip().splitlines()[-1]
    assert first != second
```

- [ ] **Step 5.2: Run failing test**

Run: `pytest tests/test_cli_profile_bridge.py -v`
Expected: FAIL — no `bridge` subcommand

- [ ] **Step 5.3: Implement bridge state module**

Create `opencomputer/profile_bootstrap/bridge_state.py`:

```python
"""Browser-bridge state — token storage, port config.

State lives at ``<profile_home>/profile_bootstrap/bridge.json``:
``{"token": "<url-safe-32-bytes>", "port": 18791}``.

Tokens are generated via :func:`secrets.token_urlsafe(32)`. Rotation
is a destructive operation — old token is immediately invalid.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from opencomputer.agent.config import _home


@dataclass(frozen=True, slots=True)
class BridgeState:
    """Serialised browser-bridge config."""

    token: str = ""
    port: int = 18791


def state_path() -> Path:
    """Resolve the bridge state file path under the active profile home."""
    return _home() / "profile_bootstrap" / "bridge.json"


def load_or_create(*, rotate: bool = False) -> BridgeState:
    """Read existing state or generate a fresh token."""
    p = state_path()
    if p.exists() and not rotate:
        try:
            data = json.loads(p.read_text())
            return BridgeState(
                token=str(data.get("token", "")),
                port=int(data.get("port", 18791)),
            )
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    state = BridgeState(token=secrets.token_urlsafe(32), port=18791)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(state)))
    return state
```

- [ ] **Step 5.4: Add `bridge` subcommand group in `cli_profile.py`**

In `opencomputer/cli_profile.py`, add at module scope (alongside `profile_app`):

```python
bridge_app = typer.Typer(
    help="Browser-bridge controls (Layer 4 of Layered Awareness)",
)
profile_app.add_typer(bridge_app, name="bridge")


@bridge_app.command("token")
def bridge_token(
    rotate: bool = typer.Option(
        False, "--rotate", help="Generate a fresh token (invalidates old)"
    ),
) -> None:
    """Print the bridge auth token. Generates one on first call."""
    from opencomputer.profile_bootstrap.bridge_state import load_or_create

    state = load_or_create(rotate=rotate)
    typer.echo(
        "Paste this into the browser extension's DevTools console:\n"
        f"  chrome.storage.local.set({{ ocBridgeToken: '{state.token}' }})\n"
    )
    typer.echo(state.token)


@bridge_app.command("status")
def bridge_status() -> None:
    """Show bridge config + whether port is reachable."""
    import socket
    from opencomputer.profile_bootstrap.bridge_state import load_or_create

    state = load_or_create()
    typer.echo(f"Token configured: {'yes' if state.token else 'no'}")
    typer.echo(f"Bind port: {state.port}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        sock.connect(("127.0.0.1", state.port))
        typer.echo("Listener: REACHABLE")
    except (OSError, socket.timeout):
        typer.echo("Listener: NOT REACHABLE (run 'opencomputer profile bridge start')")
    finally:
        sock.close()
```

- [ ] **Step 5.5: Verify tests pass**

Run: `pytest tests/test_cli_profile_bridge.py -v`
Expected: 3 PASS

- [ ] **Step 5.6: Commit**

```bash
git add opencomputer/profile_bootstrap/bridge_state.py opencomputer/cli_profile.py tests/test_cli_profile_bridge.py
git commit -m "feat(cli): opencomputer profile bridge token/status"
```

---

## Task 6: Layer 2 part A — recent files + git log scan

**Files:**
- Create: `opencomputer/profile_bootstrap/recent_scan.py`
- Test: `tests/test_profile_bootstrap_recent_scan.py`

- [ ] **Step 6.1: Write failing test for `scan_recent_files`**

```python
# tests/test_profile_bootstrap_recent_scan.py
import time
from pathlib import Path

import pytest

from opencomputer.profile_bootstrap.recent_scan import (
    scan_recent_files,
    scan_git_log,
    RecentFileSummary,
    GitCommitSummary,
)


def test_scan_recent_files_returns_recent(tmp_path: Path):
    f = tmp_path / "doc.md"
    f.write_text("Hello world")
    found = scan_recent_files(roots=[tmp_path], days=7)
    assert len(found) == 1
    assert found[0].path == str(f.resolve())
    assert found[0].size_bytes > 0


def test_scan_recent_files_skips_old(tmp_path: Path):
    f = tmp_path / "old.md"
    f.write_text("old content")
    old_time = time.time() - 30 * 24 * 3600  # 30 days ago
    import os
    os.utime(f, (old_time, old_time))
    found = scan_recent_files(roots=[tmp_path], days=7)
    assert found == []


def test_scan_recent_files_skips_dotfiles(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("SECRET=abc")
    found = scan_recent_files(roots=[tmp_path], days=7)
    assert found == []


def test_scan_recent_files_returns_empty_when_root_missing(tmp_path: Path):
    found = scan_recent_files(roots=[tmp_path / "nope"], days=7)
    assert found == []
```

- [ ] **Step 6.2: Run failing test**

Run: `pytest tests/test_profile_bootstrap_recent_scan.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 6.3: Implement file scan + dataclasses**

Create `opencomputer/profile_bootstrap/recent_scan.py`:

```python
"""Layer 2 — Recent Context Scan.

One-shot ingestion of "what's happening this week" so the agent has
current context, not just identity. Sources:

* Files modified in user-allowed dirs (this module)
* Git log across detected repos (this module)
* Calendar events (next 7 days) — see ``calendar_reader.py``
* Browser history — see ``browser_history.py``

Outputs are :class:`RecentFileSummary` / :class:`GitCommitSummary` /
``CalendarEventSummary`` / ``BrowserVisitSummary`` records that the
orchestrator (Task 11) feeds into the LLM extractor and then into the
F4 user-model graph.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

#: Filenames + extensions skipped by the recent-files walker. Belt-
#: and-suspenders alongside dotfile-skip — secrets that happen to
#: live in plain-named files don't get ingested into motifs.
_SKIP_EXTENSIONS = frozenset({".env", ".key", ".pem", ".p12", ".pgp", ".asc"})
_SKIP_NAMES = frozenset({".env", ".envrc", "id_rsa", "id_ed25519"})


@dataclass(frozen=True, slots=True)
class RecentFileSummary:
    """Metadata-only summary of a recently-modified file."""

    path: str
    mtime: float
    size_bytes: int


@dataclass(frozen=True, slots=True)
class GitCommitSummary:
    """One-line git commit summary."""

    repo_path: str
    sha: str
    timestamp: float
    subject: str
    author_email: str


def scan_recent_files(
    *,
    roots: list[Path],
    days: int = 7,
    max_files: int = 1000,
) -> list[RecentFileSummary]:
    """Walk ``roots`` and return files modified in the last ``days``.

    Skips dotfiles, symlinks, files in :data:`_SKIP_NAMES`, and files
    with extensions in :data:`_SKIP_EXTENSIONS`. Caps at ``max_files``
    to keep the scan time bounded.
    """
    cutoff = time.time() - (days * 24 * 3600)
    out: list[RecentFileSummary] = []
    for root in roots:
        if not root.exists():
            continue
        try:
            for f in root.rglob("*"):
                if not f.is_file() or f.is_symlink():
                    continue
                if f.name.startswith("."):
                    continue
                if f.name in _SKIP_NAMES:
                    continue
                if f.suffix.lower() in _SKIP_EXTENSIONS:
                    continue
                try:
                    stat = f.stat()
                except OSError:
                    continue
                if stat.st_mtime < cutoff:
                    continue
                out.append(
                    RecentFileSummary(
                        path=str(f.resolve()),
                        mtime=stat.st_mtime,
                        size_bytes=stat.st_size,
                    )
                )
                if len(out) >= max_files:
                    return out
        except (OSError, PermissionError):
            continue
    return out


def scan_git_log(
    *,
    repo_paths: list[Path],
    days: int = 7,
    max_per_repo: int = 200,
) -> list[GitCommitSummary]:
    """Run ``git log`` in each repo and return commits in the last ``days``."""
    if shutil.which("git") is None:
        return []
    since = f"{days}.days.ago"
    out: list[GitCommitSummary] = []
    for repo in repo_paths:
        if not (repo / ".git").exists():
            continue
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "log",
                    f"--since={since}",
                    f"--max-count={max_per_repo}",
                    "--pretty=format:%H%x09%at%x09%ae%x09%s",
                ],
                capture_output=True,
                text=True,
                timeout=10.0,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            sha, ts, email, subject = parts
            try:
                ts_f = float(ts)
            except ValueError:
                continue
            out.append(
                GitCommitSummary(
                    repo_path=str(repo.resolve()),
                    sha=sha[:12],
                    timestamp=ts_f,
                    subject=subject[:200],
                    author_email=email[:128],
                )
            )
    return out
```

- [ ] **Step 6.4: Verify tests pass**

Run: `pytest tests/test_profile_bootstrap_recent_scan.py -v`
Expected: 4 PASS

- [ ] **Step 6.5: Add git log test**

Append to test file:

```python
def test_scan_git_log_returns_commits(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # marker only — we mock subprocess

    fake_log = (
        "abc123def456\t1714000000\tsaksham@example.com\tInitial commit\n"
        "def456ghi789\t1714086400\tsaksham@example.com\tSecond commit\n"
    )
    import subprocess as _sp

    class _R:
        returncode = 0
        stdout = fake_log

    def fake_run(*args, **kwargs):
        return _R()

    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr(
        "opencomputer.profile_bootstrap.recent_scan.subprocess.run", fake_run
    )

    commits = scan_git_log(repo_paths=[repo], days=7)
    assert len(commits) == 2
    assert commits[0].sha == "abc123def456"
    assert commits[0].subject == "Initial commit"


def test_scan_git_log_skips_non_repo(tmp_path: Path):
    not_repo = tmp_path / "plain_dir"
    not_repo.mkdir()
    commits = scan_git_log(repo_paths=[not_repo], days=7)
    assert commits == []
```

- [ ] **Step 6.6: Verify tests pass**

Run: `pytest tests/test_profile_bootstrap_recent_scan.py -v`
Expected: 6 PASS

- [ ] **Step 6.7: Commit**

```bash
git add opencomputer/profile_bootstrap/recent_scan.py tests/test_profile_bootstrap_recent_scan.py
git commit -m "feat(profile-bootstrap): Layer 2 part A — recent files + git log scan"
```

---

## Task 7: Layer 2 part B — calendar reader (PyObjC EventKit)

**Files:**
- Create: `opencomputer/profile_bootstrap/calendar_reader.py`
- Test: `tests/test_profile_bootstrap_calendar_reader.py`

- [ ] **Step 7.1: Write failing test (mocks PyObjC)**

```python
# tests/test_profile_bootstrap_calendar_reader.py
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.profile_bootstrap.calendar_reader import (
    CalendarEventSummary,
    read_upcoming_events,
)


def test_calendar_event_summary_defaults():
    e = CalendarEventSummary()
    assert e.title == ""
    assert e.location == ""


def test_read_upcoming_events_returns_empty_on_pyobjc_missing():
    with patch(
        "opencomputer.profile_bootstrap.calendar_reader._import_event_kit",
        side_effect=ImportError(),
    ):
        events = read_upcoming_events(days=7)
    assert events == []


def test_read_upcoming_events_returns_empty_when_access_denied():
    fake_ek = MagicMock()
    # Status 2 = Denied. Not in _AUTHORIZED_STATUSES (3, 4, 5) so
    # the reader must short-circuit to [].
    fake_ek.EKEventStore.alloc.return_value.init.return_value.\
        authorizationStatusForEntityType_.return_value = 2
    with patch(
        "opencomputer.profile_bootstrap.calendar_reader._import_event_kit",
        return_value=fake_ek,
    ):
        events = read_upcoming_events(days=7)
    assert events == []
```

- [ ] **Step 7.2: Run failing test**

Run: `pytest tests/test_profile_bootstrap_calendar_reader.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 7.3: Implement calendar reader**

Create `opencomputer/profile_bootstrap/calendar_reader.py`:

```python
"""Layer 2 helper — read upcoming calendar events via PyObjC EventKit.

Returns ``[]`` when:
- Not on macOS (PyObjC import fails)
- User has not granted Calendar access in System Settings
- EventKit authorization status is anything other than ``Authorized``

The CLI-level consent gate (``ingestion.calendar``, EXPLICIT) is the
*authorization* layer. The macOS Privacy & Security pane is a
separate, OS-level grant that we cannot bypass.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

_log = logging.getLogger("opencomputer.profile_bootstrap.calendar")


@dataclass(frozen=True, slots=True)
class CalendarEventSummary:
    """One calendar event, summary only — no attendee emails."""

    title: str = ""
    start: float = 0.0  # epoch seconds
    end: float = 0.0
    location: str = ""
    calendar_name: str = ""


def _import_event_kit() -> Any:
    """Indirect import so tests can patch easily."""
    import EventKit  # type: ignore[import-not-found]
    return EventKit


#: Authorized statuses for EKEntityType.event. We accept any status
#: that lets us read events. Numeric values match Apple's
#: ``EKAuthorizationStatus`` enum (1=Restricted, 2=Denied, 3=Authorized,
#: 4=WriteOnly, 5=FullAccess as of macOS 14+). Using a set rather than
#: a single magic number keeps the check forward-compatible.
_AUTHORIZED_STATUSES = frozenset({3, 4, 5})


def read_upcoming_events(*, days: int = 7) -> list[CalendarEventSummary]:
    """Read calendar events for the next ``days`` from macOS Calendar.

    Best-effort. Returns ``[]`` on any failure path.
    """
    try:
        ek = _import_event_kit()
    except ImportError:
        _log.debug("EventKit not importable — non-macOS or PyObjC missing")
        return []

    try:
        store = ek.EKEventStore.alloc().init()
        # EKEntityTypeEvent is integer 0 in Apple's enum. We pass it
        # directly to avoid coupling to a PyObjC-exposed constant
        # name (which has changed across PyObjC versions).
        status = store.authorizationStatusForEntityType_(0)
        if int(status) not in _AUTHORIZED_STATUSES:
            _log.info("Calendar access not granted (status=%s)", status)
            return []

        from Foundation import NSDate  # type: ignore[import-not-found]

        now = NSDate.date()
        end = NSDate.dateWithTimeIntervalSinceNow_(days * 24 * 3600)
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            now, end, None,
        )
        events = store.eventsMatchingPredicate_(predicate) or []
    except Exception as exc:  # noqa: BLE001
        _log.warning("EventKit read failed: %s", exc)
        return []

    out: list[CalendarEventSummary] = []
    for ev in events:
        try:
            out.append(
                CalendarEventSummary(
                    title=str(ev.title() or "")[:200],
                    start=float(ev.startDate().timeIntervalSince1970()),
                    end=float(ev.endDate().timeIntervalSince1970()),
                    location=str(ev.location() or "")[:200],
                    calendar_name=str(ev.calendar().title() or "")[:100],
                )
            )
        except Exception:  # noqa: BLE001
            continue
    return out
```

- [ ] **Step 7.4: Verify tests pass**

Run: `pytest tests/test_profile_bootstrap_calendar_reader.py -v`
Expected: 3 PASS

- [ ] **Step 7.5: Commit**

```bash
git add opencomputer/profile_bootstrap/calendar_reader.py tests/test_profile_bootstrap_calendar_reader.py
git commit -m "feat(profile-bootstrap): Layer 2 part B — calendar reader (PyObjC EventKit)"
```

---

## Task 8: Layer 2 part C — browser history reader

**Files:**
- Create: `opencomputer/profile_bootstrap/browser_history.py`
- Test: `tests/test_profile_bootstrap_browser_history.py`

- [ ] **Step 8.1: Write failing test using real SQLite fixture**

```python
# tests/test_profile_bootstrap_browser_history.py
import sqlite3
import time
from pathlib import Path

from opencomputer.profile_bootstrap.browser_history import (
    BrowserVisitSummary,
    read_chrome_history,
)


def _build_chrome_db(path: Path, urls: list[tuple[str, str, int]]) -> None:
    """Build a minimal Chrome-shaped History DB. urls = [(url, title, visit_seconds)]."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE urls(
            id INTEGER PRIMARY KEY,
            url TEXT,
            title TEXT,
            visit_count INTEGER DEFAULT 0,
            last_visit_time INTEGER DEFAULT 0
        );
        """
    )
    # Chrome encodes time as microseconds since 1601-01-01.
    for i, (u, t, secs) in enumerate(urls):
        chrome_time = (secs + 11644473600) * 1_000_000
        conn.execute(
            "INSERT INTO urls(id, url, title, visit_count, last_visit_time) VALUES (?, ?, ?, ?, ?)",
            (i + 1, u, t, 1, chrome_time),
        )
    conn.commit()
    conn.close()


def test_read_chrome_history_recent_only(tmp_path: Path):
    db = tmp_path / "History"
    now = int(time.time())
    _build_chrome_db(
        db,
        [
            ("https://example.com", "Example", now - 60),
            ("https://old.com", "Old", now - 30 * 24 * 3600),
        ],
    )
    visits = read_chrome_history(history_db=db, days=7)
    assert len(visits) == 1
    assert visits[0].url == "https://example.com"


def test_read_chrome_history_returns_empty_when_missing(tmp_path: Path):
    visits = read_chrome_history(history_db=tmp_path / "nope", days=7)
    assert visits == []
```

- [ ] **Step 8.2: Run failing test**

Run: `pytest tests/test_profile_bootstrap_browser_history.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 8.3: Implement browser history reader**

Create `opencomputer/profile_bootstrap/browser_history.py`:

```python
"""Layer 2 helper — Chrome / Brave / Edge history reader (SQLite).

Each Chromium-family browser stores history at:
``~/Library/Application Support/<Vendor>/<Profile>/History`` on macOS.

Reading requires the file to be unlocked — Chromium holds a SQLite
exclusive lock while running. We copy the DB to a tempfile first
(file copy bypasses the SQLite lock on macOS APFS), then read.

Safari uses ``~/Library/Safari/History.db`` (different schema —
not in MVP). Firefox uses ``places.sqlite`` (different schema — also
not in MVP).
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("opencomputer.profile_bootstrap.browser_history")


@dataclass(frozen=True, slots=True)
class BrowserVisitSummary:
    """One URL visit. URL + title only — no page content in MVP."""

    url: str = ""
    title: str = ""
    visit_time: float = 0.0  # epoch seconds
    browser: str = ""


def read_chrome_history(
    *,
    history_db: Path | None = None,
    days: int = 7,
    max_visits: int = 2000,
) -> list[BrowserVisitSummary]:
    """Read Chrome-format history. ``history_db`` defaults to the macOS path."""
    if history_db is None:
        history_db = (
            Path.home()
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "Default"
            / "History"
        )
    if not history_db.exists():
        return []

    cutoff_secs = time.time() - (days * 24 * 3600)
    # Chrome time = microseconds since 1601-01-01; convert cutoff.
    cutoff_chrome = int((cutoff_secs + 11644473600) * 1_000_000)

    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / "History"
        try:
            shutil.copyfile(history_db, copy_path)
        except OSError as exc:
            _log.warning("Could not copy Chrome History (%s): %s", history_db, exc)
            return []
        try:
            conn = sqlite3.connect(f"file:{copy_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            return []
        try:
            cur = conn.execute(
                "SELECT url, title, last_visit_time "
                "FROM urls "
                "WHERE last_visit_time >= ? "
                "ORDER BY last_visit_time DESC "
                "LIMIT ?",
                (cutoff_chrome, max_visits),
            )
            rows = cur.fetchall()
        except sqlite3.DatabaseError:
            return []
        finally:
            conn.close()

    out: list[BrowserVisitSummary] = []
    for url, title, visit_time in rows:
        secs = (visit_time / 1_000_000) - 11644473600
        out.append(
            BrowserVisitSummary(
                url=str(url or "")[:1024],
                title=str(title or "")[:256],
                visit_time=float(secs),
                browser="chrome",
            )
        )
    return out
```

- [ ] **Step 8.4: Verify tests pass**

Run: `pytest tests/test_profile_bootstrap_browser_history.py -v`
Expected: 2 PASS

- [ ] **Step 8.5: Commit**

```bash
git add opencomputer/profile_bootstrap/browser_history.py tests/test_profile_bootstrap_browser_history.py
git commit -m "feat(profile-bootstrap): Layer 2 part C — Chrome browser history reader"
```

---

## Task 9: Bootstrap orchestrator (Layers 0-2 sequenced)

**Files:**
- Create: `opencomputer/profile_bootstrap/orchestrator.py`
- Test: `tests/test_profile_bootstrap_orchestrator.py`

- [ ] **Step 9.1: Write failing test**

```python
# tests/test_profile_bootstrap_orchestrator.py
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.profile_bootstrap.identity_reflex import IdentityFacts
from opencomputer.profile_bootstrap.orchestrator import (
    BootstrapResult,
    run_bootstrap,
)
from opencomputer.user_model.store import UserModelStore


@pytest.fixture
def store(tmp_path: Path) -> UserModelStore:
    return UserModelStore(tmp_path / "graph.sqlite")


def test_bootstrap_runs_layers_in_order_and_returns_result(store):
    fake_facts = IdentityFacts(name="Saksham", emails=("s@e.com",))
    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=fake_facts,
    ):
        result = run_bootstrap(
            interview_answers={
                "current_focus": "OpenComputer v1.0",
                "tone_preference": "concise",
            },
            scan_roots=[],
            git_repos=[],
            include_calendar=False,
            include_browser_history=False,
            store=store,
        )
    assert isinstance(result, BootstrapResult)
    assert result.identity_nodes_written >= 1
    assert result.interview_nodes_written == 2


def test_bootstrap_marks_complete(store, tmp_path: Path):
    marker = tmp_path / "bootstrap_complete.json"
    with patch(
        "opencomputer.profile_bootstrap.orchestrator.gather_identity",
        return_value=IdentityFacts(),
    ):
        run_bootstrap(
            interview_answers={},
            scan_roots=[],
            git_repos=[],
            include_calendar=False,
            include_browser_history=False,
            store=store,
            marker_path=marker,
        )
    assert marker.exists()
```

- [ ] **Step 9.2: Run failing test**

Run: `pytest tests/test_profile_bootstrap_orchestrator.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 9.3: Implement orchestrator**

Create `opencomputer/profile_bootstrap/orchestrator.py`:

```python
"""Bootstrap orchestrator — sequences Layers 0/1/2 for a single install run.

Called by the ``opencomputer profile bootstrap`` CLI subcommand. Each
layer is independent and best-effort — a failure in one does not
block subsequent layers.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from opencomputer.profile_bootstrap.identity_reflex import gather_identity
from opencomputer.profile_bootstrap.persistence import (
    write_identity_to_graph,
    write_interview_answers_to_graph,
)
from opencomputer.profile_bootstrap.recent_scan import (
    scan_git_log,
    scan_recent_files,
)
from opencomputer.user_model.store import UserModelStore

_log = logging.getLogger("opencomputer.profile_bootstrap.orchestrator")


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Summary of one bootstrap pass for CLI display + audit log."""

    identity_nodes_written: int = 0
    interview_nodes_written: int = 0
    files_scanned: int = 0
    git_commits_scanned: int = 0
    elapsed_seconds: float = 0.0


def run_bootstrap(
    *,
    interview_answers: dict[str, str],
    scan_roots: list[Path],
    git_repos: list[Path],
    include_calendar: bool = True,
    include_browser_history: bool = True,
    store: UserModelStore | None = None,
    marker_path: Path | None = None,
) -> BootstrapResult:
    """Run all MVP bootstrap layers and persist outputs to the user-model graph.

    Marker write at the end is the "bootstrap completed" signal the CLI
    checks on subsequent runs.
    """
    started = time.monotonic()
    s = store if store is not None else UserModelStore()

    # Layer 0
    facts = gather_identity()
    identity_n = write_identity_to_graph(facts, store=s)

    # Layer 1
    interview_n = write_interview_answers_to_graph(interview_answers, store=s)

    # Layer 2 — files
    files = scan_recent_files(roots=scan_roots, days=7) if scan_roots else []

    # Layer 2 — git
    commits = scan_git_log(repo_paths=git_repos, days=7) if git_repos else []

    # Layer 2 — calendar / browser are passed through here in MVP only as
    # counters; the LLM-extraction-and-importer wiring lands in V2 to
    # avoid blocking MVP on Ollama install. For MVP we simply log + count.
    if include_calendar:
        try:
            from opencomputer.profile_bootstrap.calendar_reader import (
                read_upcoming_events,
            )
            _ = read_upcoming_events(days=7)
        except Exception:  # noqa: BLE001
            _log.exception("calendar read failed")

    if include_browser_history:
        try:
            from opencomputer.profile_bootstrap.browser_history import (
                read_chrome_history,
            )
            _ = read_chrome_history(days=7)
        except Exception:  # noqa: BLE001
            _log.exception("browser history read failed")

    elapsed = time.monotonic() - started
    result = BootstrapResult(
        identity_nodes_written=identity_n,
        interview_nodes_written=interview_n,
        files_scanned=len(files),
        git_commits_scanned=len(commits),
        elapsed_seconds=elapsed,
    )

    if marker_path is not None:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps({**asdict(result), "completed_at": time.time()}))

    return result
```

- [ ] **Step 9.4: Verify tests pass**

Run: `pytest tests/test_profile_bootstrap_orchestrator.py -v`
Expected: 2 PASS

- [ ] **Step 9.5: Commit**

```bash
git add opencomputer/profile_bootstrap/orchestrator.py tests/test_profile_bootstrap_orchestrator.py
git commit -m "feat(profile-bootstrap): orchestrator sequences Layers 0/1/2"
```

---

## Task 10: Browser-bridge plugin (Layer 4 minimal — Python listener)

**Files:**
- Create: `extensions/browser-bridge/plugin.json`
- Create: `extensions/browser-bridge/plugin.py`
- Create: `extensions/browser-bridge/adapter.py`
- Test: `tests/test_browser_bridge.py`

- [ ] **Step 10.1: Write failing test for HTTP listener**

`pyproject.toml` has `asyncio_mode = "auto"` so async test functions don't need a decorator — they run as asyncio tests automatically.

```python
# tests/test_browser_bridge.py
import asyncio

import aiohttp


async def test_browser_bridge_accepts_post_and_publishes_event():
    from extensions.browser_bridge.adapter import BrowserBridgeAdapter
    from opencomputer.ingestion.bus import TypedEventBus

    bus = TypedEventBus()
    received: list = []

    def handler(ev) -> None:
        received.append(ev)

    bus.subscribe("browser_visit", handler)

    adapter = BrowserBridgeAdapter(bus=bus, port=18791, token="test-token")
    runner = await adapter.start()
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "url": "https://example.com",
                "title": "Example",
                "visit_time": 1714086400.0,
            }
            async with session.post(
                "http://127.0.0.1:18791/browser-event",
                json=payload,
                headers={"Authorization": "Bearer test-token"},
            ) as resp:
                assert resp.status == 200
        # event bus fanout is sync — give it a tick to settle.
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].event_type == "browser_visit"
        assert received[0].metadata["url"] == "https://example.com"
    finally:
        await runner.cleanup()


async def test_browser_bridge_rejects_missing_token():
    from extensions.browser_bridge.adapter import BrowserBridgeAdapter
    from opencomputer.ingestion.bus import TypedEventBus

    bus = TypedEventBus()
    adapter = BrowserBridgeAdapter(bus=bus, port=18792, token="real-token")
    runner = await adapter.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://127.0.0.1:18792/browser-event",
                json={"url": "x"},
            ) as resp:
                assert resp.status == 401
    finally:
        await runner.cleanup()


async def test_browser_bridge_handles_port_in_use():
    """If the port is already bound, raise a clean OSError with actionable msg."""
    from extensions.browser_bridge.adapter import BrowserBridgeAdapter
    from opencomputer.ingestion.bus import TypedEventBus

    bus = TypedEventBus()
    a = BrowserBridgeAdapter(bus=bus, port=18793, token="t")
    runner_a = await a.start()
    try:
        b = BrowserBridgeAdapter(bus=bus, port=18793, token="t")
        # Second bind on same port must raise OSError; we don't want
        # the adapter to silently swallow the bind failure.
        import pytest

        with pytest.raises(OSError):
            await b.start()
    finally:
        await runner_a.cleanup()
```

- [ ] **Step 10.2: Run failing test**

Run: `pytest tests/test_browser_bridge.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 10.3: Add module aliasing for hyphenated extension dir**

The extension dir is `browser-bridge` (hyphenated, plugin convention) but Python imports use `browser_bridge`. The repo already has the alias pattern in `tests/conftest.py` for other extensions. Add:

In `tests/conftest.py`, find the function `_register_aws_bedrock_provider_alias` near the bottom; copy its shape into a new function `_register_browser_bridge_alias` that aliases `extensions.browser_bridge` → `extensions/browser-bridge/`. Then call it from the bottom of the file alongside the existing aliases.

```python
def _register_browser_bridge_alias() -> None:
    """Register extensions.browser_bridge → extensions/browser-bridge/."""
    _ensure_extensions_pkg()
    _BB_DIR = _EXT_DIR / "browser-bridge"

    if "extensions.browser_bridge" not in sys.modules:
        mod = types.ModuleType("extensions.browser_bridge")
        mod.__path__ = [str(_BB_DIR)]
        mod.__package__ = "extensions.browser_bridge"
        sys.modules["extensions.browser_bridge"] = mod

    for sub in ("adapter", "plugin"):
        full_name = f"extensions.browser_bridge.{sub}"
        if full_name not in sys.modules:
            init = _BB_DIR / f"{sub}.py"
            if not init.exists():
                continue
            spec = importlib.util.spec_from_file_location(full_name, str(init))
            if spec is None or spec.loader is None:
                continue
            sub_mod = importlib.util.module_from_spec(spec)
            sub_mod.__package__ = "extensions.browser_bridge"
            sys.modules[full_name] = sub_mod
            # Don't exec — tests control when to load
```

Then add `_register_browser_bridge_alias()` after `_register_aws_bedrock_provider_alias()` at the bottom of the file.

- [ ] **Step 10.4: Implement browser-bridge plugin manifest**

Create `extensions/browser-bridge/plugin.json`:

```json
{
  "id": "browser-bridge",
  "name": "Browser Bridge",
  "version": "0.1.0",
  "description": "Receives tab activity from the OpenComputer browser extension and fans events into the F2 SignalEvent bus. Layer 4 (minimal) of Layered Awareness.",
  "author": "OpenComputer Contributors",
  "license": "MIT",
  "kind": "tools",
  "entry": "plugin",
  "tool_names": []
}
```

- [ ] **Step 10.5: Implement adapter**

Create `extensions/browser-bridge/adapter.py`:

```python
"""Browser-bridge adapter — aiohttp endpoint receiving Chrome-extension POSTs.

Exposes ``POST /browser-event`` on a configurable port (default 18791).
Bearer-token auth (token regenerated per profile install). Validates
payload shape, then publishes a :class:`plugin_sdk.ingestion.SignalEvent`
with ``event_type="browser_visit"`` to the in-process bus.

Cross-origin: allows extensions to POST from any origin — the bearer
token is the auth gate.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from aiohttp import web

from opencomputer.ingestion.bus import TypedEventBus
from plugin_sdk.ingestion import SignalEvent

_log = logging.getLogger("extensions.browser_bridge")


def generate_token() -> str:
    """Generate a 32-byte URL-safe token for browser-extension auth."""
    return secrets.token_urlsafe(32)


class BrowserBridgeAdapter:
    """HTTP listener bound to localhost. Publishes events into a TypedEventBus.

    The :meth:`start` method propagates :class:`OSError` raised by
    ``aiohttp`` when the port is already bound — callers should surface
    this with an actionable message ("port 18791 in use; try
    `lsof -ti:18791 | xargs kill -9`").
    """

    def __init__(
        self,
        *,
        bus: TypedEventBus,
        port: int = 18791,
        token: str = "",
        bind: str = "127.0.0.1",
    ) -> None:
        self._bus = bus
        self._port = port
        self._token = token
        self._bind = bind
        self._runner: web.AppRunner | None = None

    async def start(self) -> web.AppRunner:
        app = web.Application(client_max_size=512 * 1024)  # 512KB cap
        app.router.add_post("/browser-event", self._handle)
        app.router.add_get("/health", self._health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._bind, self._port)
        await site.start()  # raises OSError on EADDRINUSE — let it bubble
        self._runner = runner
        _log.info("browser-bridge listening on %s:%s", self._bind, self._port)
        return runner

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle(self, request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {self._token}"
        if self._token and auth != expected:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload: dict[str, Any] = await request.json()
        except (web.HTTPBadRequest, ValueError):
            return web.json_response({"error": "bad_json"}, status=400)
        url = payload.get("url")
        if not isinstance(url, str) or not url:
            return web.json_response({"error": "missing url"}, status=400)
        title = str(payload.get("title", ""))[:256]
        visit_time = float(payload.get("visit_time") or time.time())
        event = SignalEvent(
            event_type="browser_visit",
            source="browser-bridge",
            timestamp=visit_time,
            metadata={"url": url[:2048], "title": title},
        )
        self._bus.publish(event)
        return web.json_response({"status": "ok"})
```

- [ ] **Step 10.6: Implement plugin entry**

Create `extensions/browser-bridge/plugin.py`:

```python
"""Browser-bridge plugin — wires the adapter into OpenComputer's gateway."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from plugin_sdk import PluginManifest

from extensions.browser_bridge.adapter import BrowserBridgeAdapter, generate_token

_log = logging.getLogger("extensions.browser_bridge.plugin")


def register(api: Any) -> PluginManifest:
    """Register the browser-bridge plugin with the host."""
    # The HTTP listener is started lazily by the gateway daemon — see
    # `opencomputer/gateway/server.py` for the pattern other listeners
    # use. For MVP, the bridge is started manually via
    # `opencomputer profile bridge start`. Subsequent PRs wire it into
    # gateway auto-start.
    return PluginManifest(
        id="browser-bridge",
        name="Browser Bridge",
        version="0.1.0",
        description=(
            "Receives tab activity from the OpenComputer browser extension "
            "and fans events into the F2 SignalEvent bus."
        ),
        kind="tools",
    )


__all__ = ["register", "BrowserBridgeAdapter", "generate_token"]
```

- [ ] **Step 10.7: Verify tests pass**

Run: `pytest tests/test_browser_bridge.py -v`
Expected: 2 PASS

- [ ] **Step 10.8: Commit**

```bash
git add extensions/browser-bridge tests/test_browser_bridge.py tests/conftest.py
git commit -m "feat(browser-bridge): Layer 4 minimal — Python listener for Chrome extension"
```

---

## Task 11: Browser extension (Chrome MV3) + README

**Files:**
- Create: `extensions/browser-bridge/extension/manifest.json`
- Create: `extensions/browser-bridge/extension/background.js`
- Create: `extensions/browser-bridge/README.md`

(No tests — JavaScript bundle exercised manually; Python listener test in Task 10 covers the API contract.)

- [ ] **Step 11.1: Write Chrome MV3 manifest**

Create `extensions/browser-bridge/extension/manifest.json`:

```json
{
  "manifest_version": 3,
  "name": "OpenComputer Browser Bridge",
  "version": "0.1.0",
  "description": "Forwards tab activity to the local OpenComputer agent. Required for OpenComputer's Layered Awareness feature (Layer 4).",
  "permissions": ["tabs"],
  "host_permissions": ["http://127.0.0.1:18791/*"],
  "background": {
    "service_worker": "background.js"
  },
  "action": {
    "default_title": "OpenComputer Browser Bridge"
  }
}
```

- [ ] **Step 11.2: Write background service worker**

Create `extensions/browser-bridge/extension/background.js`:

```javascript
// OpenComputer Browser Bridge — Chrome MV3 service worker.
// Forwards tab navigation events to the local OC agent's listener.
//
// Token must be set via chrome.storage.local before events flow.
// Set via the install-time UI (post-MVP) or manually for now:
//   chrome.storage.local.set({ ocBridgeToken: '<paste token>' })

const ENDPOINT = 'http://127.0.0.1:18791/browser-event';

async function getToken() {
  const result = await chrome.storage.local.get(['ocBridgeToken']);
  return result.ocBridgeToken || '';
}

async function postVisit(url, title) {
  const token = await getToken();
  if (!token) {
    return;  // bridge disabled until user pastes token
  }
  if (!url || url.startsWith('chrome://') || url.startsWith('about:')) {
    return;  // skip browser-internal URLs
  }
  try {
    await fetch(ENDPOINT, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify({
        url,
        title: title || '',
        visit_time: Date.now() / 1000,
      }),
    });
  } catch (err) {
    // local listener not running — silently drop. No retries in MVP.
  }
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url) {
    postVisit(tab.url, tab.title);
  }
});
```

- [ ] **Step 11.3: Write README**

Create `extensions/browser-bridge/README.md`:

```markdown
# Browser Bridge — OpenComputer Layer 4 (minimal)

Captures tab navigation from Chrome / Brave / Edge and forwards each visit
to the local OpenComputer agent. Powers the "agent already knows what I'm
working on" awareness in Layer 4 of the Layered Awareness MVP.

## Install (Chrome / Brave / Edge)

1. Open `chrome://extensions/` (or `brave://extensions/`).
2. Toggle "Developer mode" on.
3. Click "Load unpacked".
4. Select the `extensions/browser-bridge/extension/` directory.
5. Note the extension ID Chrome assigns.

## Pair the extension with your OC agent

```bash
opencomputer profile bridge token
```

Copy the printed token. Then in Chrome's DevTools console (with the
extension's background page focused — go to `chrome://extensions/`,
click "Service worker" under OpenComputer Browser Bridge):

```javascript
chrome.storage.local.set({ ocBridgeToken: '<paste-token-here>' })
```

The extension immediately starts forwarding visits. Verify via:

```bash
opencomputer profile bridge tail
```

You should see a stream of `browser.visit` events.

## What gets sent

URL + page title + timestamp. **No page content, no form data, no
cookies.** The listener is bound to `127.0.0.1` only — nothing leaves
your machine.

## Disabling temporarily

Disable the extension in `chrome://extensions/`. Or revoke the
capability:

```bash
opencomputer consent revoke ingestion.browser_extension
```
```

- [ ] **Step 11.4: Commit**

```bash
git add extensions/browser-bridge/extension extensions/browser-bridge/README.md
git commit -m "feat(browser-bridge): Chrome MV3 extension + install README"
```

---

## Task 12: Inject user-model knowledge into system prompt

**Files:**
- Modify: `opencomputer/agent/prompt_builder.py`
- Modify: `opencomputer/agent/prompts/base.j2`
- Test: `tests/test_prompt_builder_user_facts.py`

- [ ] **Step 12.1: Write failing test**

```python
# tests/test_prompt_builder_user_facts.py
from pathlib import Path

from opencomputer.agent.prompt_builder import PromptBuilder, PromptContext
from opencomputer.user_model.store import UserModelStore


def test_user_facts_section_rendered_when_present(tmp_path: Path):
    store = UserModelStore(tmp_path / "graph.sqlite")
    store.upsert_node(kind="identity", value="name: Saksham", confidence=1.0)
    store.upsert_node(kind="goal", value="current_focus: Ship OC v1.0", confidence=1.0)

    pb = PromptBuilder()
    ctx = PromptContext(
        cwd="/tmp",
        user_home="/Users/saksham",
        now="2026-04-26T10:00:00",
        user_facts=pb.build_user_facts(store=store),
    )
    rendered = pb.build(context=ctx, plan_mode=False, yolo_mode=False)
    assert "Saksham" in rendered
    assert "OC v1.0" in rendered or "v1.0" in rendered


def test_user_facts_section_absent_when_empty(tmp_path: Path):
    store = UserModelStore(tmp_path / "graph.sqlite")
    pb = PromptBuilder()
    facts_block = pb.build_user_facts(store=store)
    assert facts_block == ""  # no facts → empty
```

- [ ] **Step 12.2: Run failing test**

Run: `pytest tests/test_prompt_builder_user_facts.py -v`
Expected: FAIL — `PromptContext` has no `user_facts`; `PromptBuilder` has no `build_user_facts`

- [ ] **Step 12.3: Extend `PromptContext` and add `build_user_facts`**

In `opencomputer/agent/prompt_builder.py`:

Add to `PromptContext` (after `soul: str = ""`):

```python
    user_facts: str = ""
    """Pre-formatted top-K user-model facts (Layered Awareness MVP).

    Built via :meth:`PromptBuilder.build_user_facts` from the F4 graph.
    Empty string means "no user-model knowledge yet" — base.j2 omits
    the section accordingly.
    """
```

Add to `PromptBuilder` class:

```python
    def build_user_facts(
        self,
        *,
        store: "UserModelStore | None" = None,
        top_k: int = 20,
    ) -> str:
        """Return a pre-formatted top-K user-facts block, or empty string.

        Pulls Identity + Goal + Preference + Attribute nodes from the
        F4 user-model graph, sorted by kind priority then descending
        confidence. Truncates to ~80 chars per fact for prompt token
        economy. Returns ``""`` when the graph is empty so that
        ``base.j2`` can omit the section via ``{% if user_facts %}``.
        """
        from opencomputer.user_model.store import UserModelStore

        s = store if store is not None else UserModelStore()
        # Bumped from default 100 to 500 so a fresh bootstrap (which
        # may write 50-200 nodes) leaves headroom for ranking before
        # the top-K cut.
        nodes = s.list_nodes(
            kinds=("identity", "goal", "preference", "attribute"),
            limit=500,
        )
        # Rank: identity > goal > preference > attribute, then by confidence
        kind_order = {"identity": 0, "goal": 1, "preference": 2, "attribute": 3}
        nodes_ranked = sorted(
            nodes,
            key=lambda n: (kind_order.get(n.kind, 99), -n.confidence),
        )[:top_k]
        if not nodes_ranked:
            return ""
        lines = [f"- ({n.kind}) {n.value[:80]}" for n in nodes_ranked]
        return "\n".join(lines)
```

- [ ] **Step 12.4: Update `base.j2` template**

The current `base.j2` is 41 lines. Insert the new block immediately AFTER the closing `{% endif %}` of the existing `user_profile` block (line 25) and BEFORE the `{% if skills -%}` block (line 26). The exact insertion site:

```jinja
{% endif %}
{% if user_facts -%}
## What I know about you

{{ user_facts }}

{% endif %}
{% if skills -%}
```

In other words, find the existing line `{% if skills -%}` and prepend the four lines beginning with `{% if user_facts -%}` directly above it. Verify with `cat opencomputer/agent/prompts/base.j2 | head -35` — the `{% if user_facts -%}` block should sit between the `user_profile` block and the `skills` block.

- [ ] **Step 12.5: Verify tests pass**

Run: `pytest tests/test_prompt_builder_user_facts.py -v`
Expected: 2 PASS

- [ ] **Step 12.6: Run full test suite to catch regressions**

Run: `pytest tests/test_prompt_builder*.py -v`
Expected: all PASS

- [ ] **Step 12.7: Commit**

```bash
git add opencomputer/agent/prompt_builder.py opencomputer/agent/prompts/base.j2 tests/test_prompt_builder_user_facts.py
git commit -m "feat(prompt-builder): inject user-model facts into system prompt (Layered Awareness MVP)"
```

---

## Task 13: CLI — `opencomputer profile bootstrap`

**Files:**
- Modify: `opencomputer/cli_profile.py`
- Test: `tests/test_cli_profile_bootstrap.py`

- [ ] **Step 13.1: Write failing test**

```python
# tests/test_cli_profile_bootstrap.py
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli_profile import profile_app

runner = CliRunner()


def test_bootstrap_skip_runs_layers_0_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    with patch(
        "opencomputer.cli_profile.run_bootstrap",
    ) as m:
        m.return_value.__class__.__name__ = "BootstrapResult"
        m.return_value.identity_nodes_written = 1
        m.return_value.interview_nodes_written = 0
        m.return_value.files_scanned = 0
        m.return_value.git_commits_scanned = 0
        m.return_value.elapsed_seconds = 0.1
        result = runner.invoke(profile_app, ["bootstrap", "--skip-interview"])
    assert result.exit_code == 0
    assert "Identity" in result.stdout
    assert m.called


def test_bootstrap_already_complete_short_circuits(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    marker = tmp_path / "profile_bootstrap" / "complete.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    result = runner.invoke(profile_app, ["bootstrap"])
    assert result.exit_code == 0
    assert "already complete" in result.stdout.lower()


def test_bootstrap_force_reruns(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    marker = tmp_path / "profile_bootstrap" / "complete.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    with patch("opencomputer.cli_profile.run_bootstrap") as m:
        m.return_value.identity_nodes_written = 1
        m.return_value.interview_nodes_written = 0
        m.return_value.files_scanned = 0
        m.return_value.git_commits_scanned = 0
        m.return_value.elapsed_seconds = 0.1
        result = runner.invoke(
            profile_app, ["bootstrap", "--skip-interview", "--force"]
        )
    assert result.exit_code == 0
    assert m.called
```

- [ ] **Step 13.2: Run failing test**

Run: `pytest tests/test_cli_profile_bootstrap.py -v`
Expected: FAIL — no `bootstrap` subcommand

- [ ] **Step 13.3: Add `bootstrap` subcommand to `cli_profile.py`**

Read the existing `opencomputer/cli_profile.py`. Find the `profile_app = typer.Typer(...)` and add a new command function alongside the existing list/create/use/delete/rename/path commands:

```python
@profile_app.command("bootstrap")
def profile_bootstrap(
    skip_interview: bool = typer.Option(
        False, "--skip-interview", help="Skip the 5-question quick interview"
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-run even if already completed"
    ),
    days: int = typer.Option(
        7, "--days", help="Look-back window for Layer 2 file/git scan"
    ),
) -> None:
    """Run the install-time bootstrap (Layered Awareness MVP, Layers 0-2).

    Reads system identity, asks 5 quick questions, scans the last 7 days
    of recent files + git activity. Total time: under 6 minutes.
    """
    from pathlib import Path

    from opencomputer.agent.config import _home
    from opencomputer.profile_bootstrap.identity_reflex import gather_identity
    from opencomputer.profile_bootstrap.orchestrator import run_bootstrap
    from opencomputer.profile_bootstrap.quick_interview import (
        QUICK_INTERVIEW_QUESTIONS,
        render_questions,
    )

    home = _home()
    marker = home / "profile_bootstrap" / "complete.json"
    if marker.exists() and not force:
        typer.echo("Bootstrap already complete. Use --force to re-run.")
        raise typer.Exit(0)

    facts = gather_identity()

    answers: dict[str, str] = {}
    if not skip_interview:
        rendered = render_questions(facts)
        typer.echo(rendered[0])  # greeting
        for (key, _), prompt in zip(QUICK_INTERVIEW_QUESTIONS, rendered[1:]):
            answer = typer.prompt(prompt, default="", show_default=False)
            if answer.strip():
                answers[key] = answer.strip()

    home_dirs = [
        Path.home() / "Documents",
        Path.home() / "Desktop",
        Path.home() / "Downloads",
    ]
    git_repos = _detect_git_repos()

    result = run_bootstrap(
        interview_answers=answers,
        scan_roots=[d for d in home_dirs if d.exists()],
        git_repos=git_repos,
        include_calendar=True,
        include_browser_history=True,
        marker_path=marker,
    )

    typer.echo("")
    typer.echo("Bootstrap complete:")
    typer.echo(f"  Identity nodes written:  {result.identity_nodes_written}")
    typer.echo(f"  Interview nodes written: {result.interview_nodes_written}")
    typer.echo(f"  Files scanned:           {result.files_scanned}")
    typer.echo(f"  Git commits scanned:     {result.git_commits_scanned}")
    typer.echo(f"  Elapsed:                 {result.elapsed_seconds:.1f}s")


def _detect_git_repos(max_repos: int = 50) -> list:
    """Find candidate git repos in common locations. Best-effort, capped."""
    from pathlib import Path

    candidates = [
        Path.home() / "Vscode",
        Path.home() / "Projects",
        Path.home() / "Code",
        Path.home() / "src",
    ]
    repos = []
    for root in candidates:
        if not root.exists():
            continue
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if (entry / ".git").exists():
                repos.append(entry)
                if len(repos) >= max_repos:
                    return repos
    return repos
```

Also at the top of the file:

```python
from opencomputer.profile_bootstrap.orchestrator import run_bootstrap  # noqa: F401
```

(The mock target in the test is `opencomputer.cli_profile.run_bootstrap`, so the import must be at module scope.)

- [ ] **Step 13.4: Verify tests pass**

Run: `pytest tests/test_cli_profile_bootstrap.py -v`
Expected: 3 PASS

- [ ] **Step 13.5: Add end-to-end integration test**

Append to `tests/test_cli_profile_bootstrap.py`:

```python
def test_bootstrap_then_prompt_includes_user_facts(tmp_path: Path, monkeypatch):
    """E2E: bootstrap → graph populated → prompt builder injects facts."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # Run the real orchestrator (no mock) with a tightly-scoped scope.
    from opencomputer.profile_bootstrap.orchestrator import run_bootstrap
    from opencomputer.user_model.store import UserModelStore
    from opencomputer.agent.prompt_builder import PromptBuilder, PromptContext

    graph_path = tmp_path / "user_model" / "graph.sqlite"
    graph_path.parent.mkdir(parents=True)
    store = UserModelStore(graph_path)

    result = run_bootstrap(
        interview_answers={
            "current_focus": "Shipping Layered Awareness MVP",
            "tone_preference": "concise",
        },
        scan_roots=[],
        git_repos=[],
        include_calendar=False,
        include_browser_history=False,
        store=store,
        marker_path=tmp_path / "complete.json",
    )
    assert result.interview_nodes_written == 2

    pb = PromptBuilder()
    facts_block = pb.build_user_facts(store=store)
    ctx = PromptContext(user_facts=facts_block)
    rendered = pb.build(context=ctx, plan_mode=False, yolo_mode=False)
    assert "Layered Awareness MVP" in rendered
    assert "concise" in rendered
```

- [ ] **Step 13.6: Smoke-test the live CLI**

Run: `opencomputer profile bootstrap --skip-interview --force`
Expected: prints identity nodes count, completes in <10 seconds

- [ ] **Step 13.7: Commit**

```bash
git add opencomputer/cli_profile.py tests/test_cli_profile_bootstrap.py
git commit -m "feat(cli): opencomputer profile bootstrap — orchestrate Layered Awareness MVP"
```

---

## Task 14: Run full test suite + ruff

- [ ] **Step 14.1: Run full pytest suite**

Run: `pytest -x -q`
Expected: all tests pass; new test count = previous + ~30

- [ ] **Step 14.2: Run ruff**

Run: `ruff check opencomputer/profile_bootstrap extensions/browser-bridge tests/test_profile_bootstrap_*.py tests/test_browser_bridge.py tests/test_capability_taxonomy_ingestion.py tests/test_prompt_builder_user_facts.py tests/test_cli_profile_bootstrap.py`
Expected: clean

- [ ] **Step 14.3: Update CHANGELOG.md**

Edit `CHANGELOG.md`. Under `[Unreleased]`, add a new section:

```markdown
### Added (Layered Awareness MVP, 2026-04-26)

First-pass implementation of "agent already knows the user" via four
overlapping layers running at different cadences:

- **Layer 0 — Identity Reflex.** Reads `$USER`, git config, macOS
  Contacts.app `me` card, system locale. <1s, no consent prompts.
- **Layer 1 — Quick Interview.** Five install-time questions
  (current focus, concerns, tone preference, do-not-do, free-form).
  Persisted as `user_explicit` user-model edges with confidence 1.0.
- **Layer 2 — Recent Context Scan.** 7-day window over files in
  `~/Documents` / `~/Desktop` / `~/Downloads`, git log across
  detected repos in `~/Vscode` / `~/Projects` / etc., calendar
  events (FDA-gated), Chrome browser history.
- **Layer 4 minimal — Browser Bridge.** Chrome MV3 extension +
  Python aiohttp listener at `127.0.0.1:18791`. Forwards every tab
  navigation as a `browser.visit` SignalEvent into the F2 bus.

CLI: `opencomputer profile bootstrap` runs Layers 0-2 sequentially,
`--skip-interview` runs Layer 0 only, `--force` re-runs after marker.

Prompt builder gains a `{{ user_facts }}` slot pulling top-20 nodes
from F4 user-model graph (Identity > Goal > Preference > Attribute,
ranked by confidence). Block omitted if graph empty.

F1 capability claims added: `ingestion.recent_files` (IMPLICIT),
`ingestion.git_log` (IMPLICIT), `ingestion.calendar` (EXPLICIT),
`ingestion.browser_history` (EXPLICIT), `ingestion.messages`
(EXPLICIT), `ingestion.browser_extension` (EXPLICIT).

V2/V3/V4 of Layered Awareness (background deepening, life-event
detector, plural personas, curious companion) ship in subsequent
plans after MVP dogfood.

Spec: `docs/superpowers/specs/2026-04-26-layered-awareness-design.md`
Plan: `docs/superpowers/plans/2026-04-26-layered-awareness-mvp.md`
```

- [ ] **Step 14.4: Final commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): Layered Awareness MVP entry"
```

---

## Self-Review (post-audit)

Pass against the spec — section by section, can I point to a task?

- ✅ **Layer 0 (Identity Reflex)** — Task 2.
- ✅ **Layer 1 (Quick Interview)** — Tasks 3 + 4.
- ✅ **Layer 2 (Recent Context Scan)** — Tasks 6, 7, 8.
- ✅ **Layer 4 minimal (Browser Bridge)** — Tasks 5 (CLI) + 10 (Python listener) + 11 (Chrome extension).
- ✅ **F1 capability claims** — Task 1.
- ✅ **User-model graph persistence** — Task 3.
- ✅ **Prompt-builder integration (`user_facts` slot)** — Task 12.
- ✅ **CLI orchestration** — Tasks 9 + 13.
- ✅ **End-to-end integration test** — Task 13 step 13.5.
- ✅ **Test coverage + ruff + changelog** — Task 14.

Spec coverage: complete for MVP scope. V2/V3/V4 explicitly out of plan scope.

Placeholder scan: no "TBD" / "TODO" / "fill in details" / "find a sensible place" / "similar to" patterns remain after the audit pass.

Type consistency:
- `IdentityFacts` field set is consistent across Tasks 2, 3, 9, 13.
- `BootstrapResult` shape consistent in Tasks 9 + 13.
- `BrowserBridgeAdapter` constructor signature consistent across Task 10 tests + plugin.py.
- `SignalEvent(event_type=..., source=..., timestamp=..., metadata={...})` matches the `plugin_sdk/ingestion.py` dataclass shape (verified at audit time).
- `UserModelStore.list_nodes(kinds=("identity",))` (plural sequence) matches the real signature (verified at audit time).
- `BridgeState` (Task 5) is referenced consistently in the bridge CLI commands.

### Audit-driven changes from the original plan

| Change | Reason |
|---|---|
| Dropped original Task 5 (Ollama LLM extractor) | Was dead code in MVP — orchestrator never called it. Moved to V2 alongside Background Deepening where it has a real consumer. |
| New Task 5 (Bridge CLI subcommands) | Browser-bridge plugin had no way to be started/queried; Task 11's README referenced commands that didn't exist. |
| `SignalEvent(event_type=..., timestamp=..., metadata=...)` | Original plan used `kind/ts/payload` — wrong against actual SDK. |
| `event_type="browser_visit"` (snake_case) | Matches existing convention (`tool_call`, `web_observation`); `browser.visit` (dotted) was made up. |
| `store.list_nodes(kinds=("identity",))` | Original used `kind="identity"` — real signature takes plural sequence. |
| Removed `@pytest.mark.asyncio` decorators | `pyproject.toml` has `asyncio_mode = "auto"` — decorators redundant. |
| Specified exact `base.j2` insertion point (after line 25, before `{% if skills -%}`) | Original "find a sensible place" was a placeholder. |
| `_AUTHORIZED_STATUSES` named constant set | Replaces magic-number `(3, 4)` tuple in calendar reader; tolerates new statuses (5 = FullAccess in macOS 14+). |
| Bumped `osascript` Contacts timeout from 3s → 30s | First call shows macOS Privacy dialog; user may take >3s to grant. |
| Added Task 13 step 13.5 — E2E integration test | Verifies bootstrap → graph → prompt round-trip; previously each component tested in isolation only. |
| Added port-collision test in Task 10 | Edge case discovered in stress test — second adapter on same port should raise OSError, not silently fail. |

### Acknowledged-as-deferred (documented, not blocking MVP)

- **Multi-profile Chrome history** — only `Default` profile read in MVP. V2.
- **Browser extension token paste UX** — manual DevTools console paste for MVP; popup UI in V2.
- **Audit-log integration in orchestrator** — F1 has HMAC-chained audit but bootstrap doesn't write entries yet. Add when V2's deepening ingest lands so audit shape covers both shapes uniformly.
- **Parallel git log calls** — sequential at MVP, ThreadPoolExecutor in V2 if scan time becomes painful (~50 repos × 1s).
- **`flock` on bootstrap marker** — concurrent bootstrap runs not protected; acceptable for single-user MVP.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-26-layered-awareness-mvp.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between tasks, fast iteration. Use `superpowers:subagent-driven-development`.

**2. Inline Execution** — execute tasks in this session via `superpowers:executing-plans`, batch with checkpoints.

The user requested a self-audit before execution; that follows next, then handoff.
