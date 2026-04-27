# TUI Phase 2 + Phase 3 — Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans to implement Phase 2.A in this session. Subsequent sub-phases ship in their own PRs per the user's `feedback_phase_workflow.md` rule.

**Goal:** Ship the full Claude-Code-parity slash-command + status-bar surface across 11 sub-phases, one PR per sub-phase. Phase 2.A is detailed in TDD-bite-sized form; Phase 2.B → 3.F are design-level outlines that each become their own plan doc when scheduled.

**Architecture:** Each sub-phase extends the `opencomputer/cli_ui/` package (slash registry + handlers) and `opencomputer/agent/` (loop hooks for steer/queue/branch). All slash commands route through `dispatch_slash(text, ctx)` from PR #180. Mid-session state changes (resume, branch, rewind) use callback fields on `SlashContext` so the chat loop owns the actual mutation.

**Tech Stack:** Python 3.12+, prompt_toolkit, Rich, existing SessionDB/AgentLoop/title_generator/CompactionEngine, OS-native CLIs for clipboard (already in `cli_ui/clipboard.py`).

---

# Phase Map

| Phase | What | Hours | Scheduled |
|---|---|---|---|
| **2.A** | `/rename`, `/resume` | ~4 | This PR |
| **2.B** | Cheap wrappers: `/skills`, `/plugins`, `/tools`, `/mcp`, `/agents`, `/doctor`, `/whoami`, `/release-notes`, `/feedback`, `/copy`, `/save` | ~3 | Next PR |
| **2.C** | Bottom status bar (model · ctx % · cost · cwd · git · mode) | ~5 | Next |
| **2.D** | `@`-file + `/`-slash autocomplete | ~4 | Next |
| **2.E** | `/think`, `/verbose`, `/effort` (provider-dependent) | ~4 | Next |
| **2.F** | `/compact`, `/recap` | ~3 | Next |
| **3.A** | Skin/theme engine + `/theme` | ~4 | Next |
| **3.B** | Modal-stack ConditionalContainer (consent, approval, clarify) | ~6 | Next |
| **3.C** | `/steer`, `/queue`, `/btw` mid-run injection | ~6 | Next |
| **3.D** | `/branch`, `/rewind`, `/undo` | ~6 | Next |
| **3.E** | OSC52 + paste-collapse | ~3 | Next |
| **3.F** | Worktree integration | ~3 | Next |
| 3.G | Voice mode | ~12 | **DEFERRED** — out of scope |

**Total active scope:** 51 hours across 12 PRs. Voice deferred.

---

# Phase 2.A — `/rename` + `/resume` (this PR)

**Goal:** Two slash commands. `/rename <new-title>` updates the current session's title in SessionDB. `/resume` opens an interactive picker; `/resume last` jumps to the most-recent prior session; `/resume <id-prefix>` jumps to a specific id.

**Architecture:**
- New handlers `_handle_rename`, `_handle_resume` in `slash_handlers.py`
- New entries `rename`, `resume` in `SLASH_REGISTRY` in `slash.py`
- Two new callbacks on `SlashContext`: `on_rename(title) -> bool`, `on_resume(target) -> bool`. The chat loop wires them up so it can mutate `session_id` (it currently lives as a `nonlocal` in `_run_chat_session`).
- Reuse existing `SessionDB.set_session_title(sid, title)` and `_resolve_resume_target(spec)` from `cli.py:469`.

**File deltas:**
- Modify: `opencomputer/cli_ui/slash.py` — add 2 CommandDefs to the registry
- Modify: `opencomputer/cli_ui/slash_handlers.py` — add 2 handlers + 2 callbacks on `SlashContext`
- Modify: `opencomputer/cli.py` — wire `on_rename` and `on_resume` callbacks into the chat loop
- Test: `tests/test_cli_ui_session_slash.py` (new)

## Tasks

### Task 1: Extend `SlashContext` with `on_rename` + `on_resume`

**Files:** `opencomputer/cli_ui/slash_handlers.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_cli_ui_session_slash.py`:

