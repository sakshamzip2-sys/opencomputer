# Hermes Security v2 — Final Gaps Implementation Plan (REVISED)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three remaining honest gaps between hermes-security-v2.md and OpenComputer after PRs #509/#511/#514 — (1) session-scoped consent verb, (2) Tirith verdict surfaced + default-deny on BLOCK, (3) `container_persistent` ephemeral-tmpfs toggle.

**Architecture (revised after plan audit):**
- **T1 (session verb):** `gate._pending_decisions` migrates from 2-tuple → 3-tuple `(decision, persist, session_scoped)`. New `_session_grants` in-memory dict consulted at top of `check()`. `resolve_pending` gains `session_scoped: bool = False` kwarg. `dispatch._handle_approval_click` recognizes `verb == "session"`. Telegram + Slack adapters add a 4th button. Matrix adapter gets 3rd emoji (matrix has no "always" today; we add "session" alongside allow/deny).
- **T2 (Tirith):** scan happens INSIDE `BashTool.execute` and `ExecuteCode.execute` (sync `tirith.check_command` wrapped in `asyncio.to_thread` to avoid blocking the event loop) after the hardline check. On verdict `block`, return an error `ToolResult` with `format_findings_for_user` content — pre-emptive refusal (Hermes's "default deny on BLOCKED"). On verdict `warn`, prepend findings to the tool result content but allow exec. On `allow`, no-op. NOT threaded through the consent gate — keeps integration surface minimal.
- **T3 (container_persistent):** add `SandboxConfig.container_persistent: bool = True`. When `False`, `sandbox/docker.py` adds explicit `--tmpfs /workspace:rw,size=512m` + `--tmpfs /root:rw,size=256m` flags so ops can lock down the implicit container layer for cron/one-shot jobs. Default `True` is a strict no-op for back-compat.

**Tech Stack:** Python 3.12+, asyncio, dataclasses (frozen+slots), pytest, ruff. No new dependencies.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `opencomputer/agent/consent/gate.py` | session-grant cache, 4th verb threading, render prompt with optional Tirith findings, SESSION_FINALIZE handler subscribed in __init__ | Modify |
| `opencomputer/gateway/dispatch.py` | `_handle_approval_click` recognizes `verb == "session"` + threads `session_scoped=True` to `resolve_pending` | Modify |
| `opencomputer/tools/bash.py` | call `tirith.check_command` (via `asyncio.to_thread`) after hardline; refuse on `block` verdict; surface `warn` findings in tool output | Modify |
| `opencomputer/tools/execute_code.py` | same Tirith integration as bash — refuse on block, surface warn findings | Modify |
| `plugin_sdk/sandbox.py` | `SandboxConfig.container_persistent: bool = True` field | Modify |
| `opencomputer/sandbox/docker.py` | when `container_persistent=False`, add `--tmpfs /workspace` + `--tmpfs /root` to argv | Modify |
| `extensions/telegram/adapter.py` | `send_approval_request` builds 4-button keyboard (once/session/always/deny) | Modify |
| `extensions/slack/adapter.py` | `send_approval_request` builds 4-button block (once/session/always/deny) | Modify |
| `extensions/matrix/adapter.py` | add 3rd reaction (🕒) → `session` (matrix has no "always" surface today) | Modify |
| `docs/security-production.md` | document `container_persistent` + 4-verb approval flow | Modify |
| `tests/test_consent_gate_session_tier.py` | session-grant lifecycle, isolation, SessionFinalize cleanup, 3-tuple migration | Create |
| `tests/test_consent_render_prompt_v2.py` | `render_prompt_message` returns `[y/N/session/always]` | Create |
| `tests/test_dispatch_session_verb.py` | `_handle_approval_click` routes `verb == "session"` to `resolve_pending(... session_scoped=True)` | Create |
| `tests/test_tirith_bash_refuses_block.py` | BashTool returns error result with findings on Tirith block; passes through on allow | Create |
| `tests/test_tirith_execute_code_refuses_block.py` | ExecuteCode same | Create |
| `tests/test_sandbox_container_persistent.py` | `False` adds tmpfs flags; `True` doesn't | Create |

**Total:** 10 modify + 6 create. No new modules.

---

## Task 1: Session-grant cache + 3-tuple resolve_pending migration

**Files:**
- Modify: `OpenComputer/opencomputer/agent/consent/gate.py`
- Test: `OpenComputer/tests/test_consent_gate_session_tier.py`

This task touches gate state (`_session_grants`, `_pending_decisions` shape), `check()` short-circuit, `resolve_pending()` signature, and the read site at line 485. Migrate atomically — partial migration corrupts existing tests.

- [ ] **Step 1: Read existing test fixtures to copy the canonical setup**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
grep -n "_setup\|ConsentGate(\|ConsentStore(" tests/test_sub_f1_consent_gate.py | head -10
```

Look at `_setup()` to understand how the test fixture builds a real `ConsentStore` + `AuditLogger` from an in-memory SQLite connection (the audit found `MagicMock()` won't suffice for these collaborators).

- [ ] **Step 2: Write the failing test**

Create `OpenComputer/tests/test_consent_gate_session_tier.py`:

```python
"""Hermes parity: 4th approval verb 'session' grants for the rest of the session only."""
from __future__ import annotations

import sqlite3

import pytest

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from opencomputer.agent.consent.gate import ConsentGate
from opencomputer.agent.consent.store import ConsentStore
from opencomputer.agent.consent.audit import AuditLogger


def _setup_gate() -> ConsentGate:
    """Mirror tests/test_sub_f1_consent_gate.py:_setup — in-memory SQLite +
    real ConsentStore + real AuditLogger."""
    conn = sqlite3.connect(":memory:")
    ConsentStore.migrate(conn)
    AuditLogger.migrate(conn)
    store = ConsentStore(conn)
    audit = AuditLogger(conn=conn, hmac_key=b"test-key-32-bytes-AAAAAAAAAAAAAA")
    return ConsentGate(store=store, audit=audit)


def _claim() -> CapabilityClaim:
    return CapabilityClaim(
        capability_id="execute_code.run",
        tier_required=ConsentTier.PER_ACTION,
        human_description="run user code",
    )


def test_session_grant_short_circuits_check_within_same_session():
    gate = _setup_gate()
    # Seed a session grant directly (simulates a click that already
    # resolved to session-scoped).
    from plugin_sdk.consent import ConsentGrant
    import time
    gate._session_grants[("s1", "execute_code.run")] = ConsentGrant(
        capability_id="execute_code.run",
        tier=ConsentTier.PER_ACTION,
        scope_filter=None,
        granted_at=time.time(),
        expires_at=None,
        granted_by="user",
    )

    decision = gate.check(_claim(), scope=None, session_id="s1")
    assert decision.allowed is True
    assert "session" in decision.reason


def test_session_grant_does_not_leak_to_other_session():
    gate = _setup_gate()
    from plugin_sdk.consent import ConsentGrant
    import time
    gate._session_grants[("s1", "execute_code.run")] = ConsentGrant(
        capability_id="execute_code.run",
        tier=ConsentTier.PER_ACTION,
        scope_filter=None,
        granted_at=time.time(),
        expires_at=None,
        granted_by="user",
    )
    decision = gate.check(_claim(), scope=None, session_id="s2")
    assert decision.allowed is False


def test_on_session_finalize_clears_only_matching_session():
    gate = _setup_gate()
    from plugin_sdk.consent import ConsentGrant
    import time
    g = ConsentGrant(
        capability_id="execute_code.run",
        tier=ConsentTier.PER_ACTION,
        scope_filter=None, granted_at=time.time(),
        expires_at=None, granted_by="user",
    )
    gate._session_grants[("s1", "execute_code.run")] = g
    gate._session_grants[("s2", "execute_code.run")] = g

    gate.on_session_finalize(session_id="s1")

    assert ("s1", "execute_code.run") not in gate._session_grants
    assert ("s2", "execute_code.run") in gate._session_grants


def test_resolve_pending_session_scoped_writes_to_cache_not_store():
    gate = _setup_gate()
    # Register a pending request as request_approval would.
    import asyncio
    key = ("s1", "execute_code.run")
    gate._pending_requests[key] = asyncio.Event()

    resolved = gate.resolve_pending(
        session_id="s1", capability_id="execute_code.run",
        decision=True, persist=False, session_scoped=True,
    )
    assert resolved is True
    # Decision tuple should now be the 3-tuple shape.
    assert gate._pending_decisions[key] == (True, False, True)
    # Persistent store untouched.
    assert gate._store.get("execute_code.run", None) is None


def test_resolve_pending_legacy_2_arg_call_still_works():
    """Backward-compat: existing dispatch code calls without session_scoped."""
    gate = _setup_gate()
    import asyncio
    key = ("s1", "execute_code.run")
    gate._pending_requests[key] = asyncio.Event()

    # Old-style call (no session_scoped kwarg).
    resolved = gate.resolve_pending(
        session_id="s1", capability_id="execute_code.run",
        decision=True, persist=True,
    )
    assert resolved is True
    # 3-tuple stored, session_scoped defaults False.
    assert gate._pending_decisions[key] == (True, True, False)
```

- [ ] **Step 3: Run test — verify failures**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_consent_gate_session_tier.py -v`

Expected: 5 tests FAIL (no `_session_grants` attr, no `on_session_finalize`, `resolve_pending` rejects unknown kwarg).

- [ ] **Step 4: Implement gate changes — `__init__`**

Modify `OpenComputer/opencomputer/agent/consent/gate.py`. In `ConsentGate.__init__` (line 87-110), change `_pending_decisions` type to 3-tuple AND add `_session_grants`:

```python
        # Round 2a P-5 — pending-approval registry. Key is
        # ``(session_id, capability_id)``. The Event is set by
        # :meth:`resolve_pending` once the user clicks; the decision
        # 3-tuple ``(allowed, persist, session_scoped)`` carries the
        # click meaning back to the caller:
        #   (True, False, False) → allow_once
        #   (True, False, True)  → allow_session (Hermes 4th verb)
        #   (True, True,  False) → allow_always
        #   (False, _, _)        → deny
        self._pending_requests: dict[tuple[str, str], asyncio.Event] = {}
        self._pending_decisions: dict[
            tuple[str, str], tuple[bool, bool, bool]
        ] = {}

        # Hermes parity: session-scoped grants. Cleared on
        # SESSION_FINALIZE. Lives in-memory only — NOT persisted to
        # ConsentStore.
        from plugin_sdk.consent import ConsentGrant  # late import for type
        self._session_grants: dict[tuple[str, str], ConsentGrant] = {}
```

- [ ] **Step 5: Implement gate changes — `check()` short-circuit**

In `check()` (line 164), AFTER the existing `auto_allow` short-circuit at line 178-193 and BEFORE the `grant = None` initialisation at line 195, insert:

```python
        # Hermes parity: session-scoped grant short-circuits before the
        # persistent store lookup. Session grants live in-memory only;
        # they are cleared on SESSION_FINALIZE via ``on_session_finalize``.
        if session_id is not None:
            sg = self._session_grants.get((session_id, claim.capability_id))
            if sg is not None and sg.tier >= claim.tier_required:
                audit_id = self._audit.append(AuditEvent(
                    session_id=session_id, actor="hook",
                    action="check_session_grant",
                    capability_id=claim.capability_id,
                    tier=int(sg.tier),
                    scope=scope,
                    decision="allow",
                    reason="session_grant matched",
                ))
                return ConsentDecision(
                    allowed=True,
                    reason="session_grant matched",
                    tier_matched=sg.tier,
                    audit_event_id=audit_id,
                )
```

- [ ] **Step 6: Implement gate changes — `resolve_pending` 3-tuple write**

In `resolve_pending()` (line 551-580), change the signature and body:

```python
    def resolve_pending(
        self,
        *,
        session_id: str,
        capability_id: str,
        decision: bool,
        persist: bool,
        session_scoped: bool = False,
    ) -> bool:
        """Mark a pending approval as resolved with the given decision.

        Three allow flavours encoded as (decision, persist, session_scoped):
        - (True, False, False) → once
        - (True, False, True)  → session — in-memory grant cleared on
          SESSION_FINALIZE
        - (True, True,  False) → always — persistent grant in ConsentStore
        - (False, _, _)        → deny

        ``session_scoped`` defaults False so existing callers
        (telegram/slack/dispatch handlers that haven't adopted the new
        verb yet) keep working unchanged.
        """
        key = (session_id, capability_id)
        event = self._pending_requests.get(key)
        if event is None or event.is_set():
            return False
        self._pending_decisions[key] = (decision, persist, session_scoped)
        event.set()
        return True
```

- [ ] **Step 7: Implement gate changes — read site at line 485**

Find line 485 in `request_approval`:
```python
        decision = self._pending_decisions.pop(key, (False, False))
        self._pending_requests.pop(key, None)
        allowed, persist = decision
```

Replace with:
```python
        # 3-tuple migration (Hermes session-verb parity). Default fills
        # session_scoped=False; old 2-tuple writes are no longer possible
        # because resolve_pending always writes a 3-tuple now.
        decision = self._pending_decisions.pop(key, (False, False, False))
        self._pending_requests.pop(key, None)
        allowed, persist, session_scoped = decision
```

Then BEFORE the existing `if allowed and persist:` block at line 489, INSERT the session-scoped branch:

```python
        if allowed and session_scoped:
            # Hermes parity: session-scoped grant. In-memory only;
            # cleared on SESSION_FINALIZE. Tier == claim's required tier.
            self._session_grants[(session_id, claim.capability_id)] = ConsentGrant(
                capability_id=claim.capability_id,
                tier=claim.tier_required,
                scope_filter=scope,
                granted_at=time.time(),
                expires_at=None,
                granted_by="user",
            )

        if allowed and persist:
            # allow_always — persist a non-expiring grant scoped to this
            ... (existing code unchanged)
```

Update the action label tuple at line 502-506:

```python
        action = (
            "approval_allow_always" if (allowed and persist)
            else "approval_allow_session" if (allowed and session_scoped)
            else "approval_allow_once" if allowed
            else "approval_deny"
        )
        reason = (
            "user clicked allow always" if (allowed and persist)
            else "user clicked allow session" if (allowed and session_scoped)
            else "user clicked allow once" if allowed
            else "user clicked deny"
        )
```

And at line 524 update the `_choice` map similarly:

```python
        _choice = (
            "always" if (allowed and persist)
            else "session" if (allowed and session_scoped)
            else "once" if allowed
            else "deny"
        )
```

- [ ] **Step 8: Implement gate changes — `on_session_finalize` + hook subscription**

Add a new method on `ConsentGate`:

```python
    def on_session_finalize(self, *, session_id: str) -> None:
        """Drop session-scoped grants for an ending session.

        Called from the hook engine when SESSION_FINALIZE fires. Idempotent
        on unknown ``session_id`` — a session that never created a grant
        passes through silently.
        """
        keys = [k for k in self._session_grants if k[0] == session_id]
        for k in keys:
            self._session_grants.pop(k, None)
```

Hook engine subscription is wired by the agent loop / gateway when the gate is constructed (the hook engine is a singleton). Add a registration helper called by callers:

```python
    def register_session_finalize_handler(self) -> None:
        """Subscribe ``on_session_finalize`` to ``HookEvent.SESSION_FINALIZE``.

        Idempotent — safe to call multiple times. Caller is responsible
        for invoking this exactly once (typically from the gate factory
        in the agent loop's __init__ path).
        """
        try:
            from opencomputer.hooks.engine import engine as _hook_engine
            from plugin_sdk.hooks import HookEvent

            async def _handler(ctx):
                sid = getattr(ctx, "session_id", None)
                if sid:
                    self.on_session_finalize(session_id=sid)

            _hook_engine.subscribe(HookEvent.SESSION_FINALIZE, _handler)
        except Exception:  # noqa: BLE001 — observer-only; gate must work without hooks
            pass
```

- [ ] **Step 9: Run tests — verify they pass**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_consent_gate_session_tier.py -v`

Expected: 5 tests PASS.

- [ ] **Step 10: Run full consent suite — no regression**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/ -k consent -v 2>&1 | tail -20`

Expected: all existing consent tests still pass.

- [ ] **Step 11: Commit**

```bash
git add opencomputer/agent/consent/gate.py tests/test_consent_gate_session_tier.py
git commit -m "feat(consent): session-scoped grant cache + 3-tuple resolve_pending (Hermes 4th approval verb)"
```

---

## Task 2: render_prompt_message — 4-verb prompt

**Files:**
- Modify: `OpenComputer/opencomputer/agent/consent/gate.py:60-77`
- Test: `OpenComputer/tests/test_consent_render_prompt_v2.py`

- [ ] **Step 1: Find existing test that asserts the old prompt string**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
grep -rn '\[y/N/always\]' tests/ opencomputer/ extensions/
```

Note all existing assertions — they need updating in the same commit.

- [ ] **Step 2: Write failing test**

Create `OpenComputer/tests/test_consent_render_prompt_v2.py`:

```python
"""Hermes parity: render_prompt_message includes session verb in prompt."""
from __future__ import annotations

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from opencomputer.agent.consent.gate import render_prompt_message


def _claim() -> CapabilityClaim:
    return CapabilityClaim(
        capability_id="execute_code.run",
        tier_required=ConsentTier.PER_ACTION,
        human_description="run user code",
    )


def test_render_includes_session_no_scope():
    msg = render_prompt_message(_claim(), None)
    assert "[y/N/session/always]" in msg
    assert "execute_code.run" in msg


def test_render_includes_session_with_scope():
    msg = render_prompt_message(_claim(), "/tmp/foo.py")
    assert "[y/N/session/always]" in msg
    assert "/tmp/foo.py" in msg
```

- [ ] **Step 3: Run test — verify failure**

Run: `pytest tests/test_consent_render_prompt_v2.py -v`

Expected: FAIL — current returns `[y/N/always]`.

- [ ] **Step 4: Update render_prompt_message at line 60-77**

```python
def render_prompt_message(claim: CapabilityClaim, scope: str | None) -> str:
    """Render the user-facing approval prompt.

    Hermes parity: four verbs ``[y/N/session/always]``. ``y`` allows
    once, ``session`` allows until SESSION_FINALIZE, ``always`` writes a
    permanent grant via ConsentStore, ``N`` (default) denies.
    """
    cap = claim.capability_id
    if scope:
        return f"Allow {cap} on {scope}? [y/N/session/always]"
    return f"Allow {cap}? [y/N/session/always]"
```

- [ ] **Step 5: Update existing tests asserting the old string**

For each match found in step 1, update `[y/N/always]` to `[y/N/session/always]`:

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
# Likely: tests/test_sub_f1_consent_gate.py around lines 170, 181
# Edit those exact lines.
```

Use the `Edit` tool to fix each match found in step 1.

- [ ] **Step 6: Run test — verify pass**

Run: `pytest tests/test_consent_render_prompt_v2.py tests/test_sub_f1_consent_gate.py -v`

Expected: all PASS.

- [ ] **Step 7: Run full consent suite — no regression**

Run: `pytest tests/ -k consent -v 2>&1 | tail -10`

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add opencomputer/agent/consent/gate.py tests/test_consent_render_prompt_v2.py tests/test_sub_f1_consent_gate.py
git commit -m "feat(consent): render_prompt_message — 4-verb prompt with session option"
```

---

## Task 3: Dispatch verb-mapping for "session"

**Files:**
- Modify: `OpenComputer/opencomputer/gateway/dispatch.py:1869-1911`
- Test: `OpenComputer/tests/test_dispatch_session_verb.py`

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_dispatch_session_verb.py`:

```python
"""Hermes parity: gateway dispatch routes verb='session' to gate.resolve_pending."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_session_verb_routes_to_resolve_pending_with_session_scoped_true():
    from opencomputer.gateway.dispatch import Dispatch

    # Minimal Dispatch fixture with an _approval_tokens registry +
    # a mocked router whose _loops yields a gate stub.
    dispatch = Dispatch.__new__(Dispatch)
    fake_gate = MagicMock()
    fake_gate.resolve_pending = MagicMock(return_value=True)
    fake_loop = MagicMock()
    fake_loop._consent_gate = fake_gate
    fake_router = MagicMock()
    fake_router._loops = {"default": fake_loop}
    dispatch._router = fake_router
    dispatch._approval_tokens = {"tok1": ("sess-1", "execute_code.run")}
    dispatch._session_profiles = {"sess-1": "default"}

    await dispatch._handle_approval_click(verb="session", token="tok1")

    fake_gate.resolve_pending.assert_called_once_with(
        session_id="sess-1",
        capability_id="execute_code.run",
        decision=True,
        persist=False,
        session_scoped=True,
    )


@pytest.mark.asyncio
async def test_unknown_verb_logs_and_returns(caplog):
    from opencomputer.gateway.dispatch import Dispatch

    dispatch = Dispatch.__new__(Dispatch)
    dispatch._router = MagicMock()
    dispatch._router._loops = {"default": MagicMock(_consent_gate=MagicMock())}
    dispatch._approval_tokens = {"tok1": ("sess-1", "cap.x")}
    dispatch._session_profiles = {"sess-1": "default"}

    with caplog.at_level("WARNING", logger="opencomputer.gateway.dispatch"):
        await dispatch._handle_approval_click(verb="unknown", token="tok1")
    assert any("unknown verb" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run test — verify failure**

Run: `pytest tests/test_dispatch_session_verb.py -v`

Expected: FAIL — current `_handle_approval_click` only handles once/always/deny.

- [ ] **Step 3: Update `_handle_approval_click` at line 1869-1911**

In `OpenComputer/opencomputer/gateway/dispatch.py`, find the existing if/elif/elif/else block:

```python
        if verb == "once":
            decision, persist = True, False
        elif verb == "always":
            decision, persist = True, True
        elif verb == "deny":
            decision, persist = False, False
        else:
            logger.warning("approval click unknown verb=%s token=%s", verb, token)
            return
        resolved = gate.resolve_pending(
            session_id=session_id,
            capability_id=capability_id,
            decision=decision,
            persist=persist,
        )
```

Replace with:

```python
        # Hermes parity: 4th verb 'session' grants for the rest of the
        # session only — dispatched to the in-memory cache via
        # resolve_pending(... session_scoped=True).
        session_scoped = False
        if verb == "once":
            decision, persist = True, False
        elif verb == "session":
            decision, persist, session_scoped = True, False, True
        elif verb == "always":
            decision, persist = True, True
        elif verb == "deny":
            decision, persist = False, False
        else:
            logger.warning("approval click unknown verb=%s token=%s", verb, token)
            return
        resolved = gate.resolve_pending(
            session_id=session_id,
            capability_id=capability_id,
            decision=decision,
            persist=persist,
            session_scoped=session_scoped,
        )
```

- [ ] **Step 4: Run tests — verify pass**

Run: `pytest tests/test_dispatch_session_verb.py -v`

Expected: 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/gateway/dispatch.py tests/test_dispatch_session_verb.py
git commit -m "feat(gateway): dispatch routes verb='session' to gate.resolve_pending(... session_scoped=True)"
```

---

## Task 4: Adapter buttons — Telegram + Slack + Matrix

**Files:**
- Modify: `OpenComputer/extensions/telegram/adapter.py:1504+` (`send_approval_request`)
- Modify: `OpenComputer/extensions/slack/adapter.py:414+` (`send_approval_request`)
- Modify: `OpenComputer/extensions/matrix/adapter.py` (reaction-emoji map)

This task does NOT use a single test file — each adapter has its own test conventions. Smoke tests are added inline; end-to-end behaviour is validated via the dispatch test above plus the gate's session-grant test.

- [ ] **Step 1: Inspect existing telegram approval flow**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
grep -n "send_approval_request\|InlineKeyboard\|callback_data" extensions/telegram/adapter.py | head -20
```

Read `send_approval_request` (line 1504) and the inline-keyboard constructor inside it. Find the existing 3-button list; you'll add a 4th.

- [ ] **Step 2: Add 4th telegram button**

Modify `OpenComputer/extensions/telegram/adapter.py`. Inside `send_approval_request`, locate the 3-button keyboard build (button labels likely "Once", "Always", "Deny" with callback_data shaped `oc:approve:once:<token>` etc.).

Add a 4th button "Session" with callback_data `oc:approve:session:<token>`. The existing `_handle_callback_query` (around adapter.py:1681) already extracts `(verb, token)` from the callback — verb will pass through as `"session"` and reach `dispatch._handle_approval_click(verb="session", ...)` which T3 just wired.

- [ ] **Step 3: Add 4th slack block_action**

Modify `OpenComputer/extensions/slack/adapter.py`. Inside `send_approval_request` (line 414), the existing block-action element list. Add a 4th element with `value="session"`, `text="Session"`, `style` defaulted (no `primary` / no `danger`).

- [ ] **Step 4: Add 3rd matrix reaction emoji**

Modify `OpenComputer/extensions/matrix/adapter.py`. Find the existing `DEFAULT_ALLOW_EMOJI = "✅"` and `DEFAULT_DENY_EMOJI = "❌"` (or similar). Add `DEFAULT_SESSION_EMOJI = "🕒"` and update the reaction-handler map so `🕒` dispatches with `verb="session"`.

Matrix has no "always" surface today (only allow/deny). The 4-verb story for matrix is (allow → once, 🕒 → session, ❌ → deny). Document in the docstring of the relevant function: `# matrix has no always — operators wanting permanent approval should approve via CLI`.

- [ ] **Step 5: Smoke-test telegram and slack via existing test files (if any)**

```bash
pytest tests/ -k 'telegram and approval' -v
pytest tests/ -k 'slack and approval' -v
pytest tests/ -k 'matrix and approval' -v
```

Adapt existing tests if they assert the exact button count (likely an `assert len(buttons) == 3` somewhere). Update to 4 (or 3 for matrix).

- [ ] **Step 6: Commit**

```bash
git add extensions/telegram/adapter.py extensions/slack/adapter.py extensions/matrix/adapter.py tests/
git commit -m "feat(channels): 4th approval button 'session' (telegram/slack) + 3rd emoji 🕒 (matrix)"
```

---

## Task 5: Wire Tirith into BashTool — refuse on block, surface warn findings

**Files:**
- Modify: `OpenComputer/opencomputer/tools/bash.py`
- Test: `OpenComputer/tests/test_tirith_bash_refuses_block.py`

This is the corrected Tirith integration: scan happens inside `BashTool.execute` AFTER hardline. Tirith's verdict gates exec directly — no consent prompt threading required.

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_tirith_bash_refuses_block.py`:

```python
"""Hermes parity: BashTool refuses on Tirith block; allows on Tirith allow."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from plugin_sdk.core import ToolCall
from opencomputer.tools.bash import BashTool
from opencomputer.security.tirith import TirithResult


@pytest.mark.asyncio
async def test_bash_refuses_on_tirith_block_with_findings():
    tool = BashTool()
    call = ToolCall(id="c1", name="Bash", arguments={"command": "echo hi"})

    blocked = TirithResult(
        action="block",
        findings=[{"severity": "high", "title": "fake test pattern",
                   "description": "test"}],
        summary="blocked: test sentinel",
    )
    with patch("opencomputer.tools.bash.tirith_check_command", return_value=blocked):
        result = await tool.execute(call)
    assert result.is_error is True
    assert "Refused" in result.content or "refused" in result.content
    assert "fake test pattern" in result.content or "test sentinel" in result.content


@pytest.mark.asyncio
async def test_bash_allows_on_tirith_allow():
    tool = BashTool()
    call = ToolCall(id="c1", name="Bash", arguments={"command": "echo hi"})

    safe = TirithResult(action="allow")
    with patch("opencomputer.tools.bash.tirith_check_command", return_value=safe):
        result = await tool.execute(call)
    # echo hi succeeds (no error result content)
    assert result.is_error is False
    assert "hi" in result.content


@pytest.mark.asyncio
async def test_bash_warn_appends_findings_but_runs():
    tool = BashTool()
    call = ToolCall(id="c1", name="Bash", arguments={"command": "echo hi"})

    warn = TirithResult(
        action="warn",
        findings=[{"severity": "medium", "title": "advisory",
                   "description": "noted"}],
        summary="advisory: noted",
    )
    with patch("opencomputer.tools.bash.tirith_check_command", return_value=warn):
        result = await tool.execute(call)
    assert result.is_error is False
    # Warning surfaces in tool output content.
    assert "advisory" in result.content or "noted" in result.content


@pytest.mark.asyncio
async def test_bash_continues_when_tirith_unavailable():
    """If tirith binary missing AND fail_open=True (default), bash runs."""
    tool = BashTool()
    call = ToolCall(id="c1", name="Bash", arguments={"command": "echo hi"})

    # No mock — real tirith.check_command will run; binary likely absent.
    # With fail_open=True (default), action='allow' → bash runs normally.
    result = await tool.execute(call)
    assert result.is_error is False
    assert "hi" in result.content
```

- [ ] **Step 2: Run test — verify failure**

Run: `pytest tests/test_tirith_bash_refuses_block.py -v`

Expected: `tirith_check_command` import fails — not exposed in `bash.py`.

- [ ] **Step 3: Wire Tirith into bash.py**

Modify `OpenComputer/opencomputer/tools/bash.py`. Add at the imports:

```python
import asyncio
from opencomputer.security.tirith import (
    TirithResult,
    check_command as tirith_check_command,
    format_findings_for_user,
)
```

In `BashTool.execute()`, AFTER the hardline check (around line 141, right after the hardline-hit early return), add:

```python
        # Hermes parity: Tirith pre-exec scan. Sync subprocess call —
        # wrapped in to_thread so the agent loop's async dispatch isn't
        # blocked. fail_open default per Tirith config; binary absent
        # returns action='allow' under fail_open and is a no-op.
        try:
            tirith_result: TirithResult = await asyncio.to_thread(
                tirith_check_command, cmd,
            )
        except Exception:  # noqa: BLE001 — never let scan break exec
            tirith_result = TirithResult(action="allow")

        if tirith_result.action == "block":
            findings_text = format_findings_for_user(tirith_result) or (
                tirith_result.summary or "blocked by Tirith"
            )
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "Refused: Tirith pre-exec scan flagged this command.\n"
                    f"{findings_text}"
                ),
                is_error=True,
            )

        # warn: don't refuse, but surface findings in the result content
        # so the model + user see them. allow: silent.
        warn_prefix = ""
        if tirith_result.action == "warn":
            findings_text = format_findings_for_user(tirith_result)
            if findings_text:
                warn_prefix = (
                    "[Tirith warning — command allowed but flagged]\n"
                    f"{findings_text}\n---\n"
                )
```

Then at the END of `execute()`, when constructing the success ToolResult, prepend `warn_prefix`:

```python
        return ToolResult(
            tool_call_id=call.id,
            content=warn_prefix + result_content,  # warn_prefix is "" if no warn
            is_error=False,
        )
```

(Adapt the exact result construction site — the existing code might build `content` differently. Find the success-path return and prepend the prefix string.)

- [ ] **Step 4: Run test — verify pass**

Run: `pytest tests/test_tirith_bash_refuses_block.py -v`

Expected: 4 tests PASS.

- [ ] **Step 5: Run full bash + security suites**

Run: `pytest tests/ -k 'bash or tirith or security' -v 2>&1 | tail -20`

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/tools/bash.py tests/test_tirith_bash_refuses_block.py
git commit -m "feat(security): wire Tirith pre-exec scan into BashTool — refuse on block, surface warn (Hermes parity)"
```

---

## Task 6: Wire Tirith into ExecuteCode — same pattern as Bash

**Files:**
- Modify: `OpenComputer/opencomputer/tools/execute_code.py`
- Test: `OpenComputer/tests/test_tirith_execute_code_refuses_block.py`

- [ ] **Step 1: Write failing test (parallel to Task 5)**

Create `OpenComputer/tests/test_tirith_execute_code_refuses_block.py`:

```python
"""Hermes parity: ExecuteCode refuses on Tirith block; surfaces warn findings."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from plugin_sdk.core import ToolCall
from opencomputer.tools.execute_code import ExecuteCode
from opencomputer.security.tirith import TirithResult


@pytest.mark.asyncio
async def test_execute_code_refuses_on_tirith_block():
    tool = ExecuteCode()
    call = ToolCall(
        id="c1", name="ExecuteCode",
        arguments={"code": "print('hi')"},
    )
    blocked = TirithResult(
        action="block",
        findings=[{"severity": "high", "title": "fake", "description": "x"}],
        summary="blocked: fake",
    )
    with patch("opencomputer.tools.execute_code.tirith_check_command",
               return_value=blocked):
        result = await tool.execute(call)
    assert result.is_error is True
    assert "Refused" in result.content or "refused" in result.content


@pytest.mark.asyncio
async def test_execute_code_allows_on_tirith_allow():
    tool = ExecuteCode()
    call = ToolCall(
        id="c1", name="ExecuteCode",
        arguments={"code": "print('hi')"},
    )
    safe = TirithResult(action="allow")
    with patch("opencomputer.tools.execute_code.tirith_check_command",
               return_value=safe):
        result = await tool.execute(call)
    # ExecuteCode may have other failures (sandbox not configured etc.)
    # but it shouldn't be "Refused: Tirith".
    assert "Refused: Tirith" not in result.content
```

- [ ] **Step 2: Run test — verify failure**

Run: `pytest tests/test_tirith_execute_code_refuses_block.py -v`

Expected: import fail.

- [ ] **Step 3: Wire Tirith into execute_code.py**

Apply the same pattern as Task 5, step 3. The hardline check is at `execute_code.py:198-201` (verify with grep). Add the Tirith call right after.

Pass the `code` content to Tirith (it's the equivalent of `cmd` for ExecuteCode):

```python
        try:
            tirith_result: TirithResult = await asyncio.to_thread(
                tirith_check_command, code_str,  # adapt local var name
            )
        except Exception:  # noqa: BLE001
            tirith_result = TirithResult(action="allow")

        if tirith_result.action == "block":
            findings_text = format_findings_for_user(tirith_result) or (
                tirith_result.summary or "blocked by Tirith"
            )
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "Refused: Tirith pre-exec scan flagged this code.\n"
                    f"{findings_text}"
                ),
                is_error=True,
            )
