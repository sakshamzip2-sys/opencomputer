# Hermes Best-of Import — Wave 5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port 14 high-value Hermes-agent features (post-PR #413) into OpenComputer in a single PR with 4 grouped commits: agent core (A), multimodal/voice (B), plugin platform (C), storage+skills (D).

**Architecture:** Each feature lands in OC's existing flat layout (`opencomputer/{agent,tools,voice,gateway,acp,cli_ui,skills_hub}/`, `plugin_sdk/`, `extensions/{provider,channel}/`). Hooks added as `HookEvent` enum entries with optional fields on `HookContext`. Channel adapter overrides extend `plugin_sdk/channel_contract.py`. Hermes' name-string hook contract is adapted to OC's typed enum-based hook contract.

**Tech Stack:** Python 3.13, asyncio, pytest, ruff. Hermes source at `/Users/saksham/Vscode/claude/sources/hermes-agent/` (HEAD ~2026-05-04). Spec: `docs/superpowers/specs/2026-05-04-hermes-best-of-import-wave5-design.md`.

**Group ordering:** A → B → C → D. Group A and Group D both touch `agent/loop.py`; A first (instruments the loop), then D (adds session-row gate).

**Worktree:** Per memory rule, work in a dedicated worktree to avoid contamination with other live sessions.

---

## Pre-Execution Corrections (verified against current OC source 2026-05-04)

After writing the initial draft, a self-audit grepping the actual OC source surfaced six API misalignments. These corrections **override the corresponding code blocks below** when there's a conflict.

### Verified OC APIs (use these, not my draft signatures)

```python
# opencomputer/agent/state.py  — class SessionDB at line 656 (NOT session_db.py)
#   - Methods exist:  create_session, get_session, list_sessions, delete_session,
#                     auto_prune, append_message, set_session_title,
#                     get_session_vibe / set_session_vibe (column-based per-session field)
#   - NO state_meta / set_state_meta / get_state_meta. Use a NEW schema column
#     migration (schema v10 → v11) to add goal_text/goal_active/goal_turns_used/goal_budget,
#     mirroring the existing `vibe` column pattern.
#   - auto_prune(older_than_days, untitled_days, min_messages, cap=200) ALREADY exists
#     and handles ghost-session pruning via Policy B (untitled+few-messages+older).
#     Reuse it for the lazy-session migration.

# opencomputer/agent/aux_llm.py — exposed functions:
#   - complete_text(messages: list[dict], system: str = "", max_tokens: int = 1024,
#                   temperature: float = 1.0, model: str | None = None) -> str
#   - complete_text_sync(...) — sync wrapper using asyncio.run
#   - complete_vision(image_base64: str, mime_type: str, prompt: str,
#                     max_tokens: int = 1024, model: str | None = None) -> str
#   - NO call_aux_model / call_aux_model_multimodal.
#   - For video, ADD complete_video() in aux_llm.py mirroring complete_vision but
#     building a {"type": "video_url", "video_url": {"url": "data:..."}} content block.

# plugin_sdk/skill_source.py — SkillSource ABC requires:
#   - name (property) -> str
#   - search(query: str, limit: int = 10) -> list[SkillMeta]
#   - fetch(identifier: str) -> SkillBundle | None
#   - inspect(identifier: str) -> SkillMeta | None
#   - NO claims() method. Routing is done by SkillSourceRouter via
#     identifier-prefix split on "/" — identifier shape MUST be "<source>/<name>".
#   - For URL skills: identifier = "url/<urlsafe_b64_or_slug>"; the router routes
#     to UrlSource by the "url" prefix.

# opencomputer/hooks/engine.py:
#   - fire_blocking(ctx) returns the FIRST non-pass HookDecision, or None if all
#     hooks returned "pass" or no hooks were registered. Plan T13.3 test must
#     `assert decision is None`, NOT assert d.decision == "pass".

# extensions/openrouter-provider/ has:
#   - plugin.json, plugin.py, provider.py
#   - The actual adapter file is provider.py (NOT openrouter_adapter.py).
#   - T5 import paths: `from extensions.openrouter_provider.provider import ...`
#     (note: directory is openrouter-provider/ but Python module is openrouter_provider).

# opencomputer/acp/session.py — class ACPSession EXISTS (line 36). Verify field
# mutability before adding state — may need to convert dataclass to non-frozen.
```

### Per-task corrections (apply during execution)

**T2 (/goal) — replace state_meta calls with column migration:**

In `state.py`, advance `SCHEMA_VERSION` to 11 and add migration:

```python
# In state.py DDL or apply_migrations:
ALTER TABLE sessions ADD COLUMN goal_text TEXT;
ALTER TABLE sessions ADD COLUMN goal_active INTEGER DEFAULT 0;
ALTER TABLE sessions ADD COLUMN goal_turns_used INTEGER DEFAULT 0;
ALTER TABLE sessions ADD COLUMN goal_budget INTEGER DEFAULT 20;
```

Add SessionDB methods:

```python
def set_session_goal(self, session_id: str, *, text: str, budget: int = 20) -> None:
    with self._connect() as conn:
        conn.execute(
            "UPDATE sessions SET goal_text=?, goal_active=1, goal_turns_used=0, goal_budget=? WHERE id=?",
            (text, budget, session_id),
        )

def get_session_goal(self, session_id: str) -> dict | None:
    with self._connect() as conn:
        row = conn.execute(
            "SELECT goal_text, goal_active, goal_turns_used, goal_budget FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
    if not row or row[0] is None:
        return None
    return {"text": row[0], "active": bool(row[1]), "turns_used": row[2], "budget": row[3]}

def update_session_goal(self, session_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with self._connect() as conn:
        conn.execute(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), session_id))

def clear_session_goal(self, session_id: str) -> None:
    with self._connect() as conn:
        conn.execute(
            "UPDATE sessions SET goal_text=NULL, goal_active=0, goal_turns_used=0 WHERE id=?",
            (session_id,),
        )
```

`opencomputer/agent/goal.py` rewrites `set_goal/get_goal/clear_goal/pause_goal/resume_goal/should_continue/increment_turn` to use `db.set_session_goal / get_session_goal / update_session_goal / clear_session_goal` directly (drops the `_key()` and `state_meta` indirection).

`_call_judge_model()` in `goal.py`:

```python
async def _call_judge_model(prompt: str) -> str:
    from opencomputer.agent.aux_llm import complete_text
    return await complete_text(
        messages=[{"role": "user", "content": prompt}],
        system=JUDGE_SYSTEM_PROMPT,
        max_tokens=8,
        temperature=0,
    )
```

T2 tests update their imports to use the new SessionDB methods directly.

**T7 (video_analyze) — add `complete_video` to aux_llm.py:**

```python
# opencomputer/agent/aux_llm.py
async def complete_video(
    *,
    video_base64: str,
    mime_type: str,
    prompt: str,
    max_tokens: int = 1024,
    model: str | None = None,
) -> str:
    """Run a video completion through the configured provider (OpenRouter video_url shape)."""
    from plugin_sdk.core import Message

    provider = _resolve_provider()
    data_url = f"data:{mime_type};base64,{video_base64}"
    content = [
        {"type": "video_url", "video_url": {"url": data_url}},
        {"type": "text", "text": prompt},
    ]
    resolved_model = model or _resolve_default_model()
    resp = await provider.complete(
        model=resolved_model,
        messages=[Message(role="user", content=content)],
        max_tokens=max_tokens,
    )
    return resp.message.content if resp and resp.message else ""

# Add "complete_video" to __all__
```

`opencomputer/tools/video_analyze.py` calls `complete_video(...)` directly instead of the fictitious `_call_aux_with_video`.

**T5 (OpenRouter cache) — fix paths:**

Change all references from `extensions/openrouter-provider/openrouter_adapter.py` to `extensions/openrouter-provider/provider.py`. Python import: `from extensions.openrouter_provider.provider import build_or_headers, parse_cache_status` (dash-to-underscore in module path). Confirm the provider class lives in that file before adding helpers.

**T13 (pre_gateway_dispatch) — fix test assertion:**

```python
# In test_pre_gateway_dispatch.py - test_plugin_crash_swallowed:
# OLD (wrong): assert d.decision == "pass"
# NEW (correct): assert d is None  # all hooks crashed → engine returned None
```

**T17 (lazy session) — reuse existing auto_prune:**

Existing `auto_prune(untitled_days=N, min_messages=1, ...)` already deletes empty/untitled sessions. Don't add `prune_empty_ghost_sessions` — instead, on startup, call:

```python
# in cli.py startup:
db.auto_prune(older_than_days=0, untitled_days=1, min_messages=1)
```

For lazy create:
- Add `SessionDB.allocate_session_id() -> str` (UUID gen, NO DB write)
- Add `SessionDB.ensure_session(session_id, *, platform, model=None, title=None) -> None` (idempotent INSERT OR IGNORE)
- Refactor `create_session(...)` to optionally call `_insert_row` only when needed; or keep it eager and mark new lazy callers to use `allocate_session_id` + `ensure_session`.

**T18 (URL skill source) — match the real SkillSource ABC:**

Rewrite `opencomputer/skills_hub/sources/url.py`:

```python
import base64
import logging
import re
from urllib.parse import urlparse

import httpx
import yaml

from plugin_sdk.skill_source import SkillBundle, SkillMeta, SkillSource

_log = logging.getLogger(__name__)
_WELL_KNOWN_PATH = "/.well-known/skills/"


class UrlSource(SkillSource):
    """Install a single SKILL.md from any http(s) URL.

    Identifier shape: ``url/<urlsafe-b64(url)>``. Router will dispatch fetch() /
    inspect() here based on the ``url/`` prefix. search() returns [] (URL skills
    are install-by-direct-identifier only; they don't appear in keyword search).
    """

    @property
    def name(self) -> str:
        return "url"

    @staticmethod
    def encode(url: str) -> str:
        return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")

    @staticmethod
    def decode(slug: str) -> str:
        pad = "=" * (-len(slug) % 4)
        return base64.urlsafe_b64decode(slug + pad).decode("utf-8")

    def search(self, query: str, limit: int = 10) -> list[SkillMeta]:
        return []  # URL skills are explicit-identifier only

    def fetch(self, identifier: str) -> SkillBundle | None:
        url = self._url_from_identifier(identifier)
        if url is None:
            return None
        try:
            text = self._http_get(url)
        except Exception as e:  # noqa: BLE001
            _log.warning("UrlSource fetch failed for %s: %s", url, e)
            return None
        return SkillBundle(identifier=identifier, skill_md=text, files={})

    def inspect(self, identifier: str) -> SkillMeta | None:
        url = self._url_from_identifier(identifier)
        if url is None:
            return None
        try:
            text = self._http_get(url)
        except Exception as e:  # noqa: BLE001
            _log.warning("UrlSource inspect failed for %s: %s", url, e)
            return None
        fm, _ = self._split_frontmatter(text)
        name = (fm or {}).get("name") or self._slug_from_url(url)
        description = (fm or {}).get("description", "")
        return SkillMeta(
            identifier=identifier, name=name, description=description,
            source=self.name, trust_level="community",
        )

    def _url_from_identifier(self, identifier: str) -> str | None:
        if not identifier.startswith("url/"):
            return None
        slug = identifier[len("url/") :]
        try:
            url = self.decode(slug)
        except Exception:
            return None
        if not url.startswith(("http://", "https://")):
            return None
        if _WELL_KNOWN_PATH in urlparse(url).path:
            return None  # Routed to WellKnownSource by convention
        if not url.endswith(".md"):
            return None
        return url

    @staticmethod
    def _http_get(url: str) -> str:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.text

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[dict | None, str]:
        if not text.startswith("---"):
            return None, text
        end = text.find("\n---", 3)
        if end < 0:
            return None, text
        fm = yaml.safe_load(text[3:end])
        body = text[end + 4 :].lstrip("\n")
        return (fm if isinstance(fm, dict) else None), body

    @staticmethod
    def _slug_from_url(url: str) -> str:
        last = url.rstrip("/").rsplit("/", 1)[-1]
        last = re.sub(r"\.md$", "", last)
        return last or "unnamed-skill"
```

Add a small CLI helper `oc skills install <url>` that maps the URL to `url/<encoded>` and routes through `SkillSourceRouter.fetch()`.

T18 tests rewrite to test `name`, `search`, `fetch`, `inspect` methods (not `claims`).

---

## Pre-flight (do once before any task)

- [ ] **PF.1: Verify main is clean and current**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git fetch origin
git log origin/main --since="2 days ago" --oneline | head -20
git status
```
Expected: working tree clean (or only the spec from this conversation untracked). No commits since the brainstorm changing the architecture.

- [ ] **PF.2: Create worktree**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git worktree add ../OC-wave5 -b feat/hermes-best-of-wave5 origin/main
cd ../OC-wave5
```

- [ ] **PF.3: Verify deps installed in worktree**

```bash
uv sync
uv run pytest --collect-only tests/agent/ tests/tools/ -q 2>&1 | tail -3
```
Expected: tests collect without errors.

- [ ] **PF.4: Copy spec into the worktree if not present**

```bash
ls docs/superpowers/specs/2026-05-04-hermes-best-of-import-wave5-design.md \
  || cp /Users/saksham/Vscode/claude/OpenComputer/docs/superpowers/specs/2026-05-04-hermes-best-of-import-wave5-design.md docs/superpowers/specs/
```

---

## Group A — Agent Core (Tasks 1–8)

### Task 1: Add tool-loop guardrails detector

**Files:**
- Create: `opencomputer/agent/tool_guardrails.py`
- Modify: `opencomputer/agent/loop.py` (instrument tool dispatch site)
- Test: `tests/agent/test_tool_guardrails.py`

**Hermes reference:** commits `58b89965c` (`agent/tool_guardrails.py`) + `0704589ce` (warning-first refactor).

**Behavior:** Detect identical tool-name+args repeated within a window. Warn at threshold W (default 10), hard-stop at threshold S (default 25) by raising `ToolLoopGuardrailError`. Configurable via `agent.tool_guardrail_warn_at` / `agent.tool_guardrail_stop_at`. Disable via `agent.tool_guardrail_enabled = false`.

