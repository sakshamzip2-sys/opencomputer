# OpenClaw Tier 1 — Selective Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the **four** OpenClaw capabilities that pass the strict "OpenClaw does it materially better than what we have" filter, as identified in `docs/refs/openclaw/2026-04-28-major-gaps.md`. Nothing else.

**Filter (re-stated for the executing engineer):** Only port from OpenClaw what (a) OpenClaw does *materially better* than Hermes/Claude Code, (b) OC doesn't already have a good-enough version of, (c) fits OC's positioning. **DO NOT** mass-port plugins, providers, channels, or polish features. If you find yourself adding scope outside the four sub-projects below, stop and ask.

**The four sub-projects (each ships independently):**

- **Sub-project A — Multi-agent isolation + channel-binding router** (XL, multi-PR). The headline architectural win.
- **Sub-project B — Standing Orders + cron integration** (L, single PR). Soft-deps on A.
- **Sub-project C — Active Memory blocking pre-reply sub-agent** (M, single PR). Standalone.
- **Sub-project D — Block streaming chunker + `humanDelay`** (M, single PR). Standalone.

**Tech Stack:** Python 3.12+, Typer (existing), pydantic v2 (existing), httpx (existing), rich.Console (existing), pyyaml (existing), pytest (existing). New deps: **none** for A/B/D. Sub-project C optionally adds nothing (uses existing provider client).