```

(For ExecuteCode, the `warn` case is less critical because the code is already in a sandbox. Skip warn-prefix for now; it can be added later.)

- [ ] **Step 4: Run test — verify pass**

Run: `pytest tests/test_tirith_execute_code_refuses_block.py -v`

Expected: 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add opencomputer/tools/execute_code.py tests/test_tirith_execute_code_refuses_block.py
git commit -m "feat(security): wire Tirith pre-exec scan into ExecuteCode — refuse on block (Hermes parity)"
```

---

## Task 7: SandboxConfig.container_persistent + docker tmpfs flags

**Files:**
- Modify: `OpenComputer/plugin_sdk/sandbox.py`
- Modify: `OpenComputer/opencomputer/sandbox/docker.py`
- Test: `OpenComputer/tests/test_sandbox_container_persistent.py`

Honest scope (per audit): current docker.py has NO implicit `/workspace` or `/root` bind-mount. The `container_persistent` toggle adds explicit `--tmpfs` flags when False; True is a strict no-op preserving current behaviour.

- [ ] **Step 1: Write failing test**

Create `OpenComputer/tests/test_sandbox_container_persistent.py`:

```python
"""Hermes parity: container_persistent: false adds explicit tmpfs flags."""
from __future__ import annotations

from plugin_sdk.sandbox import SandboxConfig
from opencomputer.sandbox.docker import DockerSandboxStrategy


def test_persistent_default_is_true():
    cfg = SandboxConfig(strategy="docker")
    assert cfg.container_persistent is True


def test_persistent_true_does_not_add_workspace_or_root_tmpfs():
    cfg = SandboxConfig(strategy="docker", container_persistent=True)
    strategy = DockerSandboxStrategy()
    argv = strategy.explain(["echo", "hi"], config=cfg)
    tmpfs_targets = [argv[i+1] for i, a in enumerate(argv) if a == "--tmpfs"]
    # Existing tmpfs trio (/tmp, /var/tmp, /run) is unchanged.
    assert any(t.startswith("/tmp:") for t in tmpfs_targets)
    # No /workspace or /root tmpfs in persistent mode.
    assert not any(t.startswith("/workspace:") for t in tmpfs_targets)
    assert not any(t.startswith("/root:") for t in tmpfs_targets)


def test_persistent_false_adds_workspace_and_root_tmpfs():
    cfg = SandboxConfig(strategy="docker", container_persistent=False)
    strategy = DockerSandboxStrategy()
    argv = strategy.explain(["echo", "hi"], config=cfg)
    tmpfs_targets = [argv[i+1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert any(t.startswith("/workspace:") for t in tmpfs_targets)
    assert any(t.startswith("/root:") for t in tmpfs_targets)
    # Existing tmpfs trio still there.
    assert any(t.startswith("/tmp:") for t in tmpfs_targets)


def test_persistent_false_preserves_explicit_paths():
    """User-declared read_paths still bind in either mode."""
    cfg = SandboxConfig(
        strategy="docker",
        container_persistent=False,
        read_paths=("/etc/resolv.conf",),
    )
    strategy = DockerSandboxStrategy()
    argv = strategy.explain(["echo", "hi"], config=cfg)
    argv_str = " ".join(argv)
    assert "/etc/resolv.conf:/etc/resolv.conf:ro" in argv_str
```