- [ ] **Step 1.1: Write failing tests**

```python
# tests/agent/test_tool_guardrails.py
import pytest
from opencomputer.agent.tool_guardrails import (
    ToolLoopGuard,
    ToolLoopGuardrailError,
    GuardrailVerdict,
)


def _call(name: str, **args):
    """Helper: build a synthetic tool call dict."""
    return {"name": name, "arguments": args}


def test_identical_repeats_warn_at_threshold():
    g = ToolLoopGuard(warn_at=3, stop_at=10)
    for _ in range(2):
        assert g.observe(_call("bash", command="ls")).level == "ok"
    v = g.observe(_call("bash", command="ls"))
    assert v.level == "warn"
    assert "bash" in v.message


def test_hard_stop_raises():
    g = ToolLoopGuard(warn_at=3, stop_at=5)
    for _ in range(4):
        g.observe(_call("bash", command="ls"))
    with pytest.raises(ToolLoopGuardrailError) as exc:
        g.observe(_call("bash", command="ls"))
    assert "5" in str(exc.value)


def test_different_args_resets_counter():
    g = ToolLoopGuard(warn_at=3, stop_at=10)
    g.observe(_call("bash", command="ls"))
    g.observe(_call("bash", command="ls"))
    v = g.observe(_call("bash", command="pwd"))
    assert v.level == "ok"


def test_disabled_never_warns():
    g = ToolLoopGuard(warn_at=1, stop_at=2, enabled=False)
    for _ in range(50):
        v = g.observe(_call("bash", command="ls"))
        assert v.level == "ok"


def test_normalizes_argument_order():
    g = ToolLoopGuard(warn_at=2, stop_at=10)
    g.observe(_call("bash", command="ls", cwd="/"))
    v = g.observe(_call("bash", cwd="/", command="ls"))  # same args, different order
    assert v.level == "warn"


def test_resets_on_reset():
    g = ToolLoopGuard(warn_at=2, stop_at=3)
    g.observe(_call("bash", command="ls"))
    g.observe(_call("bash", command="ls"))
    g.reset()
    v = g.observe(_call("bash", command="ls"))
    assert v.level == "ok"
```

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
cd /Users/saksham/Vscode/claude/OC-wave5
uv run pytest tests/agent/test_tool_guardrails.py -v
```
Expected: ImportError: `cannot import name 'ToolLoopGuard'`.

- [ ] **Step 1.3: Implement `tool_guardrails.py`**

```python
# opencomputer/agent/tool_guardrails.py
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal


class ToolLoopGuardrailError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class GuardrailVerdict:
    level: Literal["ok", "warn"]
    message: str = ""


class ToolLoopGuard:
    """Detects identical tool-call repetition within a turn.

    `observe()` returns a verdict per call; raises `ToolLoopGuardrailError`
    when the configured stop threshold is hit.
    """

    def __init__(
        self,
        *,
        warn_at: int = 10,
        stop_at: int = 25,
        enabled: bool = True,
    ) -> None:
        if warn_at < 1 or stop_at < warn_at:
            raise ValueError("warn_at must be ≥1 and stop_at must be ≥ warn_at")
        self._warn_at = warn_at
        self._stop_at = stop_at
        self._enabled = enabled
        self._last_key: str | None = None
        self._streak: int = 0

    def reset(self) -> None:
        self._last_key = None
        self._streak = 0

    def observe(self, tool_call: dict[str, Any]) -> GuardrailVerdict:
        if not self._enabled:
            return GuardrailVerdict(level="ok")
        key = self._key(tool_call)
        if key == self._last_key:
            self._streak += 1
        else:
            self._last_key = key
            self._streak = 1
        if self._streak >= self._stop_at:
            raise ToolLoopGuardrailError(
                f"Tool-loop guardrail: {tool_call.get('name', '?')} repeated "
                f"{self._streak} consecutive calls (stop_at={self._stop_at})."
            )
        if self._streak == self._warn_at:
            return GuardrailVerdict(
                level="warn",
                message=(
                    f"Tool-loop guardrail: '{tool_call.get('name', '?')}' "
                    f"has run {self._streak} consecutive identical calls "
                    f"(warn_at={self._warn_at}, stop_at={self._stop_at})."
                ),
            )
        return GuardrailVerdict(level="ok")

    @staticmethod
    def _key(tool_call: dict[str, Any]) -> str:
        name = tool_call.get("name", "")
        args = tool_call.get("arguments") or {}
        # Canonical JSON: sorted keys → arg order doesn't matter
        return f"{name}|{json.dumps(args, sort_keys=True, default=str)}"
```

- [ ] **Step 1.4: Run tests — must pass**

```bash
uv run pytest tests/agent/test_tool_guardrails.py -v
```
Expected: 6 passed.

- [ ] **Step 1.5: Wire into `agent/loop.py`**

Find the tool-dispatch site in `opencomputer/agent/loop.py` (search for the call that invokes the tool registry). Wrap it:

```python
# At top of loop.py:
from opencomputer.agent.tool_guardrails import ToolLoopGuard, ToolLoopGuardrailError

# Inside the AgentLoop class __init__ (or wherever per-turn state initializes):
self._tool_guard = ToolLoopGuard(
    warn_at=self.config.get("agent.tool_guardrail_warn_at", 10),
    stop_at=self.config.get("agent.tool_guardrail_stop_at", 25),
    enabled=self.config.get("agent.tool_guardrail_enabled", True),
)

# Reset at the start of each user turn (search for "begin_turn" / "run_turn" / similar):
self._tool_guard.reset()

# Before dispatching each tool call:
verdict = self._tool_guard.observe({"name": tool_call.name, "arguments": tool_call.arguments})
if verdict.level == "warn":
    # Display via the same channel used for system warnings (TUI message bar)
    self._emit_system_message(verdict.message)
# Then proceed with tool dispatch as before. ToolLoopGuardrailError propagates
# up; loop.py's existing exception handler should catch it and end the turn
# gracefully.
```

If the exact integration site is unclear, grep for `tool_dispatch`, `_dispatch_tool`, `registry.dispatch`, or `await tool.run` in `loop.py` and the surrounding agent module.

- [ ] **Step 1.6: Add integration test**

```python
# tests/agent/test_tool_guardrails_integration.py
import pytest
from opencomputer.agent.tool_guardrails import ToolLoopGuard, ToolLoopGuardrailError


def test_loop_guard_in_agent_lifecycle():
    """Smoke test: guard resets per-turn, observes calls, raises at stop_at."""
    g = ToolLoopGuard(warn_at=2, stop_at=4)
    # Turn 1: 3 calls — warn at 2nd
    for i in range(3):
        v = g.observe({"name": "bash", "arguments": {"cmd": "ls"}})
        if i == 1:
            assert v.level == "warn"
    # Reset for new turn
    g.reset()
    # Turn 2: 4 calls — raises on 4th
    with pytest.raises(ToolLoopGuardrailError):
        for _ in range(4):
            g.observe({"name": "bash", "arguments": {"cmd": "ls"}})
```

- [ ] **Step 1.7: Run all guardrail tests**

```bash
uv run pytest tests/agent/test_tool_guardrails.py tests/agent/test_tool_guardrails_integration.py -v
```
Expected: 7 passed.

- [ ] **Step 1.8: Commit**

```bash
git add opencomputer/agent/tool_guardrails.py opencomputer/agent/loop.py tests/agent/test_tool_guardrails.py tests/agent/test_tool_guardrails_integration.py
git commit -m "feat(agent): add tool-loop guardrails (warn + hard-stop)

Mirror hermes-agent 58b89965c + 0704589ce. Detects identical
tool-name+args repetitions; warns at agent.tool_guardrail_warn_at
(default 10), hard-stops at agent.tool_guardrail_stop_at (default 25).
Configurable via config.yaml; disable via tool_guardrail_enabled=false.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `/goal` — Persistent cross-turn goals (Ralph loop)

**Files:**
- Create: `opencomputer/agent/goal.py`
- Modify: `opencomputer/cli_ui/slash_handlers.py` (add /goal handler)
- Modify: `opencomputer/agent/loop.py` (continuation hook at end-of-turn)
- Test: `tests/agent/test_goal.py`

**Hermes reference:** commit `265bd59c1`. Read the full commit body for the design invariants:
- Continuation prompts are regular user-role messages (no system-prompt mutation, no toolset swap)
- Continuation is a user turn, never injected mid-tool-loop
- Goal state lives in `SessionDB.state_meta` keyed by `goal:<session_id>`
- Judge fails OPEN (continue) so flaky judge never wedges
- Real user message preempts the continuation loop

**OC integration:** `state_meta` lookup uses the same `SessionDB` API the rest of OC uses (grep `state_meta` to find the helper).

- [ ] **Step 2.1: Write failing tests**

```python
# tests/agent/test_goal.py
import pytest
from opencomputer.agent.goal import (
    GoalState,
    set_goal,
    get_goal,
    clear_goal,
    pause_goal,
    resume_goal,
    should_continue,
    judge_satisfied,
)


def test_goal_state_default_active():
    g = GoalState(text="ship the feature")
    assert g.active is True
    assert g.turns_used == 0
    assert g.budget == 20


def test_set_get_clear_roundtrip(tmp_path, monkeypatch):
    """Goal persists in state_meta keyed by goal:<session_id>."""
    from opencomputer.agent.session_db import SessionDB
    db = SessionDB(path=str(tmp_path / "s.db"))
    sid = db.create_session()
    set_goal(db, sid, "ship the feature")
    g = get_goal(db, sid)
    assert g.text == "ship the feature"
    assert g.active is True
    clear_goal(db, sid)
    assert get_goal(db, sid) is None


def test_pause_resume_resets_turn_counter(tmp_path):
    from opencomputer.agent.session_db import SessionDB
    db = SessionDB(path=str(tmp_path / "s.db"))
    sid = db.create_session()
    set_goal(db, sid, "x")
    g = get_goal(db, sid)
    g.turns_used = 5
    db.set_state_meta(sid, f"goal:{sid}", g.to_json())
    pause_goal(db, sid)
    assert get_goal(db, sid).active is False
    resume_goal(db, sid)
    g2 = get_goal(db, sid)
    assert g2.active is True
    assert g2.turns_used == 0


def test_should_continue_false_when_paused(tmp_path):
    from opencomputer.agent.session_db import SessionDB
    db = SessionDB(path=str(tmp_path / "s.db"))
    sid = db.create_session()
    set_goal(db, sid, "x")
    pause_goal(db, sid)
    assert should_continue(db, sid) is False


def test_should_continue_false_when_budget_exhausted(tmp_path):
    from opencomputer.agent.session_db import SessionDB
    db = SessionDB(path=str(tmp_path / "s.db"))
    sid = db.create_session()
    set_goal(db, sid, "x")
    g = get_goal(db, sid)
    g.turns_used = g.budget
    db.set_state_meta(sid, f"goal:{sid}", g.to_json())
    assert should_continue(db, sid) is False


@pytest.mark.asyncio
async def test_judge_fails_open_on_exception(monkeypatch):
    """If the judge call raises, treat as 'not satisfied' so loop continues."""
    async def boom(*a, **kw):
        raise RuntimeError("model down")
    monkeypatch.setattr("opencomputer.agent.goal._call_judge_model", boom)
    result = await judge_satisfied(goal_text="x", last_response="y")
    assert result is False  # fail-open
```

- [ ] **Step 2.2: Run tests to verify they fail**

```bash
uv run pytest tests/agent/test_goal.py -v
```
Expected: ImportError on `opencomputer.agent.goal`.

- [ ] **Step 2.3: Implement `goal.py`**

```python
# opencomputer/agent/goal.py
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from opencomputer.agent.session_db import SessionDB

DEFAULT_BUDGET: int = 20


@dataclass(slots=True)
class GoalState:
    text: str
    active: bool = True
    turns_used: int = 0
    budget: int = DEFAULT_BUDGET

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "GoalState":
        return cls(**json.loads(s))


def _key(session_id: str) -> str:
    return f"goal:{session_id}"


def set_goal(db: "SessionDB", session_id: str, text: str, budget: int = DEFAULT_BUDGET) -> GoalState:
    g = GoalState(text=text, budget=budget)
    db.set_state_meta(session_id, _key(session_id), g.to_json())
    return g


def get_goal(db: "SessionDB", session_id: str) -> GoalState | None:
    raw = db.get_state_meta(session_id, _key(session_id))
    if raw is None:
        return None
    try:
        return GoalState.from_json(raw)
    except Exception:
        logger.warning("invalid goal state for session %s; clearing", session_id)
        db.set_state_meta(session_id, _key(session_id), None)
        return None


def clear_goal(db: "SessionDB", session_id: str) -> None:
    db.set_state_meta(session_id, _key(session_id), None)


def pause_goal(db: "SessionDB", session_id: str) -> None:
    g = get_goal(db, session_id)
    if g is None:
        return
    g.active = False
    db.set_state_meta(session_id, _key(session_id), g.to_json())


def resume_goal(db: "SessionDB", session_id: str) -> None:
    g = get_goal(db, session_id)
    if g is None:
        return
    g.active = True
    g.turns_used = 0  # reset budget on resume
    db.set_state_meta(session_id, _key(session_id), g.to_json())


def should_continue(db: "SessionDB", session_id: str) -> bool:
    g = get_goal(db, session_id)
    if g is None:
        return False
    if not g.active:
        return False
    if g.turns_used >= g.budget:
        return False
    return True


def increment_turn(db: "SessionDB", session_id: str) -> None:
    g = get_goal(db, session_id)
    if g is None:
        return
    g.turns_used += 1
    db.set_state_meta(session_id, _key(session_id), g.to_json())


CONTINUATION_PROMPT_TEMPLATE = (
    "(continuing toward goal: {goal_text})\n"
    "Take the next concrete step. If the goal is complete, say so explicitly."
)


def build_continuation_prompt(goal_text: str) -> str:
    return CONTINUATION_PROMPT_TEMPLATE.format(goal_text=goal_text)


JUDGE_SYSTEM_PROMPT = (
    "You are a strict goal-satisfaction judge. The user set a standing goal "
    "and the assistant just produced a response. Determine whether the goal "
    "is now satisfied. Respond with ONLY one of: SATISFIED, NOT_SATISFIED."
)

JUDGE_USER_TEMPLATE = "Goal: {goal_text}\n\nLast assistant response:\n{last_response}"


async def _call_judge_model(prompt: str) -> str:
    """Invoke the auxiliary model. Wrapped so tests can monkeypatch."""
    from opencomputer.agent.aux_llm import call_aux_model
    return await call_aux_model(
        system=JUDGE_SYSTEM_PROMPT,
        user=prompt,
        max_tokens=8,  # one word
    )


async def judge_satisfied(*, goal_text: str, last_response: str) -> bool:
    """Returns True if the goal is satisfied, False otherwise. Fails OPEN."""
    if not last_response:
        return False
    try:
        prompt = JUDGE_USER_TEMPLATE.format(
            goal_text=goal_text, last_response=last_response[:4000]
        )
        verdict = await _call_judge_model(prompt)
        return "SATISFIED" in verdict.strip().upper().split()[:1] and "NOT" not in verdict.upper()
    except Exception as exc:
        logger.warning("goal judge call failed (failing open): %s", exc)
        return False  # fail-open: continue the loop
```

