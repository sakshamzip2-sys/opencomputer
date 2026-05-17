# Multi-Surface Life-Event Teeth — Implementation Spec

**Date:** 2026-05-17
**Branch:** `feat/multi-surface-life-events-2026-05-17`
**Tier:** 3 (multi-surface, all dispatch paths) — pre-mortem approved 2026-05-17.
**Predecessor:** PR #630 shipped life-event "teeth" CLI-only.

## 1. Problem

PR #630 shipped life-event "teeth" — `LifeEventInjectionProvider` injects a
`<life-event-hint>` block + tone directive when a life-event pattern fires, and
schedules a one-shot "gentle check-in" cron. It is registered **only** on the
CLI chat surface (`opencomputer/cli.py::_run_chat_session`). A user who talks to
the agent via Telegram/Discord (gateway), a TUI/IDE (wire), or the web UI gets
no teeth at all.

This spec extends the teeth to the gateway/wire/webui surfaces, behind an
opt-in feature flag, with the concurrency + transaction hardening that
multi-surface (concurrent-loop) use requires.

## 2. Codebase facts established by survey (2026-05-17)

- **No shared injection-registration helper.** CLI, gateway, wire, webui each
  build their `AgentLoop` independently; only CLI registers the 4 built-in
  injection providers (`cli.py:1753-1830`).
- **Each OS process is a single surface.** `oc chat`, `oc gateway`, `oc wire`,
  `oc webui` are separate processes. The `InjectionEngine` is a process-global
  singleton — so the provider can be told its surface **once**, at registration,
  via a constructor argument. No `plugin_sdk` / `InjectionContext` change needed.
- **`RequestContext` (channel/user_id) is set only on the gateway dispatch
  path** via `PluginAPI.in_request()` (`gateway/dispatch.py:1356`). Wire and
  webui call `run_conversation` without an `in_request` wrapper. So
  origin-targeted check-in crons are a **gateway-only** capability; wire/webui
  get hint injection with un-targeted (in-band) check-ins. This is intentional:
  proactive push to an ephemeral wire socket / stateless webui request is not
  meaningful — adding `in_request` wrapping to those surfaces is a cross-cutting
  change affecting all plugins and is explicitly out of scope.
- **`life_event_state.json` mutators are single-writer-unsafe** — `state.py:106-110`
  explicitly documents that concurrent writers (gateway + CLI at once) need a
  file lock. `save_state` is atomic (`os.replace`) so *readers* never tear; only
  the read-modify-write mutators need locking.
- **`schedule_followup` has a transaction gap** — `actions.py:151-162` does
  `create_job` then `mark_surfaced`; a `mark_surfaced` failure leaves an orphan
  cron that the dedup check (keyed on a recorded `cron_id`) cannot see, so the
  next turn schedules a *second* cron.

## 3. Design decisions