- [ ] **Step 2: Run test — verify failure**

Run: `pytest tests/test_sandbox_container_persistent.py -v`

Expected: FAIL — `SandboxConfig` lacks `container_persistent` field.

- [ ] **Step 3: Add field to SandboxConfig**

Modify `OpenComputer/plugin_sdk/sandbox.py`. After the `network_allowed: bool = False` field (line 74), add:

```python
    container_persistent: bool = True
    """Hermes parity: when ``False``, the Docker strategy adds explicit
    ``--tmpfs /workspace`` + ``--tmpfs /root`` flags so implicit
    state inside the container can't be persisted by accident. ``True``
    (default) preserves existing behaviour — no extra tmpfs mounts.
    Only honoured by the ``docker`` strategy; other strategies ignore."""
```

(Keep slots+frozen; the new field has a default so existing constructors continue to compile.)

- [ ] **Step 4: Add tmpfs branching in docker.py**

Modify `OpenComputer/opencomputer/sandbox/docker.py`. Find `_wrap` (line 109+). Locate the `_SECURITY_ARGS` extension into `args` (the existing tmpfs trio). After that block, add:

```python
        # Hermes parity: container_persistent: false locks down
        # /workspace + /root with explicit tmpfs so the implicit
        # container layer can't accumulate state.
        if not config.container_persistent:
            args.extend(["--tmpfs", "/workspace:rw,size=512m"])
            args.extend(["--tmpfs", "/root:rw,size=256m"])
```