```python
"""Tests for /rename and /resume slash commands."""
from __future__ import annotations

from unittest.mock import MagicMock

from rich.console import Console

from opencomputer.cli_ui.slash import SLASH_REGISTRY, resolve_command
from opencomputer.cli_ui.slash_handlers import SlashContext, dispatch_slash


def _make_ctx(
    console=None,
    on_rename=None,
    on_resume=None,
):
    return SlashContext(
        console=console or Console(record=True),
        session_id="sess-123",
        config=MagicMock(model=MagicMock(model="m", provider="p")),
        on_clear=lambda: None,
        get_cost_summary=lambda: {"in": 0, "out": 0},
        get_session_list=lambda: [],
        on_rename=on_rename or (lambda title: True),
        on_resume=on_resume or (lambda target: True),
    )


def test_registry_has_rename_and_resume():
    names = {cmd.name for cmd in SLASH_REGISTRY}
    assert "rename" in names
    assert "resume" in names


def test_resolve_rename_aliases():
    cmd = resolve_command("rename")
    assert cmd is not None
    cmd2 = resolve_command("title")  # alias
    assert cmd2 is not None and cmd2.name == "rename"


def test_resolve_resume_aliases():
    cmd = resolve_command("resume")
    assert cmd is not None


def test_dispatch_rename_calls_callback():
    captured: list[str] = []
    ctx = _make_ctx(on_rename=lambda title: captured.append(title) or True)
    r = dispatch_slash("/rename my project debug", ctx)
    assert r.handled is True
    assert captured == ["my project debug"]


def test_dispatch_rename_empty_title_errors():
    """`/rename` with no args prints an error rather than calling the callback."""
    captured: list[str] = []
    console = Console(record=True)
    ctx = _make_ctx(console=console, on_rename=lambda t: captured.append(t) or True)
    r = dispatch_slash("/rename", ctx)
    assert r.handled is True
    assert captured == []
    assert "title" in console.export_text().lower()


def test_dispatch_resume_no_args_means_pick():
    captured: list[str] = []
    ctx = _make_ctx(on_resume=lambda target: captured.append(target) or True)
    r = dispatch_slash("/resume", ctx)
    assert r.handled is True
    assert captured == ["pick"]


def test_dispatch_resume_with_target():
    captured: list[str] = []
    ctx = _make_ctx(on_resume=lambda target: captured.append(target) or True)
    r = dispatch_slash("/resume last", ctx)
    assert r.handled is True
    assert captured == ["last"]


def test_dispatch_resume_with_id_prefix():
    captured: list[str] = []
    ctx = _make_ctx(on_resume=lambda target: captured.append(target) or True)
    r = dispatch_slash("/resume abc123", ctx)
    assert r.handled is True
    assert captured == ["abc123"]


def test_dispatch_rename_callback_failure_prints_error():
    """If on_rename returns False, the handler reports the failure."""
    console = Console(record=True)
    ctx = _make_ctx(console=console, on_rename=lambda t: False)
    r = dispatch_slash("/rename foo", ctx)
    assert r.handled is True
    assert (
        "fail" in console.export_text().lower()
        or "could not" in console.export_text().lower()
    )
```

- [ ] **Step 2: Run failing**