- [ ] **Step 2.4: Run unit tests — must pass**

```bash
uv run pytest tests/agent/test_goal.py -v
```
Expected: 6 passed. If any fail, check `SessionDB.set_state_meta` / `get_state_meta` API surface — adjust calls to match.

- [ ] **Step 2.5: Wire `/goal` slash command**

Find where slash commands are registered in `opencomputer/cli_ui/slash_handlers.py` (grep for `@slash_command` or `register_slash`). Add:

```python
# opencomputer/cli_ui/slash_handlers.py — add at appropriate registration point

from opencomputer.agent import goal as _goal


@register_slash("goal")
async def slash_goal(ctx, args: str) -> SlashResult:
    """/goal <text>            set a standing goal
    /goal | /goal status       show current state
    /goal pause                pause continuation loop
    /goal resume               resume (resets turn counter)
    /goal clear                drop the goal
    """
    db = ctx.session_db
    sid = ctx.session_id
    raw = (args or "").strip()
    if not raw or raw == "status":
        g = _goal.get_goal(db, sid)
        if g is None:
            return SlashResult(text="No goal set.")
        state = "active" if g.active else "paused"
        return SlashResult(
            text=f"Goal: {g.text}\nStatus: {state}, turn {g.turns_used}/{g.budget}"
        )
    if raw == "pause":
        _goal.pause_goal(db, sid)
        return SlashResult(text="Goal paused.")
    if raw == "resume":
        _goal.resume_goal(db, sid)
        return SlashResult(text="Goal resumed (turn counter reset).")
    if raw == "clear":
        _goal.clear_goal(db, sid)
        return SlashResult(text="Goal cleared.")
    # Set new goal
    _goal.set_goal(db, sid, raw)
    return SlashResult(text=f"Goal set: {raw}\nI'll work toward this until done, paused, or budget runs out.")
```

- [ ] **Step 2.6: Hook continuation loop into agent loop end-of-turn**