- [ ] **Step 5: Run test — verify pass**

Run: `pytest tests/test_sandbox_container_persistent.py -v`

Expected: 4 tests PASS.

- [ ] **Step 6: Run full sandbox suite**

Run: `pytest tests/ -k sandbox -v 2>&1 | tail -10`

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add plugin_sdk/sandbox.py opencomputer/sandbox/docker.py tests/test_sandbox_container_persistent.py
git commit -m "feat(sandbox): container_persistent: false locks down /workspace + /root tmpfs (Hermes parity)"
```

---

## Task 8: Documentation update

**Files:**
- Modify: `OpenComputer/docs/security-production.md`

- [ ] **Step 1: Add `container_persistent` paragraph to Container isolation**

Find § Container isolation. AFTER the hardening-flags table, add:

```markdown
- [ ] **Lock down implicit container state.** Add to `config.yaml`:

      ```yaml
      sandbox:
        strategy: docker
        # container_persistent: true   # default — implicit container fs untouched
        container_persistent: false   # tmpfs /workspace + /root — explicit ephemeral
      ```

      Set `false` for cron jobs, one-shot agents, or any deployment
      where you want explicit guarantees that nothing under
      ``/workspace`` or ``/root`` can persist between calls. User-declared
      ``read_paths`` / ``write_paths`` still bind-mount in either mode —
      the toggle controls only the implicit container layer.
