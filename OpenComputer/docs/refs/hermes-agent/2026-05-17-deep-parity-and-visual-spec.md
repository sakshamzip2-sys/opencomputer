# Hermes vs OpenComputer — Deep Parity + Visual Contract (verified)

**Date:** 2026-05-17 — **verified rewrite.**
**Ground truth:** OC `origin/main` at `908483a0`, plus the
`hermes-parity-residual-2026-05-17` branch. Hermes at
`sources/hermes-agent/` (v0.12.0 snapshot, 2026-04-23).
**Companion to:** `docs/refs/hermes-agent/2026-04-28-major-gaps.md`,
`docs/refs/hermes-agent/inventory.md`.

> **Status.** This file replaces an earlier same-day draft that was an
> *unverified* audit. That draft's "Top-5 port recipes" each described a
> subsystem that **already shipped** — four of its five claimed gaps were
> false negatives. The draft compared Hermes against `cli_banner.py`'s six
> local hex constants and concluded "OC has 7 colour tokens", never
> opening `opencomputer/cli_ui/skin/` (a complete skin engine sitting one
> directory over), and it scanned only one of OC's two slash registries.
> Every claim below is checked against the cited file. If a path doesn't
> say what is claimed here, this doc is wrong — fix it.

---

## TL;DR

**The visual-contract gap is closed.** OC already shipped the skin
engine, the colour-token palette, the spinner, and the status bar. The
one true residual — the welcome banner ignoring the active skin — was
closed on 2026-05-17.

**The slash-command gap was overstated.** OC has *two* slash registries
(~40 commands each). Nearly every command the earlier draft called
"missing" exists. The genuine absentees were `/paste` and `/undo`; both
shipped on 2026-05-17.

What is genuinely still open is small and listed honestly in §4.

---

## 1. Visual contract — verified state