In `opencomputer/agent/loop.py`, locate the end-of-turn hook point (after the assistant's final response is emitted, before the loop awaits the next user message). Add:

```python
from opencomputer.agent import goal as _goal

# After the assistant final-message emission, before await next_user_message:
if _goal.should_continue(self.session_db, self.session_id):
    g = _goal.get_goal(self.session_db, self.session_id)
    last_response = self._last_assistant_text  # or wherever loop tracks this
    satisfied = await _goal.judge_satisfied(
        goal_text=g.text, last_response=last_response
    )
    if not satisfied:
        _goal.increment_turn(self.session_db, self.session_id)
        # Inject continuation as a regular user message — back into the loop
        await self.inject_user_message(_goal.build_continuation_prompt(g.text))
        continue  # loop body re-runs with the new user message
    else:
        # Goal satisfied — clear it and let the user take over
        _goal.clear_goal(self.session_db, self.session_id)
        self._emit_system_message(f"Goal satisfied and cleared: {g.text}")
```

If `inject_user_message` doesn't exist, fall back to appending to the in-memory message list and triggering the next iteration directly.

- [ ] **Step 2.7: Add slash-command integration test**

```python
# tests/cli_ui/test_slash_goal.py
import pytest
from opencomputer.cli_ui.slash_handlers import slash_goal
from opencomputer.agent.session_db import SessionDB


@pytest.mark.asyncio
async def test_slash_goal_set_then_status(tmp_path):
    db = SessionDB(path=str(tmp_path / "s.db"))
    sid = db.create_session()

    class Ctx:
        session_db = db
        session_id = sid

    r = await slash_goal(Ctx(), "ship the wave-5 PR")
    assert "Goal set" in r.text

    r2 = await slash_goal(Ctx(), "")
    assert "ship the wave-5 PR" in r2.text
    assert "active" in r2.text


@pytest.mark.asyncio
async def test_slash_goal_pause_resume_clear(tmp_path):
    db = SessionDB(path=str(tmp_path / "s.db"))
    sid = db.create_session()

    class Ctx:
        session_db = db
        session_id = sid

    await slash_goal(Ctx(), "x")
    r = await slash_goal(Ctx(), "pause")
    assert "paused" in r.text.lower()
    r = await slash_goal(Ctx(), "resume")
    assert "resumed" in r.text.lower()
    r = await slash_goal(Ctx(), "clear")
    assert "cleared" in r.text.lower()
```

- [ ] **Step 2.8: Run all goal tests**

```bash
uv run pytest tests/agent/test_goal.py tests/cli_ui/test_slash_goal.py -v
```
Expected: 8 passed.

- [ ] **Step 2.9: Commit**

```bash
git add opencomputer/agent/goal.py opencomputer/cli_ui/slash_handlers.py opencomputer/agent/loop.py tests/agent/test_goal.py tests/cli_ui/test_slash_goal.py
git commit -m "feat(agent): /goal — persistent cross-turn goals (Ralph loop)

Port hermes-agent 265bd59c1. Standing-goal slash command keeps OC working
toward a stated objective across turns until satisfied (judge call), paused,
or turn budget (20) runs out. State persisted in SessionDB.state_meta keyed
by goal:<session_id>; judge fails OPEN; real user messages preempt.

Complementary to existing Standing Orders (PR #320): orders are continuously
applied; goals are completion-driven.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `/steer` and `/queue` slash commands (ACP + CLI)

**Files:**
- Modify: `opencomputer/acp/server.py` (handle steer/queue commands)
- Modify: `opencomputer/acp/session.py` (queue + interrupt state)
- Modify: `opencomputer/cli_ui/slash_handlers.py` (CLI parity)
- Test: `tests/acp/test_steer_queue.py`

**Hermes reference:** commit `e27b0b765` (`acp_adapter/server.py`, `acp_adapter/session.py`).

**Behavior:**
- `/steer <text>` — interrupt the current turn (if running), inject text as new user message
- `/queue <text>` — append text to a queue that drains after the current turn finishes; if no turn running, behaves like a normal user message

- [ ] **Step 3.1: Write failing tests**

```python
# tests/acp/test_steer_queue.py
import pytest
from opencomputer.acp.session import ACPSession, QueuedMessage


@pytest.mark.asyncio
async def test_steer_interrupts_running_turn():
    sess = ACPSession()
    sess.mark_running()
    await sess.steer("change direction please")
    assert sess.is_interrupted is True
    assert sess.pending_user_text == "change direction please"


@pytest.mark.asyncio
async def test_queue_appends_to_buffer():
    sess = ACPSession()
    sess.mark_running()
    await sess.queue("first followup")
    await sess.queue("second followup")
    assert len(sess.queued) == 2
    assert sess.queued[0].text == "first followup"


@pytest.mark.asyncio
async def test_queue_idle_session_treated_as_normal_message():
    sess = ACPSession()
    # No mark_running() → idle
    await sess.queue("hello")
    assert len(sess.queued) == 1


@pytest.mark.asyncio
async def test_drain_queue_after_turn_ends():
    sess = ACPSession()
    sess.mark_running()
    await sess.queue("a")
    await sess.queue("b")
    sess.mark_idle()
    drained = sess.drain_queue()
    assert [m.text for m in drained] == ["a", "b"]
    assert sess.queued == []
```

- [ ] **Step 3.2: Run tests to verify they fail**

```bash
uv run pytest tests/acp/test_steer_queue.py -v
```
Expected: AttributeError or ImportError.

- [ ] **Step 3.3: Add steer + queue methods to `ACPSession`**

In `opencomputer/acp/session.py`, locate or create the `ACPSession` class. Add:

```python
from dataclasses import dataclass, field


@dataclass(slots=True)
class QueuedMessage:
    text: str


class ACPSession:
    # ... existing fields ...
    is_running: bool = False
    is_interrupted: bool = False
    pending_user_text: str | None = None
    queued: list[QueuedMessage] = field(default_factory=list)

    def mark_running(self) -> None:
        self.is_running = True

    def mark_idle(self) -> None:
        self.is_running = False
        self.is_interrupted = False

    async def steer(self, text: str) -> None:
        """Interrupt the current turn with new user text."""
        self.is_interrupted = True
        self.pending_user_text = text

    async def queue(self, text: str) -> None:
        """Append text to drain after the current turn finishes."""
        self.queued.append(QueuedMessage(text=text))

    def drain_queue(self) -> list[QueuedMessage]:
        out, self.queued = list(self.queued), []
        return out
```

If `ACPSession` is a frozen dataclass or otherwise immutable, switch to a mutable alternative (`@dataclass(slots=True)` without `frozen`).

- [ ] **Step 3.4: Run tests — must pass**

```bash
uv run pytest tests/acp/test_steer_queue.py -v
```
Expected: 4 passed.

- [ ] **Step 3.5: Wire ACP server commands**

In `opencomputer/acp/server.py`, find the slash-command dispatch (search for `prompt[/`, `command_handler`, or `@command`). Add cases:

```python
elif cmd == "/steer":
    await session.steer(args)
    return {"status": "interrupted", "text": args}
elif cmd == "/queue":
    await session.queue(args)
    return {"status": "queued", "text": args}
```

- [ ] **Step 3.6: Wire CLI parity**

In `opencomputer/cli_ui/slash_handlers.py`:

```python
@register_slash("steer")
async def slash_steer(ctx, args: str) -> SlashResult:
    """Inject text as the next user message, interrupting any running turn."""
    if not args:
        return SlashResult(text="Usage: /steer <message>")
    await ctx.acp_session.steer(args)
    return SlashResult(text=f"Steered: {args}")


@register_slash("queue")
async def slash_queue(ctx, args: str) -> SlashResult:
    """Append text to drain after the current turn finishes."""
    if not args:
        return SlashResult(text="Usage: /queue <message>")
    await ctx.acp_session.queue(args)
    return SlashResult(text=f"Queued: {args} ({len(ctx.acp_session.queued)} pending)")
```

If CLI sessions don't carry an `acp_session`, route `/steer` to inject directly into the CLI message buffer instead.

- [ ] **Step 3.7: Run all steer/queue tests + smoke**

```bash
uv run pytest tests/acp/test_steer_queue.py tests/cli_ui/ -v -k "steer or queue"
```
Expected: passes.

- [ ] **Step 3.8: Commit**

```bash
git add opencomputer/acp/server.py opencomputer/acp/session.py opencomputer/cli_ui/slash_handlers.py tests/acp/test_steer_queue.py
git commit -m "feat(acp,cli): /steer + /queue slash commands

Port hermes-agent e27b0b765. /steer interrupts the current turn with
new user text; /queue appends to a buffer that drains after the turn
ends. Available in ACP and CLI/TUI.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `busy_ack_enabled` config + runtime-metadata footer + `/footer` slash

**Files:**
- Create: `opencomputer/gateway/runtime_footer.py`
- Modify: `opencomputer/gateway/dispatch.py` (busy_ack gate + footer append)
- Modify: `opencomputer/cli_ui/slash_handlers.py` (/footer handler)
- Test: `tests/gateway/test_busy_ack.py`, `tests/gateway/test_runtime_footer.py`

**Hermes reference:** commits `2b512cbca` (busy_ack) + `e123f4ecf` (runtime footer).

- [ ] **Step 4.1: Write busy_ack failing test**

```python
# tests/gateway/test_busy_ack.py
import pytest
from opencomputer.gateway.dispatch import should_send_busy_ack


def test_busy_ack_default_enabled():
    assert should_send_busy_ack({}) is True


def test_busy_ack_explicit_false():
    cfg = {"display": {"busy_ack_enabled": False}}
    assert should_send_busy_ack(cfg) is False


def test_busy_ack_explicit_true():
    cfg = {"display": {"busy_ack_enabled": True}}
    assert should_send_busy_ack(cfg) is True
```

- [ ] **Step 4.2: Write runtime_footer failing tests**

```python
# tests/gateway/test_runtime_footer.py
from opencomputer.gateway.runtime_footer import (
    format_runtime_footer,
    resolve_footer_config,
)


def test_footer_default_disabled():
    cfg = resolve_footer_config({})
    assert cfg.enabled is False


def test_footer_renders_pct():
    line = format_runtime_footer(
        model="claude-opus-4-7",
        tokens_used=15000,
        context_length=200000,
        cwd="/Users/saksham/projects/hermes",
    )
    assert "claude-opus-4-7" in line
    assert "8%" in line  # 15000/200000 = 7.5% rounds to 8
    assert "hermes" in line


def test_footer_no_pct_when_context_unknown():
    line = format_runtime_footer(
        model="unknown-model",
        tokens_used=100,
        context_length=None,
        cwd="/x",
    )
    assert "%" not in line


def test_footer_empty_returns_empty():
    assert format_runtime_footer(model="", tokens_used=0, context_length=0, cwd="") == ""


def test_per_platform_override():
    cfg = resolve_footer_config({
        "display": {
            "runtime_footer": {"enabled": False},
            "platforms": {"telegram": {"runtime_footer": {"enabled": True}}},
        }
    }, platform="telegram")
    assert cfg.enabled is True
```

- [ ] **Step 4.3: Run tests to verify they fail**

```bash
uv run pytest tests/gateway/test_busy_ack.py tests/gateway/test_runtime_footer.py -v
```
Expected: ImportError.

- [ ] **Step 4.4: Implement `runtime_footer.py`**

```python
# opencomputer/gateway/runtime_footer.py
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class FooterConfig:
    enabled: bool


def resolve_footer_config(cfg: dict, *, platform: str | None = None) -> FooterConfig:
    display = cfg.get("display") or {}
    base = display.get("runtime_footer") or {}
    enabled = bool(base.get("enabled", False))
    if platform:
        plat = (display.get("platforms") or {}).get(platform) or {}
        plat_footer = plat.get("runtime_footer") or {}
        if "enabled" in plat_footer:
            enabled = bool(plat_footer["enabled"])
    return FooterConfig(enabled=enabled)


def format_runtime_footer(
    *,
    model: str,
    tokens_used: int,
    context_length: int | None,
    cwd: str,
) -> str:
    if not model and not cwd:
        return ""
    parts: list[str] = []
    if model:
        parts.append(model)
    if context_length and tokens_used >= 0:
        pct = round(100.0 * tokens_used / context_length)
        parts.append(f"{pct}%")
    if cwd:
        parts.append(_shorten_cwd(cwd))
    if not parts:
        return ""
    return " · ".join(parts)


def _shorten_cwd(cwd: str) -> str:
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd
```

- [ ] **Step 4.5: Implement `should_send_busy_ack` in `dispatch.py`**

In `opencomputer/gateway/dispatch.py`, add a small helper near the top of file:

```python
def should_send_busy_ack(cfg: dict) -> bool:
    return bool((cfg.get("display") or {}).get("busy_ack_enabled", True))
```

Then find the existing busy-ack send path (grep for `busy` / `ack` / `processing_message`) and wrap it:

```python
if should_send_busy_ack(self.config):
    await self._send_busy_ack(...)
```

- [ ] **Step 4.6: Wire footer append in dispatch.py**

Find where the final assistant message is sent (search for `final_response` or `_send_final` or `dispatch_assistant_message`). After that call, before the loop awaits next input:

```python
from opencomputer.gateway.runtime_footer import resolve_footer_config, format_runtime_footer

footer_cfg = resolve_footer_config(self.config, platform=self.platform_name)
if footer_cfg.enabled:
    line = format_runtime_footer(
        model=self.current_model,
        tokens_used=getattr(agent_result, "last_prompt_tokens", 0),
        context_length=getattr(agent_result, "context_length", None),
        cwd=os.getcwd(),
    )
    if line:
        await self._send_text(line)  # or whatever the platform-agnostic send is
```

- [ ] **Step 4.7: Add /footer slash command**

In `opencomputer/cli_ui/slash_handlers.py`:

```python
@register_slash("footer")
async def slash_footer(ctx, args: str) -> SlashResult:
    """/footer on|off|status — toggle runtime metadata footer."""
    cfg_path = ctx.config_path  # however config is persisted
    arg = (args or "").strip().lower()
    if arg == "status":
        cur = (ctx.config.get("display") or {}).get("runtime_footer", {}).get("enabled", False)
        return SlashResult(text=f"footer: {'on' if cur else 'off'}")
    if arg in ("on", "off"):
        ctx.config.setdefault("display", {}).setdefault("runtime_footer", {})["enabled"] = (arg == "on")
        ctx.persist_config()
        return SlashResult(text=f"footer: {arg}")
    return SlashResult(text="Usage: /footer on|off|status")
```

- [ ] **Step 4.8: Run all tests**

```bash
uv run pytest tests/gateway/test_busy_ack.py tests/gateway/test_runtime_footer.py -v
```
Expected: 8 passed.

- [ ] **Step 4.9: Commit**

```bash
git add opencomputer/gateway/runtime_footer.py opencomputer/gateway/dispatch.py opencomputer/cli_ui/slash_handlers.py tests/gateway/test_busy_ack.py tests/gateway/test_runtime_footer.py
git commit -m "feat(gateway): busy_ack_enabled + runtime-metadata footer + /footer

Port hermes-agent 2b512cbca + e123f4ecf. display.busy_ack_enabled
(default true) gates busy-input ack messages. display.runtime_footer
(default off) appends 'model · pct% · ~/cwd' to the final reply of each
turn. /footer on|off|status slash command. Per-platform overrides.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: OpenRouter response caching

**Files:**
- Modify: `extensions/openrouter-provider/openrouter_adapter.py` (or whatever the adapter file is — confirm with `ls extensions/openrouter-provider/`)
- Test: `tests/extensions/openrouter/test_response_cache.py`

**Hermes reference:** commit `457c7b76c`. Adds `X-OpenRouter-Cache: 1` and `X-OpenRouter-Cache-TTL: <seconds>` headers; reads `X-OpenRouter-Cache-Status: HIT|MISS` from streaming response.

**Note:** This is **distinct** from PR #339's prompt-cache (cache_control on Anthropic); OpenRouter response caching caches the entire LLM response across identical requests. Both can be active simultaneously.

- [ ] **Step 5.1: Confirm adapter file**

```bash
ls extensions/openrouter-provider/
```
Find the file that builds outgoing HTTP headers (likely `openrouter_adapter.py` or `provider.py`).

- [ ] **Step 5.2: Write failing tests**

```python
# tests/extensions/openrouter/test_response_cache.py
import pytest
from extensions.openrouter_provider.openrouter_adapter import (  # adjust import to actual module path
    build_or_headers,
    parse_cache_status,
)


def test_default_cache_on_with_default_ttl():
    headers = build_or_headers({})
    assert headers.get("X-OpenRouter-Cache") == "1"
    assert int(headers.get("X-OpenRouter-Cache-TTL", "0")) == 300


def test_disable_cache():
    headers = build_or_headers({"openrouter": {"response_cache": False}})
    assert "X-OpenRouter-Cache" not in headers


def test_custom_ttl():
    headers = build_or_headers({"openrouter": {"response_cache_ttl": 600}})
    assert headers.get("X-OpenRouter-Cache-TTL") == "600"


def test_ttl_clamped_to_valid_range():
    h_low = build_or_headers({"openrouter": {"response_cache_ttl": 0}})
    h_high = build_or_headers({"openrouter": {"response_cache_ttl": 999_999}})
    assert int(h_low["X-OpenRouter-Cache-TTL"]) == 1
    assert int(h_high["X-OpenRouter-Cache-TTL"]) == 86400


def test_parse_cache_status_hit():
    assert parse_cache_status({"X-OpenRouter-Cache-Status": "HIT"}) == "HIT"


def test_parse_cache_status_miss_default():
    assert parse_cache_status({}) == "MISS"
```

- [ ] **Step 5.3: Implement `build_or_headers` + `parse_cache_status`**

Add to the adapter module (the same one that already builds attribution headers):

```python
def build_or_headers(cfg: dict) -> dict[str, str]:
    or_cfg = cfg.get("openrouter") or {}
    headers: dict[str, str] = {}
    # Attribution headers (existing — keep current values, do not break)
    headers.update(_existing_attribution_headers())
    # New: response cache
    if or_cfg.get("response_cache", True):
        headers["X-OpenRouter-Cache"] = "1"
        ttl = int(or_cfg.get("response_cache_ttl", 300))
        ttl = max(1, min(86400, ttl))
        headers["X-OpenRouter-Cache-TTL"] = str(ttl)
    return headers


def parse_cache_status(response_headers: dict[str, str]) -> str:
    return (
        response_headers.get("X-OpenRouter-Cache-Status")
        or response_headers.get("x-openrouter-cache-status")
        or "MISS"
    )
```

Replace inline header dicts at every site with `build_or_headers(self.config)`. Search the file for the original `_OR_HEADERS` or attribution dict.

- [ ] **Step 5.4: Update DEFAULT_CONFIG**

If OC has a default-config mapping (grep `DEFAULT_CONFIG` in `opencomputer/`), add:

```python
DEFAULT_CONFIG.setdefault("openrouter", {}).setdefault("response_cache", True)
DEFAULT_CONFIG["openrouter"].setdefault("response_cache_ttl", 300)
```

- [ ] **Step 5.5: Log HIT/MISS during streaming response**

In the streaming response path, after the response headers arrive:

```python
status = parse_cache_status(dict(response.headers))
logger.info("openrouter response cache: %s", status)
```

- [ ] **Step 5.6: Run tests**

```bash
uv run pytest tests/extensions/openrouter/test_response_cache.py -v
```
Expected: 6 passed.

- [ ] **Step 5.7: Commit**

```bash
git add extensions/openrouter-provider/ tests/extensions/openrouter/
git commit -m "feat(openrouter): add response caching support

Port hermes-agent 457c7b76c. X-OpenRouter-Cache + X-OpenRouter-Cache-TTL
headers. Default on with 300s TTL. Logs HIT/MISS from response headers.
Distinct from PR #339's prompt-cache (cache_control on Anthropic).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Group A milestone — full pytest run

- [ ] **Step 6.1: Run full Group A test surface**

```bash
uv run pytest tests/agent/test_tool_guardrails.py tests/agent/test_tool_guardrails_integration.py tests/agent/test_goal.py tests/cli_ui/test_slash_goal.py tests/acp/test_steer_queue.py tests/gateway/test_busy_ack.py tests/gateway/test_runtime_footer.py tests/extensions/openrouter/test_response_cache.py -v
```
Expected: all pass.

- [ ] **Step 6.2: Run ruff**

```bash
uv run ruff check opencomputer/agent/goal.py opencomputer/agent/tool_guardrails.py opencomputer/gateway/runtime_footer.py opencomputer/cli_ui/slash_handlers.py opencomputer/acp/server.py opencomputer/acp/session.py extensions/openrouter-provider/
```
Fix any reported issues with `uv run ruff check --fix` or manually.

- [ ] **Step 6.3: Run full pytest (under 5 min wall)**

```bash
uv run pytest -x --timeout=300 2>&1 | tail -20
```
Expected: all pass; document the total count for §10 in the spec.

---

## Group B — Multimodal / Voice (Tasks 7–13)

### Task 7: `video_analyze` tool

**Files:**
- Create: `opencomputer/tools/video_analyze.py` (mirror `vision_analyze.py`)
- Modify: `opencomputer/tools/registry.py` (register the tool)
- Test: `tests/tools/test_video_analyze.py`

**Hermes reference:** commit `c9a3f36f5`. Read full body — design follows `vision_analyze` exactly with `video_url` content block instead of `image_url`.

- [ ] **Step 7.1: Examine OC's `vision_analyze.py` to mirror**

```bash
head -200 opencomputer/tools/vision_analyze.py
```
Note: function name, tool registration pattern, error handling, SSRF guard, retries.

- [ ] **Step 7.2: Write failing tests**

```python
# tests/tools/test_video_analyze.py
import pytest
from opencomputer.tools.video_analyze import (
    video_analyze,
    SUPPORTED_VIDEO_FORMATS,
    MAX_VIDEO_BYTES,
)


def test_supported_formats():
    assert "mp4" in SUPPORTED_VIDEO_FORMATS
    assert "webm" in SUPPORTED_VIDEO_FORMATS
    assert "mov" in SUPPORTED_VIDEO_FORMATS
    assert "avi" in SUPPORTED_VIDEO_FORMATS
    assert "mkv" in SUPPORTED_VIDEO_FORMATS
    assert "mpeg" in SUPPORTED_VIDEO_FORMATS


def test_max_size_50mb():
    assert MAX_VIDEO_BYTES == 50 * 1024 * 1024


@pytest.mark.asyncio
async def test_rejects_unsupported_format(tmp_path):
    p = tmp_path / "x.gif"
    p.write_bytes(b"fakegif")
    with pytest.raises(ValueError, match="Unsupported"):
        await video_analyze(path=str(p), prompt="describe")


@pytest.mark.asyncio
async def test_rejects_oversize(tmp_path):
    p = tmp_path / "big.mp4"
    p.write_bytes(b"\x00" * (MAX_VIDEO_BYTES + 1))
    with pytest.raises(ValueError, match="50"):
        await video_analyze(path=str(p), prompt="describe")


@pytest.mark.asyncio
async def test_happy_path_calls_aux_model(tmp_path, monkeypatch):
    p = tmp_path / "test.mp4"
    p.write_bytes(b"\x00" * 1024)  # 1KB

    captured = {}

    async def fake_aux(*, system, user, content, **kwargs):
        captured["called"] = True
        # Verify video_url content block was built
        assert any(
            isinstance(c, dict) and c.get("type") == "video_url"
            for c in (content or [])
        )
        return "a video of nothing"

    monkeypatch.setattr("opencomputer.tools.video_analyze._call_aux_with_video", fake_aux)
    result = await video_analyze(path=str(p), prompt="describe")
    assert "video of nothing" in result
    assert captured["called"]
```

- [ ] **Step 7.3: Run tests to verify they fail**

```bash
uv run pytest tests/tools/test_video_analyze.py -v
```
Expected: ImportError.

- [ ] **Step 7.4: Implement `video_analyze.py`**

```python
# opencomputer/tools/video_analyze.py
from __future__ import annotations

import base64
import logging
import mimetypes
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_VIDEO_FORMATS: frozenset[str] = frozenset(
    {"mp4", "webm", "mov", "avi", "mkv", "mpeg"}
)
MAX_VIDEO_BYTES: int = 50 * 1024 * 1024
WARN_VIDEO_BYTES: int = 20 * 1024 * 1024
MIN_TIMEOUT_S: float = 180.0


def _ext(path: str) -> str:
    return Path(path).suffix.lower().lstrip(".")


def _mime_for(ext: str) -> str:
    return mimetypes.types_map.get("." + ext, f"video/{ext}")


async def _call_aux_with_video(*, system: str, user: str, content: list[dict], **kwargs):
    """Invoke aux model with video content block. Wrapped for testability."""
    from opencomputer.agent.aux_llm import call_aux_model_multimodal
    return await call_aux_model_multimodal(
        system=system, user=user, content=content, timeout=kwargs.pop("timeout", MIN_TIMEOUT_S),
    )


async def video_analyze(*, path: str, prompt: str, model: str | None = None) -> str:
    """Analyze a video file via a multimodal LLM. Returns text description.

    Mirrors vision_analyze: base64-encodes the file and sends as a video_url
    content block (OpenRouter standard).
    """
    ext = _ext(path)
    if ext not in SUPPORTED_VIDEO_FORMATS:
        raise ValueError(
            f"Unsupported video format: .{ext}. Supported: {sorted(SUPPORTED_VIDEO_FORMATS)}"
        )
    size = os.path.getsize(path)
    if size > MAX_VIDEO_BYTES:
        raise ValueError(
            f"Video exceeds {MAX_VIDEO_BYTES // (1024*1024)} MB cap (file is {size // (1024*1024)} MB)"
        )
    if size > WARN_VIDEO_BYTES:
        logger.warning("video_analyze: %s is %d MB — large videos take longer", path, size // (1024*1024))
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    mime = _mime_for(ext)
    data_url = f"data:{mime};base64,{b64}"
    content = [
        {"type": "text", "text": prompt or "Describe this video."},
        {"type": "video_url", "video_url": {"url": data_url}},
    ]
    aux_model = (
        model
        or os.environ.get("AUXILIARY_VIDEO_MODEL")
        or os.environ.get("AUXILIARY_VISION_MODEL")
    )
    return await _call_aux_with_video(
        system="You are a careful video analyst. Be concise and concrete.",
        user=prompt,
        content=content,
        model=aux_model,
        timeout=MIN_TIMEOUT_S,
    )
```

- [ ] **Step 7.5: Register the tool**

In `opencomputer/tools/registry.py` find where `vision_analyze` is registered. Add:

```python
from opencomputer.tools.video_analyze import video_analyze

# In the registration block:
register_tool(
    name="video_analyze",
    func=video_analyze,
    description="Analyze a video file via multimodal LLM. Returns text description.",
    parameters={
        "path": {"type": "string", "required": True},
        "prompt": {"type": "string", "required": True},
        "model": {"type": "string", "required": False},
    },
    toolset="video",
    default_off=True,
)
```

If OC's tool registry uses a different shape (e.g. dataclass-based), adapt accordingly — copy the exact pattern used for `vision_analyze`.

- [ ] **Step 7.6: Run tests**

```bash
uv run pytest tests/tools/test_video_analyze.py -v
```
Expected: 5 passed.

- [ ] **Step 7.7: Commit**

```bash
git add opencomputer/tools/video_analyze.py opencomputer/tools/registry.py tests/tools/test_video_analyze.py
git commit -m "feat(tools): add video_analyze tool

Port hermes-agent c9a3f36f5. Mirror of vision_analyze for video files.
Base64 + video_url content block (OpenRouter standard). 50 MB cap, 180s
min timeout. AUXILIARY_VIDEO_MODEL env override falls back to
AUXILIARY_VISION_MODEL. Default-off in 'video' toolset.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Piper TTS native provider

**Files:**
- Create: `opencomputer/voice/tts_piper.py`
- Modify: `opencomputer/voice/tts.py` (register piper provider)
- Test: `tests/voice/test_tts_piper.py`

**Hermes reference:** commit `8d302e37a`. Read full body for design.

**Behavior:** Lazy import (`piper` only imported when used). Voice cache keyed on `(model_path, use_cuda)`. Auto-download voice if it's a name; expect path if it's a path. WAV output then optional ffmpeg conversion.

- [ ] **Step 8.1: Examine OC's existing TTS structure**

```bash
head -80 opencomputer/voice/tts.py
ls opencomputer/voice/
```
Note where edge_tts is registered.

- [ ] **Step 8.2: Write failing tests**

```python
# tests/voice/test_tts_piper.py
import pytest
from pathlib import Path
from opencomputer.voice.tts_piper import (
    PiperTTS,
    PiperConfig,
    DEFAULT_VOICE,
)


def test_default_voice_is_lessac():
    assert DEFAULT_VOICE == "en_US-lessac-medium"


def test_config_defaults():
    cfg = PiperConfig()
    assert cfg.voice == DEFAULT_VOICE
    assert cfg.use_cuda is False


@pytest.mark.asyncio
async def test_lazy_import_no_piper_installed(monkeypatch):
    """If piper-tts is not installed, instantiation should raise informative error."""
    import sys
    monkeypatch.setitem(sys.modules, "piper", None)
    p = PiperTTS(PiperConfig())
    with pytest.raises(RuntimeError, match="pip install piper-tts"):
        await p.synthesize("hello", out_path="/tmp/x.wav")


@pytest.mark.asyncio
async def test_voice_cache_reuses(monkeypatch, tmp_path):
    """Same voice path → same cached PiperVoice instance."""
    fake_voice = object()
    calls = {"n": 0}

    def fake_load(path, use_cuda=False):
        calls["n"] += 1
        return fake_voice

    monkeypatch.setattr("opencomputer.voice.tts_piper._load_voice", fake_load)
    cfg = PiperConfig(voice=str(tmp_path / "x.onnx"))
    p1 = PiperTTS(cfg)
    p2 = PiperTTS(cfg)
    p1._get_voice()
    p2._get_voice()
    assert calls["n"] == 1
```

- [ ] **Step 8.3: Run tests to verify they fail**

```bash
uv run pytest tests/voice/test_tts_piper.py -v
```
Expected: ImportError.

- [ ] **Step 8.4: Implement `tts_piper.py`**

```python
# opencomputer/voice/tts_piper.py
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_VOICE: str = "en_US-lessac-medium"


@dataclass(slots=True, frozen=True)
class PiperConfig:
    voice: str = DEFAULT_VOICE
    use_cuda: bool = False
    length_scale: float | None = None
    noise_scale: float | None = None
    noise_w_scale: float | None = None
    volume: float | None = None
    normalize_audio: bool | None = None


def _voice_cache_dir() -> Path:
    base = Path(os.environ.get("OC_HOME", str(Path.home() / ".opencomputer"))) / "cache" / "piper-voices"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _import_piper():
    """Lazy import — raises if piper-tts not installed."""
    try:
        import piper  # noqa
        return piper
    except ImportError:
        raise RuntimeError(
            "Piper TTS requires the piper-tts package. Install with: pip install piper-tts"
        )


def _resolve_voice_path(voice: str) -> Path:
    """If voice is a path → use as-is. If voice is a name → download into cache."""
    p = Path(voice)
    if p.suffix == ".onnx" and p.exists():
        return p
    cache = _voice_cache_dir()
    target = cache / f"{voice}.onnx"
    if target.exists():
        return target
    # Use piper.download_voices CLI to fetch
    logger.info("Downloading Piper voice %s into %s", voice, cache)
    subprocess.run(
        ["python", "-m", "piper.download_voices", "--download-dir", str(cache), voice],
        check=True,
    )
    return target


@lru_cache(maxsize=8)
def _load_voice(path: str, use_cuda: bool):
    """Cache voice instances keyed on (path, use_cuda). lru_cache key matches both args."""
    piper = _import_piper()
    return piper.PiperVoice.load(path, use_cuda=use_cuda)


class PiperTTS:
    def __init__(self, config: PiperConfig | None = None) -> None:
        self.config = config or PiperConfig()

    def _get_voice(self):
        path = _resolve_voice_path(self.config.voice)
        return _load_voice(str(path), self.config.use_cuda)

    async def synthesize(self, text: str, *, out_path: str) -> str:
        voice = self._get_voice()  # may raise RuntimeError if piper missing
        piper = _import_piper()
        synth_kwargs: dict[str, object] = {}
        if self.config.length_scale is not None:
            synth_kwargs["length_scale"] = self.config.length_scale
        if self.config.noise_scale is not None:
            synth_kwargs["noise_scale"] = self.config.noise_scale
        if self.config.noise_w_scale is not None:
            synth_kwargs["noise_w_scale"] = self.config.noise_w_scale
        if self.config.volume is not None:
            synth_kwargs["volume"] = self.config.volume
        if self.config.normalize_audio is not None:
            synth_kwargs["normalize_audio"] = self.config.normalize_audio
        # piper.PiperVoice.synthesize_wav() is sync — run in thread
        await asyncio.to_thread(
            voice.synthesize_wav, text, out_path, **synth_kwargs
        )
        return out_path
```

- [ ] **Step 8.5: Wire into `tts.py` registry**

In `opencomputer/voice/tts.py`, find the existing TTS dispatch (where `edge_tts` is hooked in). Add:

```python
from opencomputer.voice.tts_piper import PiperTTS, PiperConfig

BUILTIN_TTS_PROVIDERS = ("edge", "openai", "elevenlabs", "piper")

def _get_piper_provider(cfg: dict) -> PiperTTS:
    piper_cfg = (cfg.get("tts") or {}).get("piper") or {}
    return PiperTTS(PiperConfig(
        voice=piper_cfg.get("voice", "en_US-lessac-medium"),
        use_cuda=bool(piper_cfg.get("use_cuda", False)),
        length_scale=piper_cfg.get("length_scale"),
        noise_scale=piper_cfg.get("noise_scale"),
    ))

# Then in the dispatch table / match:
elif provider == "piper":
    tts = _get_piper_provider(cfg)
    return await tts.synthesize(text, out_path=out_path)
```

- [ ] **Step 8.6: Run tests**

```bash
uv run pytest tests/voice/test_tts_piper.py -v
```
Expected: 4 passed.

- [ ] **Step 8.7: Commit**

```bash
git add opencomputer/voice/tts_piper.py opencomputer/voice/tts.py tests/voice/test_tts_piper.py
git commit -m "feat(voice): add Piper as a native local TTS provider

Port hermes-agent 8d302e37a. Local neural TTS, 44 languages, no API key.
Lazy import; voice cache by (path, cuda); auto-download voice by name.
Default voice en_US-lessac-medium. Optional knobs: length_scale,
noise_scale, etc. Builtin name 'piper' shadowing prevented.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: TTS command-type provider registry

**Files:**
- Create: `opencomputer/voice/tts_command.py`
- Modify: `opencomputer/voice/tts.py` (route command-type providers)
- Test: `tests/voice/test_tts_command.py`

**Hermes reference:** commit `2facea7f7`.

**Behavior:** User declares any number of named providers in config under `tts.providers.<name>` with `type: command` and a command template. Built-in names cannot be shadowed.

- [ ] **Step 9.1: Write failing tests**

```python
# tests/voice/test_tts_command.py
import pytest
from opencomputer.voice.tts_command import (
    CommandTTSConfig,
    expand_placeholders,
    BUILTIN_NAMES_BLOCKED,
)


def test_builtin_names_blocked():
    assert "edge" in BUILTIN_NAMES_BLOCKED
    assert "piper" in BUILTIN_NAMES_BLOCKED
    assert "openai" in BUILTIN_NAMES_BLOCKED


def test_expand_placeholders_basic():
    out = expand_placeholders(
        "say --voice {voice} {input_path}",
        input_path="/tmp/in.txt", output_path="/tmp/out.wav", voice="bob"
    )
    assert "/tmp/in.txt" in out
    assert "bob" in out


def test_expand_preserves_literal_braces():
    out = expand_placeholders(
        "echo {{literal}} > {output_path}",
        input_path="i", output_path="/tmp/o", voice="v"
    )
    assert "{literal}" in out
    assert "/tmp/o" in out


def test_expand_shell_quotes_paths_with_spaces():
    out = expand_placeholders(
        "say {input_path}",
        input_path="/tmp/has space/in.txt", output_path="o", voice="v"
    )
    # Must be quoted somehow
    assert "/tmp/has space/in.txt" in out
    assert ("'" in out or '"' in out)


def test_config_validates_required_keys():
    with pytest.raises(ValueError, match="command"):
        CommandTTSConfig.from_dict({})  # missing command
```

- [ ] **Step 9.2: Run failing**

```bash
uv run pytest tests/voice/test_tts_command.py -v
```
Expected: ImportError.

- [ ] **Step 9.3: Implement `tts_command.py`**

```python
# opencomputer/voice/tts_command.py
from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass

BUILTIN_NAMES_BLOCKED: frozenset[str] = frozenset({
    "edge", "openai", "elevenlabs", "piper", "neutts", "kittentts",
})

PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
LITERAL_OPEN = re.compile(r"\{\{")
LITERAL_CLOSE = re.compile(r"\}\}")


@dataclass(slots=True, frozen=True)
class CommandTTSConfig:
    command: str
    output_format: str = "wav"

    @classmethod
    def from_dict(cls, d: dict) -> "CommandTTSConfig":
        cmd = d.get("command")
        if not cmd:
            raise ValueError("Command-type TTS provider requires 'command'")
        return cls(command=cmd, output_format=d.get("output_format", "wav"))


def expand_placeholders(
    template: str,
    *,
    input_path: str,
    output_path: str,
    voice: str = "",
    text_path: str | None = None,
    model: str = "",
    speed: str = "",
    fmt: str = "wav",
) -> str:
    """Substitute placeholders, shell-quote-aware. {{ / }} = literal { / }.
    Quotes the substituted value if it isn't already quoted in the template."""
    # First, substitute literal braces with sentinels
    s = template.replace("{{", "\x00").replace("}}", "\x01")
    mapping = {
        "input_path": input_path,
        "output_path": output_path,
        "voice": voice,
        "text_path": text_path or input_path,
        "model": model,
        "speed": speed,
        "format": fmt,
    }
    def _sub(m: re.Match) -> str:
        key = m.group(1)
        val = mapping.get(key, "")
        return shlex.quote(val) if val else ""
    s = PLACEHOLDER_RE.sub(_sub, s)
    # Restore literals
    s = s.replace("\x00", "{").replace("\x01", "}")
    return s


async def run_command_tts(
    cfg: CommandTTSConfig,
    *,
    input_path: str,
    output_path: str,
    voice: str = "",
) -> str:
    cmd = expand_placeholders(
        cfg.command,
        input_path=input_path,
        output_path=output_path,
        voice=voice,
        fmt=cfg.output_format,
    )
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Command TTS failed (exit={proc.returncode}): {err.decode(errors='replace')[:500]}")
    return output_path
```

- [ ] **Step 9.4: Wire into `tts.py`**

In `opencomputer/voice/tts.py`, register command-type providers. After loading config:

```python
from opencomputer.voice.tts_command import (
    BUILTIN_NAMES_BLOCKED, CommandTTSConfig, run_command_tts,
)

def resolve_tts_provider(provider_name: str, cfg: dict):
    # Built-ins win even if user defines a same-named entry
    if provider_name in BUILTIN_NAMES_BLOCKED:
        return _builtin_dispatch(provider_name, cfg)
    user = (cfg.get("tts") or {}).get("providers", {}).get(provider_name)
    if user and user.get("type", "command") == "command":
        return CommandTTSConfig.from_dict(user)
    raise ValueError(f"Unknown TTS provider: {provider_name}")
```

- [ ] **Step 9.5: Run tests**

```bash
uv run pytest tests/voice/test_tts_command.py -v
```
Expected: 5 passed.

- [ ] **Step 9.6: Commit**

```bash
git add opencomputer/voice/tts_command.py opencomputer/voice/tts.py tests/voice/test_tts_command.py
git commit -m "feat(voice): add command-type TTS provider registry

Port hermes-agent 2facea7f7. tts.providers.<name> with type=command lets
users wire any local CLI (festival, espeak, custom Piper paths) without
Python code. Placeholders: {input_path}, {output_path}, {voice}, etc.;
shell-quote-aware. Built-in names cannot be shadowed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: `send_multiple_images` — channel contract base

**Files:**
- Modify: `plugin_sdk/channel_contract.py` (add abstract method with default loop)
- Test: `tests/plugin_sdk/test_channel_contract_multi_image.py`

**Hermes reference:** commit `3de8e2168`.

- [ ] **Step 10.1: Write failing test**

```python
# tests/plugin_sdk/test_channel_contract_multi_image.py
import pytest
from plugin_sdk.channel_contract import BaseChannelAdapter


class FakeAdapter(BaseChannelAdapter):
    name = "fake"
    sent_singles: list = []

    # Required abstract methods (stub)
    async def connect(self): pass
    async def disconnect(self): pass
    async def send_message(self, *a, **kw): pass

    async def send_image(self, *, target, image_path, caption=None, **kw):
        self.sent_singles.append((target, image_path, caption))


@pytest.mark.asyncio
async def test_send_multiple_images_default_loops_send_image():
    a = FakeAdapter()
    await a.send_multiple_images(
        target="chat:1",
        image_paths=["/a.png", "/b.png", "/c.png"],
        caption="batch",
    )
    assert len(a.sent_singles) == 3
    assert all(s[2] == "batch" for s in a.sent_singles)


@pytest.mark.asyncio
async def test_send_multiple_images_empty_is_noop():
    a = FakeAdapter()
    await a.send_multiple_images(target="chat:1", image_paths=[])
    assert a.sent_singles == []
```

- [ ] **Step 10.2: Run failing**

```bash
uv run pytest tests/plugin_sdk/test_channel_contract_multi_image.py -v
```
Expected: AttributeError on `send_multiple_images`.

- [ ] **Step 10.3: Add method to `BaseChannelAdapter`**

In `plugin_sdk/channel_contract.py`, after the `send_image()` abstract method (around line 228), add:

```python
async def send_multiple_images(
    self,
    *,
    target: str,
    image_paths: list[str],
    caption: str | None = None,
    **kwargs,
) -> None:
    """Send N images to a target. Default: sequential single-image loop.

    Override per-platform when the channel API has a native batch
    (e.g. Telegram media_group, Discord files=[...]). On override,
    fall back to this default loop on any failure.
    """
    for path in image_paths:
        await self.send_image(
            target=target, image_path=path, caption=caption, **kwargs
        )
```

- [ ] **Step 10.4: Run tests**

```bash
uv run pytest tests/plugin_sdk/test_channel_contract_multi_image.py -v
```
Expected: 2 passed.

- [ ] **Step 10.5: Commit (no platform overrides yet)**

```bash
git add plugin_sdk/channel_contract.py tests/plugin_sdk/test_channel_contract_multi_image.py
git commit -m "feat(plugin_sdk): add send_multiple_images to channel contract

Default implementation loops send_image. Per-platform overrides land in
follow-up commits (telegram, discord, slack, mattermost, email, signal).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Per-platform `send_multiple_images` overrides

**Files:**
- Modify: `extensions/telegram/{adapter,channel}.py` — `send_media_group` (album, 10/batch)
- Modify: `extensions/discord/{adapter,channel}.py` — `channel.send(files=[...])` (10/msg)
- Modify: `extensions/slack/{adapter,channel}.py` — `files_upload_v2` (10/call)
- Modify: `extensions/mattermost/{adapter,channel}.py` — file_ids list (5/post)
- Modify: `extensions/email/{adapter,channel}.py` — single SMTP, multiple MIME parts
- Modify: `extensions/signal/{adapter,channel}.py` — multi-attachment send
- Test: `tests/gateway/test_send_multiple_images_overrides.py`

**Hermes reference:** commits `3de8e2168` + `04ea895ff` (signal).

For each platform, write the override and a chunking test. **Hermes' implementation files** under `gateway/platforms/` are the authoritative source; copy the chunk-size + animation-peel-off rules verbatim.

- [ ] **Step 11.1: Find each adapter's `send_image` site**

```bash
for d in telegram discord slack mattermost email signal; do
  echo "=== $d ==="
  grep -n "send_image\|class .*Adapter" extensions/$d/*.py 2>/dev/null | head -3
done
```

- [ ] **Step 11.2: Telegram override**

```python
# extensions/telegram/adapter.py — inside the class
async def send_multiple_images(self, *, target, image_paths, caption=None, **kw):
    if not image_paths:
        return
    # Split animations off (gif/webm peel-off)
    statics = [p for p in image_paths if not p.lower().endswith((".gif", ".webm"))]
    animated = [p for p in image_paths if p.lower().endswith((".gif", ".webm"))]
    try:
        for i in range(0, len(statics), 10):
            chunk = statics[i:i+10]
            media = [
                {"type": "photo", "media": open(p, "rb"),
                 "caption": (caption if i == 0 and j == 0 else None)}
                for j, p in enumerate(chunk)
            ]
            await self.bot.send_media_group(chat_id=target, media=media)
        for p in animated:
            await self.bot.send_animation(chat_id=target, animation=open(p, "rb"), caption=caption)
    except Exception:
        # Fallback to base per-image loop
        await super().send_multiple_images(target=target, image_paths=image_paths, caption=caption, **kw)
```

- [ ] **Step 11.3: Discord override**

```python
# extensions/discord/adapter.py
async def send_multiple_images(self, *, target, image_paths, caption=None, **kw):
    if not image_paths:
        return
    try:
        for i in range(0, len(image_paths), 10):
            chunk = image_paths[i:i+10]
            files = [discord.File(p) for p in chunk]
            await self._channel_for(target).send(content=(caption if i == 0 else None), files=files)
    except Exception:
        await super().send_multiple_images(target=target, image_paths=image_paths, caption=caption, **kw)
```

- [ ] **Step 11.4: Slack override**

```python
# extensions/slack/adapter.py
async def send_multiple_images(self, *, target, image_paths, caption=None, thread_ts=None, **kw):
    if not image_paths:
        return
    try:
        for i in range(0, len(image_paths), 10):
            chunk = image_paths[i:i+10]
            uploads = [{"file": p} for p in chunk]
            await self.client.files_upload_v2(
                channel=target, file_uploads=uploads,
                initial_comment=(caption if i == 0 else None),
                thread_ts=thread_ts,
            )
    except Exception:
        await super().send_multiple_images(target=target, image_paths=image_paths, caption=caption, **kw)
```

- [ ] **Step 11.5: Mattermost override (5/post)**

```python
# extensions/mattermost/adapter.py
async def send_multiple_images(self, *, target, image_paths, caption=None, **kw):
    if not image_paths:
        return
    try:
        for i in range(0, len(image_paths), 5):
            chunk = image_paths[i:i+5]
            file_ids = [await self._upload_file(target, p) for p in chunk]
            await self.client.posts.create_post(
                channel_id=target,
                message=(caption if i == 0 else ""),
                file_ids=file_ids,
            )
    except Exception:
        await super().send_multiple_images(target=target, image_paths=image_paths, caption=caption, **kw)
```

- [ ] **Step 11.6: Email override**

```python
# extensions/email/adapter.py
async def send_multiple_images(self, *, target, image_paths, caption=None, **kw):
    if not image_paths:
        return
    try:
        msg = MIMEMultipart()
        msg["To"] = target
        msg["Subject"] = caption or "Images"
        msg.attach(MIMEText(caption or "", "plain"))
        for p in image_paths:
            with open(p, "rb") as f:
                part = MIMEImage(f.read(), name=os.path.basename(p))
            msg.attach(part)
        await self._smtp_send(msg)
    except Exception:
        await super().send_multiple_images(target=target, image_paths=image_paths, caption=caption, **kw)
```

- [ ] **Step 11.7: Signal override**

```python
# extensions/signal/adapter.py
async def send_multiple_images(self, *, target, image_paths, caption=None, **kw):
    if not image_paths:
        return
    try:
        # signal-cli send -a <attach> -a <attach> ...
        cmd = ["signal-cli", "send", "-m", caption or "", "-a", *image_paths, target]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(err.decode(errors="replace")[:200])
    except Exception:
        await super().send_multiple_images(target=target, image_paths=image_paths, caption=caption, **kw)
```

- [ ] **Step 11.8: Tests for each platform**

```python
# tests/gateway/test_send_multiple_images_overrides.py
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_telegram_chunks_10():
    from extensions.telegram.adapter import TelegramAdapter  # adjust if class name differs
    a = TelegramAdapter.__new__(TelegramAdapter)
    a.bot = MagicMock()
    a.bot.send_media_group = AsyncMock()
    a.bot.send_animation = AsyncMock()
    paths = [f"/img{i}.png" for i in range(15)]
    await a.send_multiple_images(target="chat:1", image_paths=paths)
    # 15 photos → 2 send_media_group calls (10 + 5)
    assert a.bot.send_media_group.await_count == 2


@pytest.mark.asyncio
async def test_telegram_animation_peels_off():
    from extensions.telegram.adapter import TelegramAdapter
    a = TelegramAdapter.__new__(TelegramAdapter)
    a.bot = MagicMock()
    a.bot.send_media_group = AsyncMock()
    a.bot.send_animation = AsyncMock()
    paths = ["/a.png", "/b.gif", "/c.png"]
    await a.send_multiple_images(target="chat:1", image_paths=paths)
    assert a.bot.send_media_group.await_count == 1  # 2 statics
    assert a.bot.send_animation.await_count == 1   # 1 gif


@pytest.mark.asyncio
async def test_mattermost_chunks_5():
    from extensions.mattermost.adapter import MattermostAdapter
    a = MattermostAdapter.__new__(MattermostAdapter)
    a.client = MagicMock()
    a.client.posts.create_post = AsyncMock()
    a._upload_file = AsyncMock(return_value="fid")
    paths = [f"/img{i}.png" for i in range(7)]
    await a.send_multiple_images(target="chan:1", image_paths=paths)
    assert a.client.posts.create_post.await_count == 2  # 5 + 2


@pytest.mark.asyncio
async def test_fallback_on_native_failure():
    from extensions.discord.adapter import DiscordAdapter
    a = DiscordAdapter.__new__(DiscordAdapter)
    a._channel_for = MagicMock(side_effect=RuntimeError("api down"))
    a.send_image = AsyncMock()
    await a.send_multiple_images(target="c:1", image_paths=["/a.png", "/b.png"])
    # Native failed → fell back to base loop → send_image called twice
    assert a.send_image.await_count == 2
```

If a platform's adapter class name or attribute names differ in OC's actual code, adjust the test instantiation accordingly. Class names can be confirmed by `grep -n "class .*Adapter" extensions/<name>/*.py`.

- [ ] **Step 11.9: Run tests**

```bash
uv run pytest tests/gateway/test_send_multiple_images_overrides.py -v
```
Expected: passes (skip platforms whose actual class shape differs and write small TODOs in those tests).

- [ ] **Step 11.10: Commit**

```bash
git add extensions/telegram extensions/discord extensions/slack extensions/mattermost extensions/email extensions/signal tests/gateway/test_send_multiple_images_overrides.py
git commit -m "feat(channels): native send_multiple_images for 6 platforms

Port hermes-agent 3de8e2168 + 04ea895ff. Telegram(media_group),
Discord(send files=[]), Slack(files_upload_v2), Mattermost(file_ids list),
Email(MIME multi-attach), Signal(signal-cli multi -a). Each falls back
to base per-image loop on API failure. Telegram peels animations to
send_animation (albums don't support animations).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Group B milestone

- [ ] **Step 12.1: Run all Group B tests**

```bash
uv run pytest tests/tools/test_video_analyze.py tests/voice/ tests/plugin_sdk/test_channel_contract_multi_image.py tests/gateway/test_send_multiple_images_overrides.py -v
```
Expected: all pass.

- [ ] **Step 12.2: Ruff Group B**

```bash
uv run ruff check opencomputer/tools/video_analyze.py opencomputer/voice/ plugin_sdk/channel_contract.py extensions/telegram extensions/discord extensions/slack extensions/mattermost extensions/email extensions/signal
```
Fix issues.

---

## Group C — Plugin Platform (Tasks 13–15)

### Task 13: `pre_gateway_dispatch` hook

**Files:**
- Modify: `plugin_sdk/hooks.py` (add `PRE_GATEWAY_DISPATCH` event + fields)
- Modify: `opencomputer/gateway/server.py` (fire hook in message handler)
- Test: `tests/plugin_sdk/test_pre_gateway_dispatch.py`

**Hermes reference:** commit `1ef1e4c66`.

- [ ] **Step 13.1: Add HookEvent + HookContext fields**

In `plugin_sdk/hooks.py`:

```python
class HookEvent(str, Enum):
    # ... existing entries ...
    PRE_GATEWAY_DISPATCH = "PreGatewayDispatch"
    PRE_APPROVAL_REQUEST = "PreApprovalRequest"
    POST_APPROVAL_RESPONSE = "PostApprovalResponse"


# Update ALL_HOOK_EVENTS tuple to include the new entries (preserve order)
ALL_HOOK_EVENTS: tuple[HookEvent, ...] = (
    # ... existing ...
    HookEvent.PRE_GATEWAY_DISPATCH,
    HookEvent.PRE_APPROVAL_REQUEST,
    HookEvent.POST_APPROVAL_RESPONSE,
)


@dataclass(frozen=True, slots=True)
class HookContext:
    # ... existing fields ...
    #: Gateway message text — populated for PRE_GATEWAY_DISPATCH.
    gateway_event_text: str | None = None
    #: Sender identifier (channel-specific) — populated for PRE_GATEWAY_DISPATCH.
    sender_id: str | None = None
    #: Approval surface — "cli" or "gateway" — for PRE/POST_APPROVAL_*.
    surface: str | None = None
    #: Command being approved.
    command: str | None = None
    #: User choice on POST_APPROVAL_RESPONSE — once|session|always|deny|timeout.
    choice: str | None = None
    #: Tool dispatch latency in ms — for POST_TOOL_USE / TRANSFORM_TOOL_RESULT.
    duration_ms: int | None = None
```

- [ ] **Step 13.2: Update `HookDecision` for action/skip/rewrite**

```python
@dataclass(frozen=True, slots=True)
class HookDecision:
    decision: Literal["approve", "block", "pass", "skip", "rewrite", "allow"] = "pass"
    rewritten_text: str | None = None  # for rewrite
    reason: str | None = None
```

- [ ] **Step 13.3: Write failing test**

```python
# tests/plugin_sdk/test_pre_gateway_dispatch.py
import pytest
from plugin_sdk.hooks import HookEvent, HookContext, HookDecision
from opencomputer.hooks.engine import HookEngine


@pytest.mark.asyncio
async def test_pre_gateway_dispatch_skip_drops_message():
    engine = HookEngine()
    captured = []

    async def my_hook(ctx: HookContext) -> HookDecision:
        captured.append(ctx.gateway_event_text)
        return HookDecision(decision="skip", reason="filter")

    engine.register(HookEvent.PRE_GATEWAY_DISPATCH, my_hook)
    decision = await engine.fire_blocking(HookContext(
        event=HookEvent.PRE_GATEWAY_DISPATCH,
        session_id="s1",
        gateway_event_text="hello",
        sender_id="user-1",
    ))
    assert decision.decision == "skip"
    assert captured == ["hello"]


@pytest.mark.asyncio
async def test_pre_gateway_dispatch_rewrite():
    engine = HookEngine()

    async def hook(ctx):
        return HookDecision(decision="rewrite", rewritten_text="REWRITTEN")

    engine.register(HookEvent.PRE_GATEWAY_DISPATCH, hook)
    d = await engine.fire_blocking(HookContext(
        event=HookEvent.PRE_GATEWAY_DISPATCH, session_id="s1",
        gateway_event_text="orig", sender_id="u1",
    ))
    assert d.decision == "rewrite"
    assert d.rewritten_text == "REWRITTEN"


@pytest.mark.asyncio
async def test_plugin_crash_swallowed():
    engine = HookEngine()

    async def boom(ctx):
        raise RuntimeError("plugin bug")

    engine.register(HookEvent.PRE_GATEWAY_DISPATCH, boom)
    # Should NOT raise; engine swallows
    d = await engine.fire_blocking(HookContext(
        event=HookEvent.PRE_GATEWAY_DISPATCH, session_id="s1",
        gateway_event_text="x", sender_id="u",
    ))
    # Without a real verdict, default = "pass"
    assert d.decision == "pass"
```

- [ ] **Step 13.4: Wire fire site in `gateway/server.py`**

Find the `_handle_message` (or equivalent) entry point. Right after the internal-event guard, before auth checks:

```python
from plugin_sdk.hooks import HookEvent, HookContext

decision = await self.hook_engine.fire_blocking(HookContext(
    event=HookEvent.PRE_GATEWAY_DISPATCH,
    session_id=session_id,
    gateway_event_text=event.text,
    sender_id=event.sender_id,
))
if decision.decision == "skip":
    logger.info("pre_gateway_dispatch: dropping message (reason=%s)", decision.reason)
    return
elif decision.decision == "rewrite" and decision.rewritten_text:
    event.text = decision.rewritten_text
# decision.decision == "allow" or "pass" — proceed normally
```

- [ ] **Step 13.5: Run tests**

```bash
uv run pytest tests/plugin_sdk/test_pre_gateway_dispatch.py -v
```
Expected: 3 passed.

- [ ] **Step 13.6: Commit**

```bash
git add plugin_sdk/hooks.py opencomputer/gateway/server.py tests/plugin_sdk/test_pre_gateway_dispatch.py
git commit -m "feat(plugins): add pre_gateway_dispatch hook

Port hermes-agent 1ef1e4c66. Fires once per gateway message before auth,
allowing plugins to skip/rewrite/allow. Hook fires before auth on purpose
(enables audit + handover ingest plugins). Plugin crashes swallowed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 14: `pre_approval_request` + `post_approval_response` hooks

**Files:**
- Modify: `opencomputer/agent/trust_ramp.py` (or wherever the dangerous-command prompt is) — fire both hooks
- Test: `tests/agent/test_approval_hooks.py`

**Hermes reference:** commit `30307a980` — observer-only hooks; return values ignored; plugin crashes don't break flow.

- [ ] **Step 14.1: Locate OC's approval gate**

```bash
grep -n "consent_for_command\|prompt.*approval\|user_choice\|y/n\|once.*always\|deny.*allow" opencomputer/agent/trust_ramp.py opencomputer/agent/policy_audit.py opencomputer/security/ 2>&1 | head -20
```
Identify the function that emits the prompt.

- [ ] **Step 14.2: Write failing tests**

```python
# tests/agent/test_approval_hooks.py
import pytest
from plugin_sdk.hooks import HookEvent, HookContext, HookDecision
from opencomputer.hooks.engine import HookEngine


@pytest.mark.asyncio
async def test_pre_approval_observed():
    engine = HookEngine()
    seen = []

    async def hook(ctx):
        seen.append((ctx.surface, ctx.command))
        return HookDecision(decision="pass")

    engine.register(HookEvent.PRE_APPROVAL_REQUEST, hook)
    await engine.fire_blocking(HookContext(
        event=HookEvent.PRE_APPROVAL_REQUEST,
        session_id="s",
        surface="cli",
        command="rm -rf /",
    ))
    assert seen == [("cli", "rm -rf /")]


@pytest.mark.asyncio
async def test_post_approval_records_choice():
    engine = HookEngine()
    seen = []

    async def hook(ctx):
        seen.append((ctx.choice, ctx.surface))

    engine.register(HookEvent.POST_APPROVAL_RESPONSE, hook)
    await engine.fire_and_forget_sync(HookContext(  # or fire_blocking; both fine for observer
        event=HookEvent.POST_APPROVAL_RESPONSE,
        session_id="s", surface="gateway",
        command="rm -rf /", choice="deny",
    ))
    # Allow scheduler to process if fire_and_forget
    import asyncio; await asyncio.sleep(0.01)
    assert seen and seen[0] == ("deny", "gateway")
```

- [ ] **Step 14.3: Add fire calls to the approval gate**

In the function that emits the y/n prompt (located in step 14.1):

```python
# Before prompting:
await self.hook_engine.fire_blocking(HookContext(
    event=HookEvent.PRE_APPROVAL_REQUEST,
    session_id=self.session_id,
    surface=surface,  # "cli" or "gateway"
    command=command,
))

# ... existing prompt code ...
choice = ...  # one of "once" / "session" / "always" / "deny" / "timeout"

# After choice received:
self.hook_engine.fire_and_forget(HookContext(
    event=HookEvent.POST_APPROVAL_RESPONSE,
    session_id=self.session_id,
    surface=surface,
    command=command,
    choice=choice,
))
```

If OC has separate CLI vs gateway approval functions, fire from both, distinguishing via the `surface` kwarg.

- [ ] **Step 14.4: Run tests**

```bash
uv run pytest tests/agent/test_approval_hooks.py -v
```
Expected: 2 passed.

- [ ] **Step 14.5: Commit**

```bash
git add opencomputer/agent/trust_ramp.py opencomputer/agent/policy_audit.py tests/agent/test_approval_hooks.py
git commit -m "feat(plugins): pre_approval_request + post_approval_response hooks

Port hermes-agent 30307a980. Observer-only hooks (return values ignored)
fired around dangerous-command approvals on both CLI and gateway surfaces.
Plugin crashes swallowed by hook engine. surface kwarg distinguishes
'cli' from 'gateway'; choice on post hook is once|session|always|deny|timeout.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 15: `duration_ms` on `post_tool_call` + `transform_tool_result`

**Files:**
- Modify: `opencomputer/agent/loop.py` (measure dispatch latency)
- Test: `tests/agent/test_post_tool_call_duration.py`

**Hermes reference:** commit `59b56d445`.

- [ ] **Step 15.1: Write failing test**

```python
# tests/agent/test_post_tool_call_duration.py
import asyncio
import pytest
from plugin_sdk.hooks import HookEvent, HookContext, HookDecision
from opencomputer.hooks.engine import HookEngine


@pytest.mark.asyncio
async def test_post_tool_call_receives_duration_ms():
    """duration_ms is a non-negative int passed in HookContext."""
    engine = HookEngine()
    captured = {}

    async def hook(ctx):
        captured["ms"] = ctx.duration_ms
        return HookDecision(decision="pass")

    engine.register(HookEvent.POST_TOOL_USE, hook)
    # Simulate the loop: monotonic delta wrapping a fake tool dispatch
    import time
    start = time.monotonic()
    await asyncio.sleep(0.05)  # ~50 ms
    duration = int((time.monotonic() - start) * 1000)
    await engine.fire_blocking(HookContext(
        event=HookEvent.POST_TOOL_USE,
        session_id="s",
        duration_ms=duration,
    ))
    assert isinstance(captured["ms"], int)
    assert captured["ms"] >= 50
```

- [ ] **Step 15.2: Wire timing in loop dispatch**

In `agent/loop.py`, find the tool dispatch site:

```python
import time

t0 = time.monotonic()
try:
    result = await tool_registry.dispatch(tool_call)
finally:
    duration_ms = max(0, int((time.monotonic() - t0) * 1000))

# Pass duration_ms into both hook fires:
await self.hook_engine.fire_blocking(HookContext(
    event=HookEvent.POST_TOOL_USE,
    session_id=self.session_id,
    tool_call=tool_call,
    tool_result=result,
    duration_ms=duration_ms,
))
await self.hook_engine.fire_blocking(HookContext(
    event=HookEvent.TRANSFORM_TOOL_RESULT,
    session_id=self.session_id,
    tool_call=tool_call,
    tool_result=result,
    duration_ms=duration_ms,
))
```

- [ ] **Step 15.3: Run tests**

```bash
uv run pytest tests/agent/test_post_tool_call_duration.py -v
```
Expected: 1 passed.

- [ ] **Step 15.4: Commit**

```bash
git add opencomputer/agent/loop.py plugin_sdk/hooks.py tests/agent/test_post_tool_call_duration.py
git commit -m "feat(hooks): pass duration_ms to post_tool_call + transform_tool_result

Port hermes-agent 59b56d445 (also Claude Code 2.1.119). Plugin authors
can now build latency dashboards and per-tool SLO alerts without
manually wrapping every tool. Additive kwarg — old plugins ignore it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 16: Group C milestone

- [ ] **Step 16.1: Run Group C tests**

```bash
uv run pytest tests/plugin_sdk/test_pre_gateway_dispatch.py tests/agent/test_approval_hooks.py tests/agent/test_post_tool_call_duration.py -v
```
Expected: all pass.

- [ ] **Step 16.2: Ruff Group C**

```bash
uv run ruff check plugin_sdk/hooks.py opencomputer/gateway/server.py opencomputer/agent/trust_ramp.py opencomputer/agent/policy_audit.py opencomputer/agent/loop.py
```

---

## Group D — Storage + Skills (Tasks 17–18)

### Task 17: Lazy session creation (defer DB row until first message)

**Files:**
- Modify: `opencomputer/agent/session_db.py` — extract `_insert_session_row`, add `prune_empty_ghost_sessions`
- Modify: `opencomputer/agent/loop.py` — `_ensure_db_session()` gate
- Modify: `opencomputer/cli.py` — one-time prune on startup
- Test: `tests/agent/test_lazy_session.py`

**Hermes reference:** commit `c5b4c4816`.

**Behavior:** TUI/web open without sending → no `state.db` row written. First user message triggers the row. Existing data untouched. Migration prunes any empty ghost rows on startup.

- [ ] **Step 17.1: Write failing tests**

```python
# tests/agent/test_lazy_session.py
import pytest
from opencomputer.agent.session_db import SessionDB


def test_open_close_creates_no_row(tmp_path):
    db = SessionDB(path=str(tmp_path / "s.db"))
    sid = db.allocate_session_id()  # in-memory only
    # Don't call create_session_row / _insert_session_row
    rows = db.list_sessions()
    assert all(r.id != sid for r in rows)


def test_first_message_creates_row(tmp_path):
    db = SessionDB(path=str(tmp_path / "s.db"))
    sid = db.allocate_session_id()
    db.ensure_session(sid)  # idempotent — creates if absent
    rows = db.list_sessions()
    assert any(r.id == sid for r in rows)


def test_ensure_session_idempotent(tmp_path):
    db = SessionDB(path=str(tmp_path / "s.db"))
    sid = db.allocate_session_id()
    db.ensure_session(sid)
    db.ensure_session(sid)
    rows = [r for r in db.list_sessions() if r.id == sid]
    assert len(rows) == 1


def test_prune_removes_empty_rows(tmp_path):
    db = SessionDB(path=str(tmp_path / "s.db"))
    # Create two sessions, only one with messages
    sid1 = db.create_session()  # eager — message_count=0 ghost
    sid2 = db.create_session()
    db.append_message(sid2, role="user", content="hi")
    pruned = db.prune_empty_ghost_sessions()
    assert pruned == 1
    rows = db.list_sessions()
    assert any(r.id == sid2 for r in rows)
    assert all(r.id != sid1 for r in rows)
```

- [ ] **Step 17.2: Run failing**

```bash
uv run pytest tests/agent/test_lazy_session.py -v
```
Expected: AttributeError on `allocate_session_id` / `ensure_session` / `prune_empty_ghost_sessions`.

- [ ] **Step 17.3: Implement in `session_db.py`**

Refactor existing `create_session()`:

```python
def _insert_session_row(self, session_id: str, *, title: str | None = None) -> None:
    """Insert a row into sessions table (DRY helper)."""
    self._conn.execute(
        "INSERT OR IGNORE INTO sessions (id, created_at, title) VALUES (?, ?, ?)",
        (session_id, time.time(), title),
    )
    self._conn.commit()


def allocate_session_id(self) -> str:
    """Generate a session id without writing a DB row."""
    return str(uuid.uuid4())


def ensure_session(self, session_id: str, *, title: str | None = None) -> None:
    """Idempotent: insert the row if not already present."""
    self._insert_session_row(session_id, title=title)


def create_session(self, *, title: str | None = None) -> str:
    """Eager (legacy): allocate AND write row immediately."""
    sid = self.allocate_session_id()
    self._insert_session_row(sid, title=title)
    return sid


def prune_empty_ghost_sessions(self) -> int:
    """One-time migration: delete sessions with zero messages."""
    cur = self._conn.execute(
        """DELETE FROM sessions WHERE id IN (
               SELECT s.id FROM sessions s
               LEFT JOIN messages m ON m.session_id = s.id
               GROUP BY s.id HAVING COUNT(m.id) = 0
           )"""
    )
    self._conn.commit()
    return cur.rowcount
```

- [ ] **Step 17.4: Use lazy-create in agent loop**

In `opencomputer/agent/loop.py`:

```python
# In __init__: don't eagerly create session row
self.session_id = self.session_db.allocate_session_id() if not self.session_id else self.session_id

# Add gate method, called at run_conversation entry:
def _ensure_db_session(self) -> None:
    self.session_db.ensure_session(self.session_id)

# Call _ensure_db_session() at the start of run_conversation() and before any other DB writes.
```

In `cli.py`, near startup:

```python
from opencomputer.agent.session_db import SessionDB
db = SessionDB(...)
pruned = db.prune_empty_ghost_sessions()
if pruned:
    logger.info("Pruned %d empty ghost sessions on startup", pruned)
```

- [ ] **Step 17.5: Run tests**

```bash
uv run pytest tests/agent/test_lazy_session.py -v
```
Expected: 4 passed.

- [ ] **Step 17.6: Run regression — existing tests**

```bash
uv run pytest tests/agent/ -x --timeout=120
```
Look for any test that depends on a session row existing before a message is written. Adjust those tests to call `db.ensure_session(sid)` first.

- [ ] **Step 17.7: Commit**

```bash
git add opencomputer/agent/session_db.py opencomputer/agent/loop.py opencomputer/cli.py tests/agent/test_lazy_session.py
git commit -m "feat(state): lazy session creation — defer DB row until first message

Port hermes-agent c5b4c4816. allocate_session_id() doesn't write the
row; ensure_session() (called at first message) does. Eager
create_session() retained for tests/legacy. One-time
prune_empty_ghost_sessions() on startup cleans existing ghost rows.

Closes: TUI/web open-and-close ghost session clutter.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 18: Skill install from HTTP(S) URL

**Files:**
- Create: `opencomputer/skills_hub/sources/url.py`
- Modify: `opencomputer/skills_hub/router.py` (register UrlSource ahead of WellKnown)
- Test: `tests/skills_hub/test_url_source.py`

**Hermes reference:** commit `9c416e20a`.

- [ ] **Step 18.1: Write failing tests**

```python
# tests/skills_hub/test_url_source.py
import pytest
from opencomputer.skills_hub.sources.url import UrlSource


def test_claims_https_skill_md():
    s = UrlSource()
    assert s.claims("https://example.com/skill.md") is True
    assert s.claims("https://example.com/foo.md") is True  # any .md
    assert s.claims("http://example.com/skill.md") is True


def test_does_not_claim_well_known():
    s = UrlSource()
    assert s.claims("https://example.com/.well-known/skills/foo.md") is False


def test_does_not_claim_non_md():
    s = UrlSource()
    assert s.claims("https://example.com/SKILL.zip") is False


def test_does_not_claim_github_repo_url():
    s = UrlSource()
    assert s.claims("https://github.com/user/repo") is False  # not .md


@pytest.mark.asyncio
async def test_fetch_uses_frontmatter_name(monkeypatch):
    fake_md = "---\nname: my-skill\ndescription: x\n---\n# Body"
    async def fake_get(url):
        return fake_md

    monkeypatch.setattr("opencomputer.skills_hub.sources.url._http_get", fake_get)
    s = UrlSource()
    spec = await s.fetch("https://example.com/skill.md")
    assert spec.name == "my-skill"


@pytest.mark.asyncio
async def test_fetch_falls_back_to_url_slug(monkeypatch):
    async def fake_get(url):
        return "# No frontmatter just markdown"

    monkeypatch.setattr("opencomputer.skills_hub.sources.url._http_get", fake_get)
    s = UrlSource()
    spec = await s.fetch("https://example.com/foo-bar/skill.md")
    assert spec.name in ("skill", "foo-bar")  # slug fallback
```

- [ ] **Step 18.2: Run failing**

```bash
uv run pytest tests/skills_hub/test_url_source.py -v
```
Expected: ImportError.

- [ ] **Step 18.3: Implement `url.py`**

```python
# opencomputer/skills_hub/sources/url.py
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
import yaml

from opencomputer.skills_hub.sources import SkillSource, SkillSpec


WELL_KNOWN_PREFIX = "/.well-known/skills/"


class UrlSource(SkillSource):
    name = "url"
    trust_level = "community"

    def claims(self, identifier: str) -> bool:
        if not identifier.startswith(("http://", "https://")):
            return False
        parsed = urlparse(identifier)
        if WELL_KNOWN_PREFIX in parsed.path:
            return False
        return parsed.path.endswith(".md")

    async def fetch(self, identifier: str, *, name_override: str | None = None) -> SkillSpec:
        text = await _http_get(identifier)
        frontmatter, body = _split_frontmatter(text)
        name = (
            name_override
            or (frontmatter or {}).get("name")
            or _slug_from_url(identifier)
        )
        description = (frontmatter or {}).get("description", "")
        return SkillSpec(
            name=name,
            description=description,
            body=body,
            source_identifier=identifier,
            trust_level=self.trust_level,
        )


async def _http_get(url: str) -> str:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end < 0:
        return None, text
    fm = yaml.safe_load(text[3:end])
    body = text[end + 4:].lstrip("\n")
    return (fm if isinstance(fm, dict) else None), body


def _slug_from_url(url: str) -> str:
    last = url.rstrip("/").rsplit("/", 1)[-1]
    last = re.sub(r"\.md$", "", last)
    return last or "unnamed-skill"
```

- [ ] **Step 18.4: Register in router**

In `opencomputer/skills_hub/router.py` find where sources are registered (probably a `_SOURCES = [...]` list). Add `UrlSource` AHEAD of WellKnown so /.well-known/ URLs route correctly:

```python
from opencomputer.skills_hub.sources.well_known import WellKnownSkillSource
from opencomputer.skills_hub.sources.github import GithubSkillSource
from opencomputer.skills_hub.sources.url import UrlSource

_SOURCES = [
    WellKnownSkillSource(),     # claims /.well-known/skills/...
    UrlSource(),                # claims any other http(s)://...md
    GithubSkillSource(),        # claims github.com URLs
    # ... others ...
]
```

- [ ] **Step 18.5: Run tests**

```bash
uv run pytest tests/skills_hub/test_url_source.py -v
```
Expected: 6 passed.

- [ ] **Step 18.6: Commit**

```bash
git add opencomputer/skills_hub/sources/url.py opencomputer/skills_hub/router.py tests/skills_hub/test_url_source.py
git commit -m "feat(skills_hub): install skills from a direct HTTP(S) URL

Port hermes-agent 9c416e20a. UrlSource adapter — claims any http(s)://*.md
that isn't /.well-known/skills/. Frontmatter name preferred; URL-slug fallback.
Trust level 'community'; full security scan still runs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 19: Group D + final integration

- [ ] **Step 19.1: Group D tests**

```bash
uv run pytest tests/agent/test_lazy_session.py tests/skills_hub/test_url_source.py -v
```
Expected: pass.

- [ ] **Step 19.2: FULL pytest suite**

```bash
uv run pytest --timeout=300 2>&1 | tee /tmp/wave5-fullsuite.log | tail -30
```
Expected: ALL pass (including pre-existing 8,800+). If any fail, investigate — could be a regression from Group A/B/C/D changes.

- [ ] **Step 19.3: FULL ruff**

```bash
uv run ruff check . 2>&1 | tail -20
```
Fix all reported issues.

- [ ] **Step 19.4: Update memory MEMORY.md after merge**

Add a single line to `~/.claude/projects/-Users-saksham-Vscode-claude/memory/MEMORY.md` (post-PR-merge):

```markdown
- [Hermes Wave 5 Best-of-Import](project_hermes_wave5_done.md) — PR #<N>: /goal + tool guardrails + video_analyze + Piper TTS + 10 more (Tier 1, 14 features, ~5k LOC).
```

(Create `project_hermes_wave5_done.md` post-merge with the squash hash + commit summary.)

- [ ] **Step 19.5: Push and open PR**

```bash
git push -u origin feat/hermes-best-of-wave5
gh pr create --title "feat(hermes-wave5): /goal + tool guardrails + video_analyze + Piper + 10 more (Tier 1)" --body "$(cat <<'EOF'
## Summary

Wave 5 of the hermes-best-of-import series. Ports 14 high-value Hermes-agent features (post-PR #413) into OpenComputer in 4 grouped commits.

**Group A — Agent Core**
- /goal — persistent cross-turn goals (Ralph loop, hermes 265bd59c1)
- Tool-loop guardrails (warn + hard-stop, hermes 58b89965c)
- /steer + /queue ACP+CLI slash commands (hermes e27b0b765)
- busy_ack_enabled config + runtime-metadata footer + /footer (hermes 2b512cbca + e123f4ecf)
- OpenRouter response caching (hermes 457c7b76c) — distinct from PR #339 prompt-cache

**Group B — Multimodal / Voice**
- video_analyze tool (hermes c9a3f36f5)
- Piper TTS native provider (hermes 8d302e37a)
- TTS command-type provider registry (hermes 2facea7f7)
- Native send_multiple_images for 6 channels (hermes 3de8e2168 + 04ea895ff)

**Group C — Plugin Platform**
- pre_gateway_dispatch hook (hermes 1ef1e4c66)
- pre_approval_request + post_approval_response observer hooks (hermes 30307a980)
- duration_ms on post_tool_call + transform_tool_result (hermes 59b56d445)

**Group D — Storage + Skills**
- Lazy session creation (defer DB row, hermes c5b4c4816)
- Skill install from HTTP(S) URL (hermes 9c416e20a)

**Out of scope (deferred):** Tier 2 backlog in spec §8 (curator, kanban board, new skills batch, etc.).

## Test plan

- [ ] All 85+ new tests pass
- [ ] Full suite (8,800+) pass
- [ ] ruff clean
- [ ] Manual: /goal lifecycle (set, status, pause, resume, clear)
- [ ] Manual: /steer interrupt mid-tool-call
- [ ] Manual: /queue + drain after turn end
- [ ] Manual: /footer toggle visible in CLI
- [ ] Manual: TUI open + close → no ghost session row
- [ ] Manual: hermes URL skill install (end-to-end with a public test SKILL.md)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 19.6: Update task statuses**

After PR merge, mark `Task 19` complete and write the post-merge memory entry.

---

## Self-review checklist (run after writing this plan)

**1. Spec coverage** — every Tier 1 item from the spec maps to at least one task:
- A1 /goal → Task 2 ✓
- A2 tool guardrails → Task 1 ✓
- A3 /steer + /queue → Task 3 ✓
- A4 busy_ack + runtime footer → Task 4 ✓
- A5 OpenRouter response caching → Task 5 ✓
- B1 video_analyze → Task 7 ✓
- B2 Piper TTS → Task 8 ✓
- B3 TTS command registry → Task 9 ✓
- B4 send_multiple_images (base + 6 platforms) → Tasks 10 + 11 ✓
- C1 pre_gateway_dispatch → Task 13 ✓
- C2 approval hooks → Task 14 ✓
- C3 duration_ms → Task 15 ✓
- D1 lazy session creation → Task 17 ✓
- D2 URL skill source → Task 18 ✓

**2. Placeholder scan** — no "TODO", "TBD", "implement later" inside any task body. Open-Q references in §17.2 / §11.8 / §14.1 are tied to grep commands the executor runs, not deferrals.

**3. Type consistency** — `HookEvent`, `HookContext`, `HookDecision` shapes match `plugin_sdk/hooks.py` headers we read. `SessionDB.{allocate_session_id, ensure_session, create_session, prune_empty_ghost_sessions, set_state_meta, get_state_meta}` consistent across Tasks 2 and 17. `BaseChannelAdapter.send_multiple_images` signature consistent across Tasks 10 and 11.

**4. Group ordering** — Group A first (instruments loop), Group D last (adds session-row gate). Cross-group dependencies: Task 15 (`duration_ms`) depends on Task 13 (`HookContext.duration_ms` field) — Task 13 lands first within Group C.

---

## Execution

Per user directive: invoke `superpowers:executing-plans` next, with self-audit happening as part of /executing-plans entry.