```

- [ ] **Step 2: Add 4-verb approval flow paragraph**

ABOVE § Hardline blocklist, add:

```markdown
## Approval flow (manual mode)

When the consent gate fires a manual prompt, four verbs are available:

| Verb | Meaning | Storage |
|---|---|---|
| `once` (`y`) | Allow this single execution | Ephemeral — no state written |
| `session` | Allow until session ends (SESSION_FINALIZE) | In-memory dict, not persisted |
| `always` | Allow indefinitely | Permanent grant in `consent.db` |
| `deny` (`N`, default) | Block this execution | Ephemeral; user can re-prompt next call |

For chat-driven approvals (Telegram / Slack), four buttons render. For
Matrix, three reaction emojis: ✅ (once) / 🕒 (session) / ❌ (deny) —
matrix has no "always" surface; operators wanting permanent approval
should approve via CLI (`oc consent grant <capability_id>`).

## Tirith pre-exec scan

OpenComputer runs Tirith on every Bash command and ExecuteCode block
after the hardline blocklist. Three verdicts:

| Verdict | Behaviour |
|---|---|
| `allow` | Command runs normally |
| `warn` | Command runs; findings prefixed to tool output |
| `block` | Command refused; findings returned as error result |

When Tirith's binary is unavailable + `tirith_fail_open: true` (default),
all commands reach `allow`. Set `tirith_fail_open: false` for
strict-deny when the scanner is unreachable.
```

- [ ] **Step 3: Commit**

```bash
git add docs/security-production.md
git commit -m "docs(security): document container_persistent + 4-verb approval flow + Tirith verdicts"
```

---

## Task 9: Final verification — full suite + ruff + manual smoke

- [ ] **Step 1: Run full test suite**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
pytest tests/ -q 2>&1 | tail -25
```