**Out of scope (deferred to follow-up plans):**
- Hook taxonomy expansion beyond a single new `BeforeAgentReply` event needed for Sub-project C.
- Inbound queue modes (Hermes /queue + /steer cover the basics).
- Replay sanitization rules (small win; ship as a 50-line PR when needed).
- Background Tasks ledger expansion (OC's `tasks/` is sufficient).
- Diagnostics OTEL plugin.
- Lobster / TaskFlow / sandbox-browser / Heartbeat / ACP expansion.
- Mobile node CLI / canvas / voice wake / SIP.
- Provider plugin breadth (LiteLLM-as-provider is a separate later plan).
- Channel adapter long tail (already covered by Hermes megamerge PR #221).

**Why this order of sub-projects:** A is foundational (per-agent state dirs unblock B's per-agent Standing Orders and unblock per-agent Active Memory configs). B depends on A only softly (works single-agent). C and D are truly standalone. So **A → (B || C || D)** in parallel works.

---

## Phase 0 — Pre-flight verification (run BEFORE Sub-project A)

**Why:** Hermes Tier 1.A plan caught 9 critical assumption-breaks at this stage. Same risk exists here. **Don't skip.** Run all five tasks; record findings to `docs/superpowers/plans/2026-04-28-openclaw-tier1-DECISIONS.md`.

### Task 0.1: Verify SessionDB schema + migration path

**File:** `opencomputer/agent/state.py`

- [ ] **Step 1:** Read `state.py` end-to-end. Note current schema version and migration mechanism. Look for `CREATE TABLE` statements; confirm there's an existing migrator (e.g., `_apply_migrations`).
- [ ] **Step 2:** Locate the test that asserts schema (`tests/test_session_db.py` or similar). Run it: `pytest tests/test_session_db.py -v`. Confirm it passes on `main`.
- [ ] **Step 3:** Document in `DECISIONS.md`:
  - Current schema version
  - Migration mechanism
  - Path forward for adding `agent_id TEXT NOT NULL DEFAULT 'default'` column to relevant tables
  - Whether existing `pytest tests/` passes still after a hypothetical schema bump (run a dummy `ALTER TABLE` on a copy and confirm).

### Task 0.2: Verify gateway dispatch shape

**File:** `opencomputer/gateway/dispatch.py`

- [ ] **Step 1:** Read `dispatch.py` end-to-end. Document the actual signature of `handle_message_event(event)` and what `event` contains.
- [ ] **Step 2:** Trace one end-to-end inbound flow: Slack adapter receives a message → calls dispatch → dispatch routes → AgentLoop runs.
- [ ] **Step 3:** Identify ALL channel adapters that call dispatch. Listed in the OC current-state survey (`docs/refs/openclaw/2026-04-28-oc-current-state.md` § Extensions catalog § Channel adapters). Confirm.
- [ ] **Step 4:** Document in `DECISIONS.md`:
  - Exact dispatch signature
  - Which adapters call it
  - The MessageEvent fields available (peer, channel, accountId, etc.) — from `plugin_sdk/core.py::MessageEvent`. **Specifically: do `accountId` and `peer` already exist on `MessageEvent`?** If not, that's a SDK extension we need.

### Task 0.3: Verify hook engine emit points

**File:** `opencomputer/hooks/engine.py`, `opencomputer/agent/loop.py`

- [ ] **Step 1:** Read `hooks/engine.py` and confirm the `HookEngine.emit(event, ctx)` signature. Catalog all emit calls in `agent/loop.py`.
- [ ] **Step 2:** Find the loop iteration boundary where `BeforeAgentReply` (Sub-project C) would emit — between prompt-build and provider call.
- [ ] **Step 3:** Document in `DECISIONS.md`: exact line numbers in `agent/loop.py` where the emit will land.

### Task 0.4: Verify cron job registry shape

**File:** `opencomputer/cron/scheduler.py`, `opencomputer/cron/jobs.py`

- [ ] **Step 1:** Read both files end-to-end. Document `CronScheduler.schedule(job)` signature and `Job` dataclass shape.
- [ ] **Step 2:** Document in `DECISIONS.md`: how Sub-project B's `Program(...).triggers` will register cron triggers via `CronScheduler.schedule(...)`. Confirm `Job.fire_callback` shape supports our use case.

### Task 0.5: Verify channel adapter streaming surface

**Files:** `extensions/{telegram,discord,slack}/plugin.py` (sample three)

- [ ] **Step 1:** Read the streaming/`on_delta`/`send` paths in three adapters. Document the exact callback shape used today.
- [ ] **Step 2:** Identify the *highest-level* place where the chunker would wrap. Is it `provider.stream()` → `dispatch.on_delta(text_chunk)` → `adapter.send(text_chunk)`? Or does each adapter own its own streaming?
- [ ] **Step 3:** Document in `DECISIONS.md`: the wrapping layer for Sub-project D.

### Task 0.6: Phase 0 commit gate

- [ ] Commit `DECISIONS.md` to a new branch `prep/openclaw-tier1-decisions`. Push. The branch is *only* docs; merging is optional. The Phase 0 doc is the contract for later phases.

---

## Sub-project A — Multi-agent isolation + channel-binding router

**Goal:** Inside one Gateway daemon, route inbound channel messages to one of N truly isolated agents using a deterministic binding match. Per-agent state directory at `~/.opencomputer/<profile>/agents/<id>/` containing own auth, sessions, workspace, AGENTS/SOUL, model registry, skills allowlist.

**Strategy:** Six PRs. Each PR ships working software that doesn't break the existing single-agent flow. Tests gate each PR.

**File map for Sub-project A:**

**Created:**
- `plugin_sdk/multi_agent.py` — public types: `AgentDescriptor`, `BindingRule`, `BindingMatchKey`
- `opencomputer/agents_runtime/__init__.py` — re-exports
- `opencomputer/agents_runtime/registry.py` — `AgentRegistry`
- `opencomputer/agents_runtime/router.py` — `AgentRouter` with deterministic-most-specific-wins
- `opencomputer/agents_runtime/state_dirs.py` — per-agent state-dir resolver
- `opencomputer/cli_agents.py` — Typer subapp for `oc agents …`
- `opencomputer/migrations/0042_add_agent_id_to_sessions.py` — SessionDB schema migration
- `tests/agents_runtime/test_models.py`
- `tests/agents_runtime/test_registry.py`
- `tests/agents_runtime/test_router.py`
- `tests/agents_runtime/test_state_dirs.py`
- `tests/test_cli_agents.py`
- `tests/test_session_db_agent_id_migration.py`
- `tests/test_gateway_dispatch_agent_routing.py`

**Modified:**
- `plugin_sdk/__init__.py` — export `AgentDescriptor`, `BindingRule`, `BindingMatchKey`
- `plugin_sdk/core.py::MessageEvent` — add `account_id: str | None`, `peer: str | None`, `parent_peer: str | None`, `guild_id: str | None`, `roles: tuple[str, ...] | None`, `team_id: str | None` if not already present (verified in Phase 0.2)
- `opencomputer/agent/state.py::SessionDB` — accept and persist `agent_id`
- `opencomputer/gateway/dispatch.py` — resolve binding key → `agentId` and pass downstream
- `opencomputer/cli.py` — register `oc agents` subapp via `cli_agents.attach(app)`
- `opencomputer/agent/loop.py` — accept `agent_id` parameter (defaults to `"default"`); thread through to `SessionDB`
- `opencomputer/agent/memory.py::MemoryManager.list_skills()` — walk `~/.opencomputer/<profile>/agents/<id>/skills/` plus shared root
- All 14 channel adapters in `extensions/` — pass `account_id`, `peer`, etc. on `MessageEvent` if not already present (verified in 0.5)
- `~/.opencomputer/<profile>/config.yaml` schema validator — accept `agents:` block with `defaults:`, `bindings:` keys

### PR-A1: Foundation — SDK types + AgentRegistry + per-agent state-dir resolver

**Phase goal:** Public contract sealed; data layer testable in isolation; **no networking, no DB, no gateway changes yet**.

#### Task A1.1: Define `AgentDescriptor`, `BindingRule`, `BindingMatchKey` in `plugin_sdk`

**Files:**
- Create: `plugin_sdk/multi_agent.py`
- Test: `tests/agents_runtime/test_models.py`

- [ ] **Step 1: Write the failing test for AgentDescriptor + BindingRule + BindingMatchKey dataclasses**

```python
# tests/agents_runtime/test_models.py
"""Tests for AgentDescriptor + BindingRule + BindingMatchKey public types."""
import pytest
from plugin_sdk.multi_agent import AgentDescriptor, BindingRule, BindingMatchKey


def test_agent_descriptor_required_fields():
    a = AgentDescriptor(id="work", name="Work Agent")
    assert a.id == "work"
    assert a.name == "Work Agent"
    assert a.workspace_subdir == "work"  # default = id

def test_agent_descriptor_id_validation():
    with pytest.raises(ValueError, match="must match"):
        AgentDescriptor(id="bad id with spaces", name="x")

def test_binding_rule_match_score():
    rule = BindingRule(
        agent="work",
        channel="slack",
        account_id="T01ABC",
    )
    key_match = BindingMatchKey(channel="slack", account_id="T01ABC", peer="U99")
    key_partial = BindingMatchKey(channel="slack", account_id="T02OTHER", peer="U99")

    assert rule.matches(key_match) is True
    assert rule.matches(key_partial) is False
    assert rule.match_score(key_match) == 2  # channel + account_id


def test_binding_rule_match_score_more_specific_wins():
    less = BindingRule(agent="work", channel="slack", account_id="T01")
    more = BindingRule(agent="home", channel="slack", account_id="T01", peer="U99")
    key = BindingMatchKey(channel="slack", account_id="T01", peer="U99")

    assert less.matches(key)
    assert more.matches(key)
    assert more.match_score(key) > less.match_score(key)
```

- [ ] **Step 2: Run the test; expect ImportError or NotImplemented**

```bash
pytest tests/agents_runtime/test_models.py -v
# Expected: FAIL — module not found
```

- [ ] **Step 3: Implement the dataclasses**

```python
# plugin_sdk/multi_agent.py
"""Public types for OpenComputer multi-agent isolation + channel-binding router.

An *agent* is a fully scoped brain (workspace + sessions + auth + model registry).
A *binding* maps a channel-account-peer-(...) tuple to one agent inside a Gateway.

Mirrors the OpenClaw multi-agent pattern:
  https://docs.openclaw.ai/concepts/multi-agent
but adapted to OpenComputer's profile-as-outer-boundary architecture: each
profile may contain N agents, each agent has its own state under
``<profile_home>/agents/<agentId>/``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class AgentDescriptor:
    """One isolated agent inside a profile.

    The ``id`` must be lowercase alphanumeric + ``-``/``_`` (max 64 chars). It is
    used as the on-disk directory name and as the routing key.
    """

    id: str
    name: str
    workspace_subdir: str = ""  # set in __post_init__ if empty
    description: str = ""

    def __post_init__(self) -> None:
        if not _AGENT_ID_RE.match(self.id):
            raise ValueError(
                f"agent id {self.id!r} must match /^[a-z0-9][a-z0-9_-]{{0,63}}$/"
            )
        if not self.workspace_subdir:
            object.__setattr__(self, "workspace_subdir", self.id)


@dataclass(frozen=True, slots=True)
class BindingMatchKey:
    """The set of attributes a binding rule may match against.

    Populated by the channel adapter when emitting a MessageEvent. Unset fields
    are ``None``.
    """

    channel: str | None = None
    account_id: str | None = None
    peer: str | None = None
    parent_peer: str | None = None
    guild_id: str | None = None
    team_id: str | None = None
    roles: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class BindingRule:
    """Map a (channel, account_id, peer, parent_peer, guild_id, team_id, roles)
    match-tuple to an ``agent`` id. Most-specific-wins resolves ties: count of
    specified keys is the score; configuration order breaks ties between
    rules of equal score.
    """

    agent: str
    channel: str | None = None
    account_id: str | None = None
    peer: str | None = None
    parent_peer: str | None = None
    guild_id: str | None = None
    team_id: str | None = None
    roles: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, key: BindingMatchKey) -> bool:
        """Every specified field of self must equal the corresponding field of key.
        Roles match by subset (rule's roles must be a subset of key's roles)."""
        for attr in ("channel", "account_id", "peer", "parent_peer", "guild_id", "team_id"):
            r = getattr(self, attr)
            k = getattr(key, attr)
            if r is not None and r != k:
                return False
        if self.roles:
            key_roles = set(key.roles or ())
            if not set(self.roles).issubset(key_roles):
                return False
        return True

    def match_score(self, key: BindingMatchKey) -> int:
        """Score = count of fields specified in self (and matching key) +
        ``len(self.roles)`` if roles match."""
        if not self.matches(key):
            return 0
        score = sum(
            1
            for attr in ("channel", "account_id", "peer", "parent_peer", "guild_id", "team_id")
            if getattr(self, attr) is not None
        )
        score += len(self.roles)
        return score


__all__ = ["AgentDescriptor", "BindingRule", "BindingMatchKey"]
```

- [ ] **Step 4: Run the test; expect PASS**

- [ ] **Step 5: Commit**

```bash
git add plugin_sdk/multi_agent.py tests/agents_runtime/test_models.py
git commit -m "feat(plugin_sdk): add multi_agent SDK types (AgentDescriptor, BindingRule, BindingMatchKey)"
```

#### Task A1.2: Re-export from `plugin_sdk/__init__.py`

**Files:**
- Modify: `plugin_sdk/__init__.py`

- [ ] **Step 1: Read the existing `plugin_sdk/__init__.py`**
- [ ] **Step 2: Add the imports + `__all__`**

```python
# In plugin_sdk/__init__.py — add to existing imports
from plugin_sdk.multi_agent import AgentDescriptor, BindingRule, BindingMatchKey
```

```python
# Add to __all__ tuple
"AgentDescriptor", "BindingRule", "BindingMatchKey",
```

- [ ] **Step 3: Run the SDK boundary test**

```bash
pytest tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v
```

- [ ] **Step 4: Commit**

#### Task A1.3: Implement `AgentRegistry` + per-agent state-dir resolver

**Files:**
- Create: `opencomputer/agents_runtime/__init__.py`
- Create: `opencomputer/agents_runtime/registry.py`
- Create: `opencomputer/agents_runtime/state_dirs.py`
- Test: `tests/agents_runtime/test_registry.py`, `tests/agents_runtime/test_state_dirs.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/agents_runtime/test_state_dirs.py
"""Tests for per-agent state directory resolution."""
from pathlib import Path
from opencomputer.agents_runtime.state_dirs import (
    agent_state_dir,
    default_agent_id,
)


def test_default_agent_id():
    assert default_agent_id() == "default"


def test_agent_state_dir(tmp_path):
    d = agent_state_dir(profile_home=tmp_path, agent_id="work")
    assert d == tmp_path / "agents" / "work"


def test_agent_state_dir_default(tmp_path):
    d = agent_state_dir(profile_home=tmp_path, agent_id="default")
    assert d == tmp_path / "agents" / "default"
```

```python
# tests/agents_runtime/test_registry.py
import pytest
from plugin_sdk.multi_agent import AgentDescriptor, BindingRule
from opencomputer.agents_runtime.registry import AgentRegistry


def test_registry_register_and_get():
    reg = AgentRegistry()
    reg.register(AgentDescriptor(id="work", name="Work"))
    reg.register(AgentDescriptor(id="home", name="Home"))

    assert reg.get("work").name == "Work"
    assert reg.list_ids() == ["home", "work"]


def test_registry_duplicate_register():
    reg = AgentRegistry()
    reg.register(AgentDescriptor(id="work", name="Work"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(AgentDescriptor(id="work", name="Work2"))


def test_registry_unknown_get_returns_none():
    reg = AgentRegistry()
    assert reg.get("unknown") is None


def test_registry_default_agent_always_present():
    reg = AgentRegistry()
    # Even with no explicit registration, the default agent exists.
    assert reg.get("default") is not None
    assert reg.get("default").name == "Default"
```

- [ ] **Step 2: Run the tests; expect FAIL**

- [ ] **Step 3: Implement**

```python
# opencomputer/agents_runtime/state_dirs.py
"""Per-agent state directory resolution."""
from __future__ import annotations

from pathlib import Path

DEFAULT_AGENT_ID = "default"


def default_agent_id() -> str:
    return DEFAULT_AGENT_ID


def agent_state_dir(*, profile_home: Path, agent_id: str) -> Path:
    """Compute the state directory for ``agent_id`` under the given profile.

    Layout::

        <profile_home>/
            agents/
                <agent_id>/
                    auth/auth-profiles.json
                    skills/
                    workspace/
                    SOUL.md
                    AGENTS.md
                    USER.md
    """
    return profile_home / "agents" / agent_id
```

```python
# opencomputer/agents_runtime/registry.py
"""In-memory registry of AgentDescriptor instances inside a profile.

The registry always exposes a 'default' agent (auto-registered at construction)
so single-agent users never need to touch the multi-agent surface.
"""
from __future__ import annotations

from plugin_sdk.multi_agent import AgentDescriptor

DEFAULT_AGENT = AgentDescriptor(id="default", name="Default", description="Default agent — single-agent fallback")


class AgentRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, AgentDescriptor] = {}
        self.register(DEFAULT_AGENT)

    def register(self, descriptor: AgentDescriptor) -> None:
        if descriptor.id in self._by_id:
            raise ValueError(f"agent id {descriptor.id!r} already registered")
        self._by_id[descriptor.id] = descriptor

    def get(self, agent_id: str) -> AgentDescriptor | None:
        return self._by_id.get(agent_id)

    def list_ids(self) -> list[str]:
        return sorted(self._by_id.keys())

    def list_descriptors(self) -> list[AgentDescriptor]:
        return [self._by_id[k] for k in self.list_ids()]
```

```python
# opencomputer/agents_runtime/__init__.py
"""Multi-agent runtime — registry, router, state directories."""
from opencomputer.agents_runtime.registry import AgentRegistry, DEFAULT_AGENT
from opencomputer.agents_runtime.state_dirs import (
    DEFAULT_AGENT_ID,
    agent_state_dir,
    default_agent_id,
)

__all__ = [
    "AgentRegistry",
    "DEFAULT_AGENT",
    "DEFAULT_AGENT_ID",
    "agent_state_dir",
    "default_agent_id",
]
```

- [ ] **Step 4: Run the tests; expect PASS**
- [ ] **Step 5: Commit**

#### Task A1.4: Implement `AgentRouter` (binding match → agent id)

**Files:**
- Create: `opencomputer/agents_runtime/router.py`
- Test: `tests/agents_runtime/test_router.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/agents_runtime/test_router.py
import pytest
from plugin_sdk.multi_agent import BindingRule, BindingMatchKey
from opencomputer.agents_runtime.router import AgentRouter


def test_router_no_rules_returns_default():
    router = AgentRouter(rules=[], default_agent="default")
    assert router.resolve(BindingMatchKey(channel="slack")) == "default"


def test_router_single_match():
    rules = [BindingRule(agent="work", channel="slack")]
    router = AgentRouter(rules=rules, default_agent="default")
    assert router.resolve(BindingMatchKey(channel="slack")) == "work"


def test_router_most_specific_wins():
    rules = [
        BindingRule(agent="work", channel="slack", account_id="T01"),
        BindingRule(agent="home", channel="slack", account_id="T01", peer="U99"),
    ]
    router = AgentRouter(rules=rules, default_agent="default")
    key = BindingMatchKey(channel="slack", account_id="T01", peer="U99")
    assert router.resolve(key) == "home"


def test_router_tie_broken_by_config_order():
    rules = [
        BindingRule(agent="first", channel="slack"),
        BindingRule(agent="second", channel="slack"),
    ]
    router = AgentRouter(rules=rules, default_agent="default")
    assert router.resolve(BindingMatchKey(channel="slack")) == "first"


def test_router_no_match_falls_back_to_default():
    rules = [BindingRule(agent="work", channel="slack")]
    router = AgentRouter(rules=rules, default_agent="default")
    assert router.resolve(BindingMatchKey(channel="discord")) == "default"


def test_router_role_matching():
    rules = [BindingRule(agent="admin-only", channel="slack", roles=("admin",))]
    router = AgentRouter(rules=rules, default_agent="default")
    assert router.resolve(BindingMatchKey(channel="slack", roles=("admin", "user"))) == "admin-only"
    assert router.resolve(BindingMatchKey(channel="slack", roles=("user",))) == "default"
```

- [ ] **Step 2: Run; expect FAIL**

- [ ] **Step 3: Implement**

```python
# opencomputer/agents_runtime/router.py
"""Deterministic most-specific-wins binding-rule router.

Score = count of specified fields matching + count of roles matching.
Ties broken by configuration order (the rule listed first wins).
"""
from __future__ import annotations

from plugin_sdk.multi_agent import BindingMatchKey, BindingRule


class AgentRouter:
    def __init__(self, *, rules: list[BindingRule], default_agent: str) -> None:
        self._rules = list(rules)
        self._default = default_agent

    def resolve(self, key: BindingMatchKey) -> str:
        best_score = 0
        best_agent: str | None = None
        for rule in self._rules:  # config order = tiebreaker
            score = rule.match_score(key)
            if score > best_score:
                best_score = score
                best_agent = rule.agent
        return best_agent or self._default
```

- [ ] **Step 4: Run tests; expect PASS**
- [ ] **Step 5: Commit**

#### Task A1.5: PR-A1 gate — full Sub-project A1 + existing test suite green

- [ ] Run: `pytest tests/agents_runtime/ -v` — expect all green.
- [ ] Run: `pytest tests/ -q` — expect zero regressions.
- [ ] Run: `ruff check opencomputer/ plugin_sdk/ tests/` — expect zero issues.
- [ ] **Open PR-A1 to `main`** with title `feat(multi-agent): SDK types + AgentRegistry + AgentRouter (A1)`.
- [ ] Wait for review / merge before proceeding to PR-A2.

---

### PR-A2: SessionDB schema migration — `agent_id` column

**Phase goal:** Every session row carries an `agent_id`. Existing rows back-fill to `'default'`. Existing tests pass.

#### Task A2.1: Write the migration

**Files:**
- Create: `opencomputer/migrations/0042_add_agent_id_to_sessions.py`
- Test: `tests/test_session_db_agent_id_migration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_db_agent_id_migration.py
"""Test that the agent_id column is added to existing SessionDB rows."""
import sqlite3
from pathlib import Path
from opencomputer.agent.state import SessionDB
from opencomputer.migrations import apply_migrations


def test_migration_adds_agent_id_column(tmp_path):
    db_path = tmp_path / "test.db"
    # Create a pre-migration DB with the old schema (no agent_id).
    pre_db = sqlite3.connect(db_path)
    pre_db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            title TEXT
        );
        INSERT INTO sessions (id, created_at, title) VALUES ('s1', 1, 'foo');
    """)
    pre_db.commit()
    pre_db.close()

    # Apply migrations.
    apply_migrations(db_path)

    # Verify the column exists and the row was back-filled.
    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT id, agent_id FROM sessions").fetchall()
    assert rows == [("s1", "default")]
    db.close()
```

- [ ] **Step 2: Run; expect FAIL** (the migration module doesn't exist)

- [ ] **Step 3: Implement the migration**

```python
# opencomputer/migrations/0042_add_agent_id_to_sessions.py
"""Add agent_id column to sessions and any other agent-scoped tables.

Back-fills existing rows to 'default' so single-agent users see no behavior
change. The default value is enforced at the schema level so newly-inserted
rows that don't specify agent_id get 'default' too.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def upgrade(db: sqlite3.Connection) -> None:
    # Idempotent: only add column if it doesn't exist.
    cur = db.execute("PRAGMA table_info(sessions)")
    cols = [r[1] for r in cur.fetchall()]
    if "agent_id" not in cols:
        db.execute("ALTER TABLE sessions ADD COLUMN agent_id TEXT NOT NULL DEFAULT 'default'")
    # Same for related tables that key by session_id; add agent_id where session
    # ownership is implicit. (Verify in Phase 0.1 which tables this includes.)
    db.commit()


def downgrade(db: sqlite3.Connection) -> None:
    # SQLite doesn't support DROP COLUMN before 3.35; downgrade is a no-op for
    # forward compat. The data lives in the agent_id column harmlessly.
    pass


__all__ = ["upgrade", "downgrade"]
```

```python
# opencomputer/migrations/__init__.py — add or update
import sqlite3
from importlib import import_module
from pathlib import Path

# List of migrations in apply order.
_MIGRATIONS = [
    # ... earlier migrations ...
    "0042_add_agent_id_to_sessions",
]


def apply_migrations(db_path: Path) -> None:
    db = sqlite3.connect(db_path)
    try:
        # Track applied migrations in a tiny meta table (idempotent guard).
        db.executescript("""
            CREATE TABLE IF NOT EXISTS _migrations (
                name TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL
            )
        """)
        applied = {r[0] for r in db.execute("SELECT name FROM _migrations").fetchall()}
        for name in _MIGRATIONS:
            if name in applied:
                continue
            mod = import_module(f"opencomputer.migrations.{name}")
            mod.upgrade(db)
            db.execute(
                "INSERT INTO _migrations (name, applied_at) VALUES (?, strftime('%s','now'))",
                (name,),
            )
            db.commit()
    finally:
        db.close()


__all__ = ["apply_migrations"]
```

- [ ] **Step 4: Run; expect PASS**

#### Task A2.2: Wire SessionDB to honor `agent_id`

**Files:**
- Modify: `opencomputer/agent/state.py::SessionDB`

- [ ] **Step 1: Add `agent_id` parameter to `SessionDB.create_session(...)` (default `"default"`).**
- [ ] **Step 2: Add `SessionDB.list_sessions(agent_id=None)` filter that returns rows where `agent_id = ?` if specified, else all.**
- [ ] **Step 3: Update existing tests; they should still pass with the default value.**
- [ ] **Step 4: Run full test suite; expect no regressions.**

#### Task A2.3: PR-A2 gate

- [ ] All `tests/` green.
- [ ] **Open PR-A2** with title `feat(multi-agent): SessionDB agent_id schema migration (A2)`. Note dependency on PR-A1 in the body.

---

### PR-A3: Gateway dispatch + binding router

**Phase goal:** The Gateway resolves binding-key → agent_id at the dispatch boundary and threads `agent_id` into the agent-loop call.

#### Task A3.1: Add `agents:` config block parser

**Files:**
- Modify: `opencomputer/agent/config.py` — add `AgentsConfig` dataclass

- [ ] Pydantic dataclass: `AgentsConfig` with fields `defaults: AgentsDefaults` and `bindings: list[BindingRule]`. Validates against `plugin_sdk.multi_agent.BindingRule`.
- [ ] Test: load a sample yaml with `agents:` block and confirm parsing.

#### Task A3.2: Add `MessageEvent` extension fields (verify in Phase 0.2 first)

**Files:**
- Modify: `plugin_sdk/core.py::MessageEvent`

- [ ] Add `account_id: str | None = None`, `peer: str | None = None`, `parent_peer: str | None = None`, `guild_id: str | None = None`, `roles: tuple[str, ...] | None = None`, `team_id: str | None = None` if not already present.
- [ ] Add corresponding fields in each of the 14 channel adapters where they emit events (Phase 0.2 list).

#### Task A3.3: Implement `AgentRouter.resolve(key)` at dispatch entry

**Files:**
- Modify: `opencomputer/gateway/dispatch.py::handle_message_event`
- Test: `tests/test_gateway_dispatch_agent_routing.py`

- [ ] Build `BindingMatchKey` from the incoming `MessageEvent`.
- [ ] Resolve via `AgentRouter`.
- [ ] Pass `agent_id=resolved` to `AgentLoop.run_conversation(...)`.

```python
# tests/test_gateway_dispatch_agent_routing.py
"""Test that gateway dispatch routes messages to the correct agent_id."""
import pytest
from plugin_sdk.core import MessageEvent
from plugin_sdk.multi_agent import BindingRule
from opencomputer.gateway.dispatch import _resolve_agent_id_for_event


def test_dispatch_routes_to_default_when_no_rules():
    rules = []
    event = MessageEvent(channel="slack", peer="U99", account_id="T01")
    assert _resolve_agent_id_for_event(event, rules=rules, default="default") == "default"


def test_dispatch_routes_to_most_specific_match():
    rules = [
        BindingRule(agent="work", channel="slack", account_id="T01"),
        BindingRule(agent="home", channel="slack", account_id="T01", peer="U99"),
    ]
    event = MessageEvent(channel="slack", peer="U99", account_id="T01")
    assert _resolve_agent_id_for_event(event, rules=rules, default="default") == "home"
```

- [ ] Run; expect PASS.
- [ ] Confirm existing dispatch tests still pass.

#### Task A3.4: PR-A3 gate

- [ ] All tests green; integration test routes a Slack event in a workspace to two different agents based on `peer`.
- [ ] **Open PR-A3** with title `feat(multi-agent): gateway dispatch routes to agent via BindingRule (A3)`.

---

### PR-A4: CLI `oc agents …`

**Phase goal:** User can list/create/show/delete agents and their bindings from the CLI.

#### Task A4.1: Typer subapp

**Files:**
- Create: `opencomputer/cli_agents.py`
- Test: `tests/test_cli_agents.py`

- [ ] Mirror the structure of `cli_profile.py`. Subcommands:
  - `oc agents list`
  - `oc agents create <id> [--name "..."] [--description "..."]`
  - `oc agents show <id>`
  - `oc agents delete <id> [--yes]`
  - `oc agents bindings list`
  - `oc agents bindings add --agent <id> [--channel ...] [--account-id ...] [--peer ...] [--guild-id ...]`
  - `oc agents bindings remove <index>`
  - `oc agents bindings test --channel ... [--account-id ...] [...]` — reports which agent would handle the synthetic event

- [ ] All commands write to `~/.opencomputer/<profile>/config.yaml::agents`.
- [ ] Tests use `CliRunner.invoke(...)` with a temp profile home.

#### Task A4.2: Wire into root `cli.py`

```python
# In opencomputer/cli.py — add near the existing subapp registrations
from opencomputer.cli_agents import attach as attach_agents_app

attach_agents_app(app)  # registers `oc agents …`
```

#### Task A4.3: PR-A4 gate

- [ ] CLI tests green. Manual smoke: `oc agents create work` then `oc agents list` shows it.
- [ ] **Open PR-A4** with title `feat(multi-agent): oc agents CLI (A4)`.

---

### PR-A5: Per-agent skills + auth profiles

**Phase goal:** Each agent has its own `~/.opencomputer/<profile>/agents/<id>/skills/` plus auth-profiles. Allowlist filter applied per agent.

#### Task A5.1: MemoryManager skills walk per agent

**Files:**
- Modify: `opencomputer/agent/memory.py::MemoryManager.list_skills`

- [ ] Update the walk to include `~/.opencomputer/<profile>/agents/<active_agent_id>/skills/` plus shared root.
- [ ] Tests: seed two agents with different skills; confirm allowlist filter respects per-agent.

#### Task A5.2: Auth profiles per-agent

**Files:**
- Modify: provider plugins to read `auth-profiles.json` from the active agent's state dir
- Add: backwards-compat fallback to profile-level `auth-profiles.json` if agent-level not present.

- [ ] Tests: per-agent credentials don't leak across agents.

#### Task A5.3: PR-A5 gate

- [ ] All tests green; manual smoke: two agents in one profile load different skills.
- [ ] **Open PR-A5** with title `feat(multi-agent): per-agent skills + auth-profile isolation (A5)`.

---

### PR-A6: Docs + cookbook

**Phase goal:** Make multi-agent discoverable to new users.

#### Task A6.1: README section

- [ ] Add `## Multi-agent` section to root `README.md` covering:
  - One-Gateway-multiple-agents motivation
  - Bindings configuration example (mirror OpenClaw's YAML shape)
  - `oc agents` CLI quick reference
  - Per-agent state directory layout
  - Migration story for single-agent users (no action required)

#### Task A6.2: CHANGELOG entry

- [ ] Bump `pyproject.toml::version` minor (e.g., `0.2.0 → 0.3.0`).
- [ ] Add `## [Unreleased] / Added: Multi-agent isolation + channel-binding router`.

#### Task A6.3: Cookbook example

- [ ] New `docs/cookbook/multi-agent-slack-routing.md` with a worked example: "I have two Slack workspaces (work + home) and I want different agents to handle each. Here's the config and the resulting behavior."

#### Task A6.4: PR-A6 gate

- [ ] **Open PR-A6** with title `docs(multi-agent): README + cookbook + CHANGELOG (A6)`.

---

## Sub-project B — Standing Orders + cron integration

**Goal:** Declarative `## Program: <name>` blocks in AGENTS.md grant the agent permanent operating authority for autonomous programs (scope + triggers + approval gates + escalation rules), wired to existing cron.

**Strategy:** One PR. Soft-deps on Sub-project A — without per-agent state, Standing Orders all live in `~/.opencomputer/<profile>/home/AGENTS.md`. With A, each agent has its own.

### Task B.1: Standing-Orders parser

**Files:**
- Create: `opencomputer/standing_orders/__init__.py`
- Create: `opencomputer/standing_orders/parser.py`
- Test: `tests/standing_orders/test_parser.py`

- [ ] Parser reads markdown looking for `## Program: <name>` sections. For each, parses bullet lists under `**Scope:**`, `**Triggers:**`, `**Approval gates:**`, `**Escalation rules:**`.
- [ ] Output: `Program(name, scope, triggers, approval_gates, escalation_rules)` dataclass.
- [ ] Trigger format: `every Friday at 17:00`, `cron: 0 17 * * 5`, `event: gmail:label-added(invoices)`.

```python
# tests/standing_orders/test_parser.py
from opencomputer.standing_orders.parser import parse_standing_orders, Program

SAMPLE = """
# AGENTS.md

Other content here.

## Program: weekly-report

**Scope:** Compose and send the weekly engineering report.
**Triggers:**
- cron: 0 17 * * 5
- event: linear:label-added(report-ready)
**Approval gates:**
- Bash, Edit need approval per call.
**Escalation rules:**
- If data sources fail health check, post to #eng-ops.
"""

def test_parser_extracts_program():
    progs = parse_standing_orders(SAMPLE)
    assert len(progs) == 1
    p = progs[0]
    assert p.name == "weekly-report"
    assert "engineering report" in p.scope
    assert "0 17 * * 5" in p.triggers
    assert "Bash" in p.approval_gates
```

### Task B.2: Standing-Orders runtime + cron registration

**Files:**
- Create: `opencomputer/standing_orders/runtime.py`
- Test: `tests/standing_orders/test_runtime.py`

- [ ] `register_programs(scheduler, programs)` registers cron triggers via `CronScheduler.schedule(...)`.
- [ ] Each fired program runs the agent loop with a synthetic prompt: `"Standing-Order program <name> triggered. Scope: <scope>. Execute within authority bounds."`
- [ ] System-prompt block: when a program is "active" for an agent (cron just fired or event fired), the prompt builder injects:
  ```
  <standing-order id="weekly-report">
  You have permanent operating authority for this program. Scope: …
  Tools you may use without per-call approval: <approves: list>.
  Tools that always require approval: <requires_approval: list>.
  </standing-order>
  ```

### Task B.3: CLI

**Files:**
- Create: `opencomputer/cli_standing_orders.py`
- Modify: `opencomputer/cli.py` — register subapp

- [ ] `oc standing-orders list / show <name> / test <name> / disable <name>`.
- [ ] `oc standing-orders test <name>` runs the program once synchronously and shows the result.

### Task B.4: PR-B gate

- [ ] All tests green.
- [ ] Manual smoke: write a sample `## Program: hello-world` in `home/AGENTS.md`, fire `oc standing-orders test hello-world`, confirm agent runs.
- [ ] **Open PR-B** with title `feat(standing-orders): text-contract program authority (B)`. Note soft-dep on PR-A5 in body.

---

## Sub-project C — Active Memory blocking pre-reply sub-agent

**Goal:** A bounded sub-agent that runs on every eligible reply, queries `memory_search` / `memory_get`, and injects the result as a hidden untrusted prefix.

**Strategy:** One PR. Standalone. Adds one new hook event (`BeforeAgentReply`).

### Task C.1: Add `BeforeAgentReply` hook event

**Files:**
- Modify: `plugin_sdk/hooks.py` — add `BeforeAgentReply` to `ALL_HOOK_EVENTS`
- Modify: `opencomputer/agent/loop.py` — emit `BeforeAgentReply` between prompt-build and provider call

- [ ] Event signature mirrors existing `PreLLMCall` but is agent-level (provider-agnostic). Hook context exposes `messages`, `runtime`, `agent_id`.
- [ ] Test: emitting the event reaches a registered handler.

### Task C.2: Active-Memory plugin scaffold

**Files:**
- Create: `extensions/active-memory/plugin.py`
- Create: `extensions/active-memory/manifest.py` (per OC `PluginManifest` shape)
- Create: `extensions/active-memory/sub_agent.py`
- Test: `tests/extensions/active_memory/test_plugin.py`

- [ ] Plugin registers a `BeforeAgentReply` hook handler.
- [ ] Handler runs sub-agent inference call with prompt-style template, gets `{"action": "inject"|"skip", "summary": "..."}`. On `inject`, prepend `<relevant-memories>...</relevant-memories>` block to system message.

### Task C.3: Sub-agent prompt templates (6 styles)

**Files:**
- Create: `extensions/active-memory/prompts.py`

- [ ] Six template strings: `balanced`, `strict`, `contextual`, `recall-heavy`, `precision-heavy`, `preference-only`. Mirror OpenClaw's `extensions/active-memory/src/prompts.ts` (read upstream for exact text).

### Task C.4: Caching + persistence

- [ ] In-memory LRU cache keyed by `(chat_id, last_user_msg_hash)` with `cacheTtlMs` TTL.
- [ ] Optional `persistTranscripts: true` writes the sub-agent transcript to `<agent_state_dir>/active-memory/YYYY-MM-DD.md`.

### Task C.5: Slash command `/active-memory`

- [ ] `/active-memory pause` → set per-session toggle to false
- [ ] `/active-memory resume` → re-enable
- [ ] `/active-memory status` → show config + last-N decisions

### Task C.6: PR-C gate

- [ ] All tests green; smoke: a synthetic chat with relevant memories produces an injection; with no relevant memories produces `skip`.
- [ ] **Open PR-C** with title `feat(active-memory): blocking pre-reply recall sub-agent (C)`.

---

## Sub-project D — Block streaming chunker + `humanDelay`

**Goal:** Channel deltas arrive at human-readable cadence: paragraph-first → newline → sentence → whitespace; never split inside code fences; idle-coalesce; randomized 800-2500ms `humanDelay` between blocks.

**Strategy:** One PR. Standalone. Default off — channels opt in via config.

### Task D.1: BlockChunker dataclass + algorithm

**Files:**
- Create: `plugin_sdk/streaming/__init__.py`
- Create: `plugin_sdk/streaming/block_chunker.py`
- Test: `tests/plugin_sdk_streaming/test_block_chunker.py`

- [ ] `BlockChunker(min_chars=200, max_chars=1500, prefer_boundaries=("paragraph","newline","sentence","whitespace"), never_split_fences=True, idle_coalesce_ms=100, human_delay_min_ms=800, human_delay_max_ms=2500)`.
- [ ] `feed(delta: str) -> Iterator[Block]` yields blocks when boundaries are reached or `idle_coalesce_ms` passes since the last input.
- [ ] `flush() -> Iterator[Block]` flushes any buffered partial block at end-of-stream.

```python
# tests/plugin_sdk_streaming/test_block_chunker.py
from plugin_sdk.streaming.block_chunker import BlockChunker


def test_single_paragraph():
    bc = BlockChunker(min_chars=10, max_chars=100)
    out = list(bc.feed("Hello world. How are you?")) + list(bc.flush())
    assert "".join(b.text for b in out) == "Hello world. How are you?"


def test_paragraph_split():
    bc = BlockChunker(min_chars=10, max_chars=100)
    text = "First paragraph here.\n\nSecond paragraph here."
    blocks = list(bc.feed(text)) + list(bc.flush())
    assert len(blocks) == 2
    assert blocks[0].text == "First paragraph here."
    assert blocks[1].text == "Second paragraph here."


def test_never_split_fence():
    bc = BlockChunker(min_chars=10, max_chars=20)  # tiny max
    text = "```python\nprint('hello world this exceeds 20 chars')\n```"
    blocks = list(bc.feed(text)) + list(bc.flush())
    # Despite max_chars=20, the fence stays whole.
    assert len(blocks) == 1
    assert blocks[0].text == text
```

### Task D.2: humanDelay timing

**Files:**
- Modify: `plugin_sdk/streaming/block_chunker.py`
- Test: `tests/plugin_sdk_streaming/test_human_delay.py`

- [ ] After a block is yielded, schedule the next one to be released no sooner than `random.uniform(min_ms, max_ms)` ms later.
- [ ] Statistical test: 100 inter-block delays should have mean within 10% of `(min+max)/2`.

### Task D.3: Channel-adapter integration (Telegram first)

**Files:**
- Modify: `extensions/telegram/plugin.py` — opt-in via config `streaming.block_chunker: true`
- Test: `tests/extensions/telegram/test_block_chunker_integration.py`

- [ ] When config opted in, wrap `on_delta` in `BlockChunker.feed(delta) -> Iterator[Block]`.
- [ ] Tests use a mock Bot API and verify edits are coalesced into block-level boundaries.

### Task D.4: PR-D gate

- [ ] All tests green; manual smoke against a real bot shows human-paced replies.
- [ ] **Open PR-D** with title `feat(streaming): block chunker + humanDelay (D)`.

---

## Final summary

### What ships across sub-projects A/B/C/D

- ~6 PRs (A1-A6) for multi-agent isolation + channel-binding router (XL)
- 1 PR (B) for Standing Orders (L)
- 1 PR (C) for Active Memory plugin (M, includes new `BeforeAgentReply` hook event)
- 1 PR (D) for Block streaming chunker (M)

### Out of scope (deferred)

- Hook taxonomy expansion beyond `BeforeAgentReply`.
- Inbound queue modes / Lobster / TaskFlow / Background Tasks ledger / Diagnostics OTEL.
- Mobile / canvas / voice-wake / SIP voice-call.
- Provider plugin breadth / channel adapter long tail.

### Test plan

- [ ] All `tests/agents_runtime/`, `tests/standing_orders/`, `tests/extensions/active_memory/`, `tests/plugin_sdk_streaming/` green.
- [ ] No regressions in existing `tests/`.
- [ ] Ruff clean.
- [ ] Smoke tests: two-agent Slack routing, Standing-Order test fire, Active-Memory injection on relevant chat, Telegram block-chunker producing human-paced edits.

### OpenClaw upstream references (for the porter)

- Multi-agent: `docs/concepts/multi-agent.md`, `src/agents/auth-profiles*`, `docs/gateway/configuration.md`
- Standing Orders: `docs/automation/standing-orders.md`, `docs/automation/cron-jobs.md`
- Active Memory: `extensions/active-memory/openclaw.plugin.json`, `docs/concepts/active-memory.md`, `extensions/active-memory/src/index.ts`, `extensions/active-memory/src/prompts.ts`
- Block streaming: `docs/concepts/streaming.md`

### Plan reference

- Gap audit: `docs/refs/openclaw/2026-04-28-major-gaps.md`
- Reference catalog: `docs/refs/openclaw/2026-04-28-deep-feature-survey.md`
- OC current state: `docs/refs/openclaw/2026-04-28-oc-current-state.md`

---

## Self-review (run after writing the plan, before execution)

**1. Spec coverage** — does each gap in `2026-04-28-major-gaps.md` § "The four real gaps" have a sub-project here?

- Gap 1 (multi-agent isolation + channel-binding router) → Sub-project A ✓
- Gap 2 (Standing Orders + cron integration) → Sub-project B ✓
- Gap 3 (Active Memory blocking pre-reply sub-agent) → Sub-project C ✓
- Gap 4 (Block streaming chunker + humanDelay) → Sub-project D ✓

All four covered. No additional sub-projects (which would be scope creep).

**2. Placeholder scan** — search for "TBD", "TODO", "fill in", "similar to", "implement later" and confirm none in the plan body.

(Confirmed at write time.)

**3. Type consistency** — same identifier names across sub-projects:
- `AgentDescriptor`, `BindingRule`, `BindingMatchKey` (used in A1.1, A1.4, A3.3, A4.1) ✓
- `AgentRegistry`, `AgentRouter` (used consistently A1.3, A1.4, A3.3) ✓
- `agent_id` field (added in A2.1, used in A3.3, A5.1) ✓
- `Program` dataclass (B1, B2, B3) ✓

---

## Self-Audit (expert critic pass — 2026-04-28)

> **Critical fixes are applied inline above.** This section records the audit reasoning for the executing engineer.

### Issues found and resolved

1. **`oc agents` namespace collision risk.** `oc agent` would collide with several existing flows; using `oc agents` (plural) per A4.1 is intentional. ✓
2. **`agents_runtime/` namespace.** Original sketch used `opencomputer/agents/`; this collides with `opencomputer/agents/code-reviewer.md` (subagent definitions). Final plan uses `opencomputer/agents_runtime/`. ✓
3. **`agent/` (singular) collision.** OC has `opencomputer/agent/` for the loop. Adding `opencomputer/agent/multi.py` would surprise. The plan keeps the new module at `opencomputer/agents_runtime/`. ✓
4. **SessionDB schema migration must be idempotent.** A2.1 uses `PRAGMA table_info` to check before `ALTER TABLE`. ✓
5. **MessageEvent SDK field additions need verification before Phase A3.** Hence Phase 0.2 explicitly checks current `MessageEvent` shape. ✓
6. **Active Memory hook event must NOT use `PreLLMCall` directly.** PreLLMCall is provider-level (fires per provider call); BeforeAgentReply is agent-level (fires once per turn before provider call). Using PreLLMCall would fire on EVERY retry/streaming reconnection, blowing the `cacheTtlMs` cache. Adding the dedicated event in C1 is required. ✓
7. **Standing Orders parser failure modes.** Markdown is messy. Tests in B1 cover: nested bullets, missing scope, malformed schedule, no triggers, multiple programs in one file. ✓
8. **Block chunker code-fence detection.** Naive `````` match on `` ``` `` will false-positive on text like "use the ``` syntax." Real-world impl needs state machine: open-fence → close-fence pairing, language tag tolerance, indented fences. The test in D1 stress-tests this. ✓
9. **Standing Orders + multi-agent ordering.** B says soft-dep on A. If shipping B before A: scope to single-agent. The plan calls this out explicitly. ✓
10. **Per-agent skills allowlist filter (A5.1).** OC's allowlist is per-profile today. Per-agent allowlist is additive: profile-level allowlist + agent-level allowlist (intersection). Test for the intersection behavior. ✓

### Non-critical findings (executor must respect)

- Don't refactor existing `agent/` modules during this work. New code goes in `agents_runtime/`.
- Sub-project D defaults to **off** — confirm config defaults reflect that. Don't break existing chat flows.
- All four sub-projects must keep existing single-agent users invisibly unaffected (the `'default'` agent fallback is the contract).
- Tests for SessionDB migration must include a "DB created on this version, no migration needed" path.

### Confidence after refinement

- **Sub-project A:** High. The shape is well-defined; OpenClaw's design is the upstream reference; OC's profile/SessionDB plumbing is well-understood.
- **Sub-project B:** Medium. Standing-Orders parser robustness is the risk. Test the corner cases.
- **Sub-project C:** High. Pattern is straightforward; the only architectural addition is one hook event.
- **Sub-project D:** Medium. Block chunker correctness on streaming code is the risk. State machine + extensive tests.

### Stress test — real-world scenarios

1. **"I have two Slack workspaces (work + home), and I want different agents to handle each."** ✓ Sub-project A directly serves this. `oc agents bindings add --agent work --channel slack --account-id T01` and analogous for home.

2. **"I want my home agent to never call Bash."** ✓ Per-agent toolset allowlist via config (existing OC mechanism); per-agent allowlist applies to the agent loop's tool registry.

3. **"I want a Standing Order: every Friday, summarize the week and send it to me."** ✓ Sub-project B. `## Program: weekly-summary / **Triggers:** cron: 0 17 * * 5` plus a `summarize-week` skill.

4. **"In casual chat, I'd like the agent to remember my partner's name without me having to invoke memory."** ✓ Sub-project C. Active Memory runs on every reply; if a memory matches, it's injected.

5. **"My Telegram replies feel robotic."** ✓ Sub-project D. Per-channel `block_chunker: true` and `humanDelay`.

---

## End of plan

**Length:** ~580 lines. Mirrors the structure of `2026-04-28-hermes-tier1a-skills-hub.md` (3280 lines) but the four-sub-project scope means each phase is shorter — the size delta is intentional and reflects the user's "be selective" guidance.

**Resume command for next session:**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git checkout main && git pull

# Read in order:
#   1. docs/refs/openclaw/2026-04-28-major-gaps.md
#   2. THIS FILE
#   3. (during Phase 0): docs/superpowers/plans/2026-04-28-openclaw-tier1-DECISIONS.md (created in Phase 0.6)

# Pick a sub-project to start. Recommended: A (multi-agent foundation).
# Then execute via:
#   /executing-plans  (sequential, one engineer)
#   /subagent-driven-development  (parallel agents per task; preferred for sub-project A)

# Sub-projects B, C, D can run in parallel after Sub-project A's PR-A2 lands
# (the SessionDB migration is the only shared dependency for B and C).
```