| Subsystem | Earlier draft claimed | Verified reality |
|---|---|---|
| Colour-token registry | "OC ships 7 tokens; 22 missing" | **Shipped.** `cli_ui/skin/builtins/default.yaml` ships a full 24-key palette (banner, UI semantics, prompt/input, response box, session label/border, status bar, voice badge, selection, completion-menu panes). `cli_ui/skin/spec.py:SkinSpec.colors` is the registry. |
| Skin engine | "entirely missing in OC" | **Shipped.** `cli_ui/skin/` = `loader.py` + `apply.py` + `spec.py`, **9 built-in skins** (`default`, `mono`, `daylight`, `ares`, `charizard`, `poseidon`, `sisyphus`, `slate`, `warm-lightmode`). Live runtime swap via `apply_skin()`; `/skin <name>` is wired in `agent/slash_commands_impl/skin_personality_cmd.py`. |
| Spinner / KawaiiSpinner | "entirely missing; OC uses rich's default" | **Shipped.** `cli_ui/busy_indicator.py` has 5 named styles (`kawaii`, `minimal`, `dots`, `wings`, `none`) with width-uniform frames; `SkinSpec` carries `spinner_waiting_faces` / `spinner_thinking_faces` / `spinner_thinking_verbs` / `spinner_wings`, all populated per skin. The `default` skin's pools were widened to ~10 faces / 12 verbs on 2026-05-18 (§3). |
| Status bar | "entirely missing in OC" | **Shipped (PR #418).** `cli_ui/status_line.py` renders `◆ model · ctx 12.4K/200K ██████░░░░ 6% · $0.06 · 15m` and grades the context badge green → yellow → orange → red (`status_line.py:163-180`). A `/statusbar` toggle exists (`agent/slash_commands_impl/display_toggles_cmd.py:StatusbarCommand`). |
| Banner honours the skin | (proposed as Recipe 1's kernel) | **Closed 2026-05-17.** `cli_banner.py` previously hardcoded six hex constants. `cli_banner._palette()` now resolves the palette from the active skin's `banner_*` block; the `default` skin (and no-skin) keep OC pink, byte-identical to the historical splash. |

**Net:** the only genuine visual-contract residual was the banner, now
closed. Nothing else here is worth porting.

---

## 2. Slash commands — the two-registry reality

OC has **two** slash-command systems. The earlier draft found only the
first and concluded 27 commands were missing.

1. **`cli_ui/slash.py`** — `SLASH_REGISTRY`, a list of `CommandDef`s,
   handlers in `cli_ui/slash_handlers.py` (`_handle_*` + the `_HANDLERS`
   dict). This is the registry the **`oc chat` REPL** dispatches
   (`cli.py` → `dispatch_slash`).
2. **`agent/slash_commands_impl/`** — ~40 `SlashCommand` subclasses
   registered into `plugins/registry.slash_commands` by
   `agent/slash_commands.py::register_builtin_slash_commands`. This is
   the registry **gateway / wire / ACP** dispatch (and `AgentLoop`'s
   in-loop slash dispatch, `loop.py:1939`) reads.

`/reasoning` and `/sources` appear in *both* registries — the cli_ui
handler bridges to the agent command via an `on_*_dispatch` callback.

**Reachability — closed 2026-05-17 (see §3).** The `oc chat` REPL
historically dispatched *only* the cli_ui registry, so a command living
only in `agent/slash_commands_impl/` (`/copy`, `/rollback`,
`/background`, `/agents`, …) produced "unknown command" in chat. The
REPL now routes a slash it doesn't recognise to the agent registry via
`try_dispatch_agent_slash`, so every agent command is reachable from
`oc chat` as well as gateway / wire / ACP.

---

## 3. What the 2026-05-17 `hermes-parity-residual` branch shipped

| Change | Files | Notes |
|---|---|---|
| `/paste` — attach a clipboard image | `cli_ui/slash.py`, `cli_ui/slash_handlers.py` | Wraps the existing cross-platform engine `cli_ui/clipboard.py` (`has_clipboard_image` / `save_clipboard_image`); queues the PNG via the `on_image_attach` callback `/image` already uses. cli_ui registry — needs `SlashContext`. |
| `/undo` — remove the last user/assistant exchange | `agent/slash_commands_impl/undo_cmd.py` (new), `agent/slash_commands.py`, `cli_ui/slash.py`, `cli_ui/slash_handlers.py`, `cli.py` | Hermes-parity conversation-history op (distinct from `/rollback`, which restores filesystem checkpoints). `undo_last_exchange()` truncates the session at the last `role=="user"` message via `SessionDB.replace_session_messages`, removing the whole exchange (prompt + reply + tool messages) atomically. Reachable everywhere: agent `UndoCommand` for gateway/wire/ACP, plus a cli_ui bridge for `oc chat`. |
| Banner honours the active skin | `cli_banner.py` | See §1, last row. |
| Agent slash commands reachable from `oc chat` | `cli_ui/slash_handlers.py`, `agent/slash_commands.py`, `cli.py` | `dispatch_slash` gained an `on_unknown` hook; the REPL wires it to `try_dispatch_agent_slash`, which dispatches a slash absent from the cli_ui registry through the agent `SlashCommand` registry. `/copy`, `/rollback`, `/background`, `/agents` and ~35 other agent commands now work in `oc chat`, not only gateway/wire/ACP. Dispatched directly via the slash dispatcher — no persist / end-session side effect. |
| Spinner face/verb variety | `cli_ui/skin/builtins/default.yaml` | The `default` skin's spinner pools were 3-4 entries each; widened to ~10 waiting faces / ~10 thinking faces / 12 verbs / 4 wings so the spinner feels varied across a long session. Pinned by `test_default_skin_ships_a_rich_face_pool`. |

All five are TDD-covered: `tests/test_slash_paste.py`,
`tests/test_slash_undo.py`, `tests/test_banner_skin.py`,
`tests/test_slash_agent_fallthrough.py`,
`tests/test_skin_spinner_faces.py`.

---

## 4. Genuine remaining gaps (honest, short)

- **Web dashboard polish.** OC's `opencomputer/dashboard/` is slimmer
  than Hermes's React app — no i18n, no theme system, no plugin-tabs.
  Genuine M-L effort; belongs in its own spec.

(The non-`default` built-in skins keep their own smaller spinner pools —
opt-in skins, lower priority; widen them the same way if wanted.)

Deliberate non-goals: the commercial memory backends (`holographic` /
`retaindb` / `supermemory` — SaaS) and `bluebubbles` / `zalo` channels.
Native Daytona/Modal sandbox backends already landed in PR #637.

---

## 5. What OC has that Hermes does not — keep perspective

| OC capability | Hermes equivalent |
|---|---|
| Layered Awareness L0–L4 (passive education, life-event detection, ambient sensor) | none |
| Plural personas + vibe classifier + companion voice | flat personality list |
| Gateway-vs-CLI parity probe (`gateway/parity_probe.py`) | none |
| F1 consent gate with HMAC audit chain (`security/consent.py`) | flat approval prompt |
| Profile handoff — 8-subsystem rebind on profile change | restart-only |
| Auto-skill-evolution quarantine → approve loop | none |
| macOS desktop control (`extensions/computer-use/`) | basic only |
| Pink branding + braille laurels + responsive 3-tier banner | gold + caduceus + 2-tier |

OC is structurally ahead on awareness, security, multi-surface
orchestration, and OS integration. Hermes is ahead on dashboard breadth.
The CLI-ergonomics gap the earlier draft worried about was mostly
imaginary.

---

## Files cited (verify any claim)

| Claim | File |
|---|---|
| Skin engine | `opencomputer/cli_ui/skin/{loader,apply,spec}.py` + `builtins/*.yaml` |
| 24-key colour palette | `opencomputer/cli_ui/skin/builtins/default.yaml` |
| Spinner styles | `opencomputer/cli_ui/busy_indicator.py` |
| Status bar | `opencomputer/cli_ui/status_line.py` |
| Banner + skin resolution | `opencomputer/cli_banner.py` (`_palette`) |
| cli_ui slash registry | `opencomputer/cli_ui/slash.py`, `opencomputer/cli_ui/slash_handlers.py` |
| Agent slash registry | `opencomputer/agent/slash_commands.py`, `opencomputer/agent/slash_commands_impl/` |
| `oc chat` slash routing | `opencomputer/cli.py` (`_is_slash_input` → `dispatch_slash`) |
| `/undo` logic | `opencomputer/agent/slash_commands_impl/undo_cmd.py` |

---

## Appendix — how the earlier draft went wrong

The 2026-05-17 draft was generated without opening the files it cited.
Two specific failures, recorded so the pattern is not repeated:

1. It compared Hermes's skin palette against the six literal hex
   constants in `cli_banner.py` and concluded OC had no token registry —
   never opening `cli_ui/skin/`, a 9-skin engine with a 24-key palette.
2. It scanned `cli_ui/slash.py` (38 entries) for slash commands and
   declared 27 missing — never finding `agent/slash_commands_impl/`,
   which holds ~40 more.

Lesson: a parity audit must read each cited file before making the
claim. A file path in an audit is a promise; an unverified promise is a
landmine for the next reader.