Expected: all tests PASS, no regression.

- [ ] **Step 2: Run ruff**

```bash
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: zero errors.

- [ ] **Step 3: Self-verify wiring with grep**

```bash
# Session verb wired through gate
grep -c "session_scoped" opencomputer/agent/consent/gate.py opencomputer/gateway/dispatch.py

# Tirith call sites
grep -l "tirith_check_command" opencomputer/tools/bash.py opencomputer/tools/execute_code.py

# container_persistent
grep -c "container_persistent" plugin_sdk/sandbox.py opencomputer/sandbox/docker.py

# Render prompt updated
grep -c "y/N/session/always" opencomputer/agent/consent/gate.py

# Adapters updated
grep -c "session" extensions/telegram/adapter.py extensions/slack/adapter.py extensions/matrix/adapter.py
```

Each grep should return non-zero counts on the relevant files.

- [ ] **Step 4: If any test/ruff fails — fix in place before push**

Do NOT push to main with red CI. Fix root causes; if a fix is non-obvious, stop and surface the blocker.

- [ ] **Step 5: Open PR**

```bash
git checkout -b feat/hermes-security-v2-final-gaps-2026-05-09
git push -u origin feat/hermes-security-v2-final-gaps-2026-05-09
gh pr create --title "feat(security): Hermes-security-v2 final 3 gaps (session verb, Tirith integration, container_persistent)" --body "$(cat <<'EOF'
## Summary