`pytest tests/test_cli_ui_session_slash.py -v` → all FAIL (registry doesn't have `rename`/`resume`; SlashContext lacks callbacks).

- [ ] **Step 3: Add the two CommandDefs**

Edit `opencomputer/cli_ui/slash.py`. Append to `SLASH_REGISTRY`:

```python
    CommandDef(
        name="rename",
        description="Set a friendly title for the current session.",
        category="session",
        aliases=("title",),
        args_hint="<new title>",
    ),
    CommandDef(
        name="resume",
        description="Switch to a prior session (interactive picker by default).",
        category="session",
        args_hint="[last|<session-id-prefix>]",
    ),
```

- [ ] **Step 4: Extend `SlashContext` and add handlers**

Edit `opencomputer/cli_ui/slash_handlers.py`. Add fields to the dataclass:

```python
@dataclass
class SlashContext:
    console: Console
    session_id: str
    config: Any
    on_clear: Callable[[], None]
    get_cost_summary: Callable[[], dict[str, int]]
    get_session_list: Callable[[], list[dict[str, Any]]]
    on_rename: Callable[[str], bool] = lambda title: False
    on_resume: Callable[[str], bool] = lambda target: False
```

Add two handlers above `_HANDLERS`:

```python
def _handle_rename(ctx: SlashContext, args: list[str]) -> SlashResult:
    title = " ".join(args).strip()
    if not title:
        ctx.console.print(
            "[red]/rename needs a title[/red] — e.g. `/rename my-debug-session`"
        )
        return SlashResult(handled=True)
    ok = ctx.on_rename(title)
    if ok:
        ctx.console.print(f"[green]session renamed →[/green] {title}")
    else:
        ctx.console.print(
            "[red]rename failed[/red] (no current session?)"
        )
    return SlashResult(handled=True)


def _handle_resume(ctx: SlashContext, args: list[str]) -> SlashResult:
    target = (args[0] if args else "pick").strip()
    ok = ctx.on_resume(target)
    if not ok:
        ctx.console.print(
            "[red]resume failed[/red] — target not found or no prior sessions"
        )
    return SlashResult(handled=True)
```

Wire into `_HANDLERS`:

```python
    "rename": _handle_rename,
    "resume": _handle_resume,
```

- [ ] **Step 5: Run tests → pass**

`pytest tests/test_cli_ui_session_slash.py -v` → all 9 PASS.

- [ ] **Step 6: Commit**

```bash
git add opencomputer/cli_ui/slash.py opencomputer/cli_ui/slash_handlers.py tests/test_cli_ui_session_slash.py
git commit -m "feat(cli-ui): /rename + /resume registry entries + handlers"
```

---

### Task 2: Wire callbacks in the chat loop

**Files:** `opencomputer/cli.py`

- [ ] **Step 1: Read the current `SlashContext` build site**

`grep -n "SlashContext(" opencomputer/cli.py` — find the construction in `_run_chat_session`.

- [ ] **Step 2: Add `_on_rename` and `_on_resume` closures**

In `_run_chat_session`, near where `_on_clear` is defined, add:

```python
    def _on_rename(title: str) -> bool:
        """Update the current session's stored title in SessionDB."""
        try:
            from opencomputer.agent.state import SessionDB
            db = SessionDB(profile_home / "sessions.db")
            db.set_session_title(session_id, title)
            return True
        except Exception as e:  # noqa: BLE001
            _log.warning("rename failed: %s", e)
            return False

    def _on_resume(target: str) -> bool:
        """Switch the active session to the resolved target.

        ``target`` is one of ``"pick"``, ``"last"``, or a session id
        (full or unique prefix). Resolution reuses the helper from CLI
        startup. On success: rebinds ``session_id`` (nonlocal), resets
        the token tally, prints a banner showing the title if any.
        Returns False on no-match / ambiguous prefix; the handler then
        reports the failure to the user.

        Audit refinements (vs first draft):
        - Short-circuit when resolved == current ``session_id`` (no-op +
          friendly message instead of noisy reset).
        - Ambiguous id-prefix lists matches so the user can disambiguate.
        - Post-resume banner shows the session title.
        """
        nonlocal session_id
        try:
            from opencomputer.agent.state import SessionDB
            db = SessionDB(profile_home / "sessions.db")
            if target in ("pick", "last"):
                resolved = _resolve_resume_target(target)
            else:
                rows = db.list_sessions(limit=200)
                matches = [r["id"] for r in rows if r["id"].startswith(target)]
                if len(matches) > 1:
                    console.print(
                        f"[yellow]ambiguous prefix[/yellow] {target!r} matches "
                        f"{len(matches)} sessions:"
                    )
                    for mid in matches[:10]:
                        title = db.get_session_title(mid) or "(untitled)"
                        console.print(f"  [dim]{mid[:8]}[/dim]  {title}")
                    return False
                resolved = matches[0] if matches else None
            if not resolved:
                return False
            if resolved == session_id:
                console.print(
                    "[dim]already on this session — nothing to resume.[/dim]"
                )
                return True
            session_id = resolved
            _token_tally["in"] = 0
            _token_tally["out"] = 0
            title = db.get_session_title(session_id) or "(untitled)"
            console.print(
                f"[bold cyan]resumed →[/bold cyan] {session_id[:8]} "
                f"[dim]({title})[/dim]"
            )
            return True
        except Exception as e:  # noqa: BLE001
            _log.warning("resume failed: %s", e)
            return False
```

- [ ] **Step 3: Pass callbacks into `SlashContext(...)`**

Update the `SlashContext(...)` construction:

```python
            slash_ctx = SlashContext(
                console=console,
                session_id=session_id,
                config=cfg,
                on_clear=_on_clear,
                get_cost_summary=_get_cost_summary,
                get_session_list=_get_session_list,
                on_rename=_on_rename,
                on_resume=_on_resume,
            )
```

- [ ] **Step 4: Verify import + smoke**

`python -c "import opencomputer.cli; print('ok')"` → no errors.

Then manual smoke (in real terminal):
```
opencomputer chat
> /rename my-test
> /resume last
> /resume pick
> /exit
```

- [ ] **Step 5: Commit**

```bash
git add opencomputer/cli.py
git commit -m "feat(cli): wire /rename + /resume callbacks into chat loop"
```

---

### Task 3: CHANGELOG + push + PR

- [ ] Append to CHANGELOG `## [Unreleased]`:

```markdown
### Added (TUI Phase 2.A — session management slash commands)

- **`/rename <title>`** (alias `/title`) — set a friendly title for the
  current session. Persists via `SessionDB.set_session_title`. Combined
  with the existing background auto-titler (Tier S port), users can
  always re-title manually if the auto-pick is off.
- **`/resume [last|<id-prefix>|pick]`** — switch the active session
  mid-chat without exiting. Bare `/resume` opens the picker; `/resume
  last` jumps to the most recent prior session; `/resume abc1` matches
  on session-id prefix (single match required).
- New callbacks on `SlashContext`: `on_rename(title) -> bool`,
  `on_resume(target) -> bool`. Wired to closures in `_run_chat_session`
  that mutate `nonlocal session_id` and reset the cumulative token tally.
```

- [ ] Run full suite + lint
- [ ] `git push -u origin feat/tui-phase2a-session-mgmt`
- [ ] `gh pr create` with summary

---

# Phase 2.B — Cheap wrappers (next PR sketch)

**Goal:** 11 thin slash commands — most are 5-line invocations of existing CLI subcommands.

**Slash commands to add:**
| Cmd | Behavior | Backed by |
|---|---|---|
| `/skills` | List installed skills | `cli_skills.py` |
| `/plugins` | List loaded plugins | `cli_plugin.py` |
| `/tools` | Show registered tool names | `registry.names()` |
| `/mcp` | MCP server status | `cli_mcp.py` |
| `/agents` | Registered agent templates | `_discover_and_register_agents` |
| `/doctor` | Health check | `doctor.py` |
| `/whoami` | Print user identity | profile config |
| `/release-notes` | Show CHANGELOG.md | pager |
| `/feedback` | Open feedback URL or save text | new |
| `/copy [N]` | Copy Nth assistant response to clipboard | OSC52 + pyperclip fallback |
| `/save [path]` | Save full transcript (md) | `console.save_text` |

Each handler is ≤15 lines. Most reuse existing functions verbatim.

---

# Phase 2.C — Bottom status bar (next PR sketch)

**Goal:** prompt_toolkit `bottom_toolbar` showing live model · context % · cost · mode · cwd · git branch · bg-task count.

**Implementation:** Per hermes' pattern — a daemon thread runs `_invalidate(min_interval)` on the prompt session, snapshot helpers (`_get_status_bar_snapshot`) read the agent loop's running state, fragment builder emits styled content. Three width tiers (52/76/full) for graceful degradation. Critical: `wrap_lines=False` to prevent ghost rows; pin prompt_toolkit minor version because hermes' `_on_resize` patch uses private API.

---

# Phase 2.D — Autocomplete (next PR sketch)

**Goal:** `Tab` after `/` shows slash-command menu with descriptions; after `@` shows `git ls-files` output (fuzzy-matched).

**Implementation:** `merge_completers([SlashCommandCompleter(SLASH_REGISTRY), LocalFileMentionCompleter])` — both implement `prompt_toolkit.completion.Completer`. SlashCommandCompleter wraps a `WordCompleter` in a `FuzzyCompleter`. LocalFileMentionCompleter caches git ls-files output, invalidates on `.git/index` mtime change, 2s refresh interval.

---

# Phase 2.E — Reasoning controls (next PR sketch)

**Goal:** `/think`, `/verbose`, `/effort` slash commands.

**Implementation:** Provider-dependent. Anthropic supports extended-thinking with budget tokens; OpenAI o1/o3 has reasoning effort. Each provider exposes a `set_thinking_level(level)` method on `BaseProvider`; the slash handler invokes it. `/effort` would be an interactive slider (prompt_toolkit Application — substantial UI work, optional).

---

# Phase 2.F — Compaction (next PR sketch)

**Goal:** `/compact` manually triggers `CompactionEngine`, `/recap` generates a session summary on resume.

**Implementation:** `CompactionEngine` already exists in `agent/compaction.py`. `/compact` adds a CLI surface to `engine.maybe_compact()`. `/recap` uses the cheap-route (`should_route_cheap`) + a summarization prompt. Print before the user prompt re-appears.

---

# Phase 3.A — Skin engine (next PR sketch)

**Goal:** Port `hermes_cli/skin_engine.py` (reduced) — 1 default + 2-3 starter themes (mono, daylight, ares).

**Implementation:** ~3-4h. YAML schema, `SkinConfig` dataclass, `get_active_skin`, `set_active_skin`, `get_prompt_toolkit_style_overrides()`. Wire into `Console` via `Theme` and into `Application.style`. `/theme <name>` swaps live.

---

# Phase 3.B — Modal-stack ConditionalContainer (next PR sketch)

**Goal:** Switch consent / approval / clarify prompts from print-and-wait to inline-modal overlays.

**Implementation:** Adopt the hermes `RunningPromptDelegate` pattern. Each modal is a `ConditionalContainer(Window(FormattedTextControl(...)))` filtered on a state dict. Worker threads put requests on a `response_queue` and block on `.get()` while the UI thread renders the modal.

---

# Phase 3.C — Steer + Queue + BTW (next PR sketch)

**Goal:** Mid-run guidance (`/steer`), next-turn defer (`/queue`), side-question (`/btw`).

**Implementation:** Requires changes to `AgentLoop`:
- `agent.steer(text)` — appends to `_pending_steer` (lock-protected). Polled at safe boundaries; injected into next tool-result with marker.
- `/queue` puts on `_pending_input` queue (already separate from `_interrupt_queue`).
- `/btw <q>` runs a side-conversation through the cheap-route LLM, displayed as a dismissable inline pane, NOT written to transcript.

---

# Phase 3.D — Branch + Rewind (next PR sketch)

**Goal:** `/branch [<name>]` forks the current session at the current turn; `/rewind [N]` removes the last N turns from history.

**Implementation:** `cli_session.session_fork` already exists for the CLI surface. Slash command wraps it. `/rewind` uses `SessionDB` to delete the last N message rows + bump cost back. Both prompt for confirmation.

---

# Phase 3.E — OSC52 + paste-collapse (next PR sketch)

**Goal:** OSC52 clipboard write (SSH/tmux); pastes >5 lines collapse into a placeholder.

**Implementation:** Direct port of `cli.py:3946 _write_osc52_clipboard` from hermes. Paste-collapse: extend our BracketedPaste handler to write long pastes to `<profile>/pastes/paste_N.txt` and insert `[Pasted text #N: K lines → /path]` into the prompt buffer. Expand placeholders at submit time.

---

# Phase 3.F — Worktree integration (next PR sketch)

**Goal:** Status bar shows worktree state; `/worktree` slash command for list/switch.

**Implementation:** Read `git worktree list` output; cache for the status-bar refresh. `/worktree` opens a picker. Branch indicator updates live.

---

## Self-Review Checklist (Pre-execution — Phase 2.A only)

1. **Spec coverage** — `/rename` and `/resume` both have task entries with full code? ✓
2. **Placeholder scan** — no TBD/TODO in Phase 2.A. (Later phases are sketches by design.) ✓
3. **Type consistency** — `SlashContext.on_rename: Callable[[str], bool]`, `on_resume: Callable[[str], bool]` — used consistently. ✓
4. **Concurrency / mutation** — `_on_resume` mutates `nonlocal session_id`. Verified `_run_chat_session` is single-threaded inside one `asyncio.run`. ✓
5. **Failure modes** — `set_session_title` failure (no row matching session_id) returns False; handler prints "rename failed". `_resolve_resume_target` returns None on no-match → handler prints "resume failed". ✓
6. **CLI vs slash overlap** — `--resume` flag at startup is unaffected; `/resume` only operates mid-session. ✓