- **D1 — Surface-aware provider.** `LifeEventInjectionProvider.__init__` takes
  `surface: str = "cli"`. `collect()` gates on it: `surface == "cli"` → always
  active (preserves #630, unflagged — CLI teeth must not regress); any other
  surface → active only when the `multi_surface_life_events` flag is True for
  the current profile.
- **D2 — Drain-then-gate.** `collect()` drains the registry queue *first*, then
  applies the gate. Draining-and-discarding on a flag-off non-CLI surface keeps
  the process-global registry queue bounded (no leak on a long-running flag-off
  gateway daemon).
- **D3 — Origin from `RequestContext`.** `collect()` builds the
  `schedule_followup` `origin` mapping from the active `PluginAPI.request_context`
  when resolvable (gateway). CLI/wire/webui have no `request_context` →
  `origin=None` (unchanged, graceful — cron still created, just un-targeted).
  All resolution is fail-safe (`try/except → None`).
- **D4 — Feature flag.** `life_events.multi_surface_life_events`, default
  `False`, in `<profile>/feature_flags.json` — same dotted-key pattern as
  `policy_engine.*` / `data_retention.*`.
- **D5 — State concurrency.** A generic `file_lock(lock_path)` context manager
  is extracted from `profiles_lock.py`; `state.py`'s three mutators wrap their
  read-modify-write in `file_lock(<home>/.life_event_state.lock)`. Reads stay
  lock-free.
- **D6 — Transaction integrity.** `schedule_followup` compensates a
  post-`create_job` `mark_surfaced` failure with `remove_job(cron_id)` — no
  orphan cron, clean re-fire next turn.
- **D7 — Out of scope (surfaced, not fixed).** The same non-CLI injection gap
  affects `ThinkingInjector` / `PathGlobRulesProvider` / `HandoffInjectionProvider`
  (`HandoffInjectionProvider`'s "Applies to ALL surfaces" comment at
  `cli.py:1778-1782` is **false**). Separate concern — surfaced in the PR body,
  not fixed here. The hook engine's `fire_and_forget` dropping `HookSpec.timeout_ms`
  (`hooks/engine.py:196-218`) is likewise a separate PR.

## 4. Tasks (TDD; subagent-driven-development; one commit per task)

### Task 1 — `multi_surface_life_events` feature flag
- **File:** `opencomputer/agent/feature_flags.py`
- Add `DEFAULT_LIFE_EVENTS: dict[str, Any] = {"multi_surface_life_events": False}`
  after `DEFAULT_DATA_RETENTION`.
- In `read()`, add a `dotted_key.startswith("life_events.")` branch alongside
  the existing `policy_engine.` / `data_retention.` branches, returning
  `DEFAULT_LIFE_EVENTS.get(leaf, default)`.
- Match `data_retention` (top-level, NOT seeded into `read_all()`).
- **Tests:** `tests/test_feature_flags.py` — absent file → `False`; written value
  round-trips; unknown `life_events.*` key → caller default.

### Task 2 — generic `file_lock` + state-mutator locking
- **File A — `opencomputer/profiles_lock.py`:** extract
  `@contextmanager file_lock(lock_path: Path) -> Iterator[None]` holding the
  fcntl/msvcrt dance (mkdir's `lock_path.parent`); refactor `profile_yaml_lock`
  to `with file_lock(profile_dir / ".profile.lock"): yield`. Keep
  `profile_yaml_lock`'s signature unchanged (back-compat).
- **File B — `opencomputer/awareness/life_events/state.py`:** wrap the
  read-modify-write of `mark_surfaced`, `clear`, `clear_verdict_pending` in
  `with file_lock(_home() / ".life_event_state.lock"):`. Update the
  `state.py:106-110` comment (it now *has* a lock). Reads stay lock-free.
- **Tests:** existing `tests/test_profiles_lock.py` stays green; new concurrency
  test — N threads each `mark_surfaced` a distinct pattern, after join
  `load_state()` holds all N. The test MUST fail without the lock (force
  interleaving via a `threading.Barrier` or instrumented `save_state`).

### Task 3 — compensating `remove_job` in `schedule_followup`
- **File:** `opencomputer/awareness/life_events/actions.py`
- Wrap the `state.mark_surfaced(...)` call (`actions.py:162`) in `try/except`:
  on failure, call `remove_job(cron_id)` (best-effort, own `try/except`), log a
  WARNING, and re-raise the original `mark_surfaced` exception.
- **Tests:** `tests/test_life_event_actions.py` — `mark_surfaced` raising →
  `remove_job` called with the created `cron_id` + exception propagates;
  success path → no `remove_job` call.

### Task 4 — surface-aware provider: gating + origin
- **File:** `opencomputer/awareness/life_events/injection.py`
- `LifeEventInjectionProvider.__init__(self, surface: str = "cli")` → store
  `self._surface`.
- Module helper `_multi_surface_enabled() -> bool` — reads
  `FeatureFlags(_home()/"feature_flags.json").read("life_events.multi_surface_life_events", False)`;
  fail-safe (`try/except → False`).
- Module helper `_resolve_origin() -> dict | None` — resolves the active
  `PluginAPI.request_context` (find the accessor in `opencomputer/plugins/loader.py`);
  when it carries `channel` + `user_id` → `{"platform": channel, "chat_id": user_id}`;
  else `None`; fail-safe (`try/except → None`).
- `collect()`: drain FIRST (unchanged); `if not firings: return None`; THEN
  `if self._surface != "cli" and not _multi_surface_enabled(): return None`;
  then dedup; pass `origin=_resolve_origin()` into `schedule_followup` (was
  hardcoded `None`). Update the `collect()` docstring.
- Module helper `register_life_event_injection_provider(surface: str = "cli")` —
  idempotent `engine.unregister("life_event_hint")` + `engine.register(...)`,
  wrapped `try/except` + WARN ("never break loop boot"). Add to `__all__`.
- **Tests:** `tests/test_life_event_injection.py` — `surface="cli"` ≡ #630
  behavior; `surface="gateway"` + flag off → drains then `None`;
  `surface="gateway"` + flag on → hint block; origin built from a mocked
  `request_context`.

### Task 5 — register the provider on every surface
- **Files:** `opencomputer/cli.py` (`_run_chat_session` ~1818-1830 → collapse to
  `register_life_event_injection_provider("cli")`; `wire` command ~4106 → add
  `register_life_event_injection_provider("wire")`), `opencomputer/cli_gateway.py`
  (`_run_foreground`, after the loop is built → `register_life_event_injection_provider("gateway")`),
  `opencomputer/dashboard/routes/openai_compat.py` (`_run_agent_completion`,
  after `build_agent_loop_for_profile` → `register_life_event_injection_provider("webui")`).
- **Tests:** per-surface registration tests — exercise each surface's setup path,
  assert `engine` carries `life_event_hint` and the registered provider's
  `_surface` is correct.

### Task 6 — docs + E2E
- **File A — `docs/awareness/life-events.md`:** add a "Multi-surface" section —
  the flag, what each surface gets (CLI always; gateway/wire/webui flag-gated;
  gateway gets origin-targeted crons, wire/webui get in-band check-ins), how to
  enable. Refresh the v1-limitations section.
- **File B — `tests/test_life_event_teeth_e2e.py`:** add a multi-surface E2E
  test — gateway-surface provider, flag on → firing → hint block + targeted
  `schedule_followup`; flag off → `None`.

## 5. Rollback

Single squash commit → `git revert`. Faster: the `multi_surface_life_events`
flag defaults False — flipping it off restores exactly the #630 (CLI-only)
behavior with no code change.

## 6. Out of scope — surfaced for follow-up

- **R2:** non-CLI injection gap for `ThinkingInjector` / `PathGlobRulesProvider` /
  `HandoffInjectionProvider`; `HandoffInjectionProvider`'s `cli.py:1778-1782`
  "all surfaces" comment is false.
- **R7:** `HookEngine.fire_and_forget` drops `HookSpec.timeout_ms`
  (`hooks/engine.py:196-218`) — separate PR.