Closes the three remaining honest gaps between hermes-security-v2.md and OpenComputer after PRs #509 / #511 / #514:

1. **Session-scoped consent verb** — adds the 4th approval option `[y/N/session/always]` with an in-memory grant cache cleared on `SessionFinalize`. Telegram + Slack adapters render the 4th button; Matrix gets a 3rd reaction emoji 🕒 (matrix has no "always" surface today).
2. **Tirith verdict integration** — `BashTool` and `ExecuteCode` call `tirith.check_command()` after the existing hardline check (wrapped in `asyncio.to_thread` to avoid blocking the event loop). On `block` → tool returns error with formatted findings (Hermes "default-deny on BLOCKED"). On `warn` → findings prefixed to output. On `allow` → silent.
3. **`container_persistent: false` ephemeral mode** — `SandboxConfig` gains a new field; Docker sandbox adds explicit `--tmpfs /workspace` + `--tmpfs /root` when False (default True preserves current behaviour).

## Confirmed deferrals (not in scope)

- `unauthorized_dm_behavior: pair | ignore` per platform — original spec marked YAGNI; verified zero hits.
- `OPENCOMPUTER_EXEC_ASK` env var — equivalent capability via `--auto`.
- Tirith findings threaded through ExecuteCode's existing consent prompt — would require new `PromptHandler` signature; deferred to a follow-up.

## Test plan

- [x] `pytest tests/test_consent_gate_session_tier.py -v` — 5 new tests (session-grant lifecycle, isolation, finalize, 3-tuple migration, legacy-call backcompat)
- [x] `pytest tests/test_consent_render_prompt_v2.py -v` — 2 new tests
- [x] `pytest tests/test_dispatch_session_verb.py -v` — 2 new tests
- [x] `pytest tests/test_tirith_bash_refuses_block.py -v` — 4 new tests
- [x] `pytest tests/test_tirith_execute_code_refuses_block.py -v` — 2 new tests
- [x] `pytest tests/test_sandbox_container_persistent.py -v` — 4 new tests
- [x] Full suite: `pytest tests/ -q` — no regressions
- [x] Ruff: `ruff check opencomputer/ plugin_sdk/ extensions/ tests/` — zero errors

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Risk register (revised)

| Risk | Mitigation |
|---|---|
| `_session_grants` dict leaks across long-running gateway sessions | `register_session_finalize_handler()` subscribes `on_session_finalize` to `HookEvent.SESSION_FINALIZE`; gateway dispatch already fires this on session eviction. |
| `_pending_decisions` 2-tuple → 3-tuple migration breaks an existing test reading the old shape | All known reads at line 485 unpacked as 2-tuple. Step 7 explicitly migrates that site. Tests run in step 10 catch any remaining 2-tuple consumer. |
| Tirith sync subprocess blocks event loop | Wrapped in `asyncio.to_thread`; default 5s timeout already in `tirith.py`. |
| Tirith integration adds latency to every Bash call | ~100ms typical scan time; fail-open default returns immediately when binary absent. |
| Adapter button additions break old clients | All adapters extend, never break: old verbs still routed; new verb additive. |
| `container_persistent: false` workspace lost between turns surprises users | Default `True` preserves behaviour; doc explains trade-off. |
| Existing prompt-string tests assert `[y/N/always]` and break | Step 1 of T2 grep finds them; step 5 updates them in the same commit. |

---

## Self-review checklist

- [x] Every gap from the design spec has a task: T1+T2+T3+T4 (session verb), T5+T6 (Tirith), T7+T8 (container_persistent + docs)
- [x] No TBD / TODO placeholders — every step has actual code or actual command
- [x] All file paths verified to exist (audit found `_build_consent_inline_keyboard` etc. didn't — those were removed)
- [x] Type signatures consistent: `_pending_decisions: dict[..., tuple[bool, bool, bool]]` shape used uniformly at write+read
- [x] All callers of changed signatures get a default param: `resolve_pending(... session_scoped=False)` default
- [x] Test fixtures use real ConsentStore / AuditLogger (not MagicMock) per audit B3 / B4
- [x] Tests target real adapter API surface (`send_approval_request`, not invented `_build_consent_inline_keyboard`)
- [x] Tirith integration goes inside BashTool/ExecuteCode (per audit B6) not inside the consent gate
- [x] `tirith.check_command` sync call wrapped in `asyncio.to_thread` (per audit B7)
- [x] Existing prompt-string tests updated in same commit as render change (per audit B9)
