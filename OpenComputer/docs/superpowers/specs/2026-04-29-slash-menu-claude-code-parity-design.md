# Slash menu — full Claude-Code parity (commands + skills + ranking + MRU)

**Goal:** When the user types `/` in OpenComputer's TUI, surface a single unified dropdown showing every slash command AND every installed skill, ranked by match quality and recent use, exactly mirroring Claude Code's autocomplete UX.

**Status:** Design (2026-04-29). Awaiting user spec review before writing the implementation plan.

**Owner:** Saksham (current Claude session). Coordinated with parallel session "archit" — see §10.

---

## 1. The problem

Two user-reported gaps in the OpenComputer TUI today:

1. **Skills are invisible from the dropdown.** `MemoryManager.list_skills()` returns 50+ skills (bundled + user-saved + evolution-staged + hub-installed via PR #220). Zero of them surface when the user types `/`. The picker shows only the 14 hardcoded entries in `SLASH_REGISTRY`. Discovery is broken — the user has to memorize skill IDs to invoke them.
2. **The dropdown feels broken.** Strict `startswith` filter + `[:10]` cap means typing `/re` shows only 4 commands and silently truncates anything that didn't fit. No fuzzy matching, no MRU bias, no source labelling. Compared to Claude Code (which mixes commands + skills, ranks by match quality + recent use, and labels each row's source), the menu reads as incomplete.

Both reduce to: **the dropdown's source data is wrong, its filter is wrong, and its ordering is wrong.**

## 2. Reference behaviour we're matching

Confirmed from the public Claude Code CHANGELOG and reference docs in `sources/claude-code/`:

| Behaviour | Claude Code | OC today | Target |
|---|---|---|---|
| `/` empty prefix shows all items | yes — commands AND skills mixed | only the 14 commands | match Claude Code |
| Skills appear in dropdown by name | yes (CHANGELOG line 1358 confirms) | no | match Claude Code |
| Three-column row: name • source/category • description | yes | yes for commands; n/a for skills | extend to skills |
| Description truncated at ~250 chars | yes (CHANGELOG line 534) | no — full description rendered | match Claude Code |
| Filter is fuzzy / ranked, not strict prefix | yes | no — strict `startswith` | match Claude Code |
| Recently-used items float to top | yes — MRU surface | no | match Claude Code |
| Built-in commands always reachable even with many skills | yes (CHANGELOG line 1358 — bug fixed there) | n/a (no skills surfaced yet) | preserve as we add skills |
| Dispatch path for `/<skill-name>` | tool-result style — skill content lands as a `tool_result` so the model treats it as a tool's output | text-message style — `agent/slash_dispatcher.py` handles via `slash_skill_fallback` (PR #225, in flight) and returns the body as a `SlashCommandResult` whose output becomes the assistant's reply | wrap fallback result as a synthetic `SkillTool` `tool_use`/`tool_result` pair (Hybrid dispatch — see §6) |

## 3. Architecture

### 3.1 Two layers, two PRs

The fix splits cleanly across two layers, mapping to two PRs:

| Layer | What | Where | PR |
|---|---|---|---|
| **TUI surface** | Dropdown source, filter, ranking, MRU, rendering | `opencomputer/cli_ui/*` only | This PR (independent of archit's open PRs) |
| **Hybrid dispatch wrap** | When the slash dispatcher returns a skill-fallback result, wrap it as a synthetic `SkillTool` `tool_use`/`tool_result` pair so the agent sees skill content the way it would see SkillTool output | 1 surgical change in `agent/loop.py` (~15 lines) | Follow-up PR after archit's #225 merges |

Splitting these is deliberate. archit's PRs #223/#224/#225 ALL touch `agent/loop.py`. Landing the TUI surface in its own PR (zero `agent/` touches) means: (a) no merge-conflict cascade with archit, (b) the user gets the visible improvement immediately, (c) the Hybrid wrap lands after #225 stabilizes so it can target the actual final shape of `loop.py`.

This is honest deferral, not feature-cutting. Both pieces are scoped, owned, and tracked. Sub-PR 2 is gated only on archit's #225 merging.

### 3.2 Component diagram (Sub-PR 1 — TUI surface)

```
                 user types '/' or '/re' or '/pead'
                                │
                                ▼
                 ┌──────────────────────────────────┐
                 │ cli_ui/slash_picker_source.py    │
                 │ (NEW)                            │
                 │   UnifiedSlashSource             │
                 │   .iter_items()                  │
                 │   .rank(prefix) -> List[Match]   │
                 └──────────────────────────────────┘
                          │            │
                  reads from           uses MRU
                          │            │
            ┌─────────────┴──┐    ┌────┴────────────────┐
            ▼                ▼    ▼                     │
     SLASH_REGISTRY   MemoryManager      cli_ui/slash_mru.py
     (14 commands)   .list_skills()     (NEW — append-only
     in slash.py     (existing API,      JSON, last-50 cap)
                     50+ skills)
                                │
                                ▼
                 ┌──────────────────────────────────┐
                 │ cli_ui/slash_completer.py        │
                 │ (MODIFIED)                       │
                 │   SlashCommandCompleter delegates│
                 │   filtering to the source        │
                 └──────────────────────────────────┘
                                │
                                ▼
                 ┌──────────────────────────────────┐
                 │ cli_ui/input_loop.py             │
                 │ (MODIFIED)                       │
                 │   _refilter() reads ranked items │
                 │   _on_enter() records pick to MRU│
                 │   render: source tag + 250-char  │
                 │   description trim               │
                 └──────────────────────────────────┘
                                │
                       user picks /<name>, hits Enter
                                │
                                ▼
                       chat loop submits
                       "/<name> <inline-args>"
                                │
                                ▼ (Sub-PR 1 stops here)
              agent/slash_dispatcher.py — current behaviour
              (commands -> handlers; skills -> archit's
              slash_skill_fallback returns SKILL.md as
              SlashCommandResult.output, surfaced as
              assistant text reply)

         ────────────────  Sub-PR 2 boundary ────────────────

                                ▼
              agent/loop.py — Hybrid wrap (NEW in Sub-PR 2):
              if result.source == "skill":
                  synthesize tool_use (SkillTool) +
                  tool_result (SKILL.md body), inject into
                  conversation, continue agent loop
```

### 3.3 New + modified files (Sub-PR 1)

| File | Action | Approx LOC | Responsibility |
|---|---|---|---|
| `opencomputer/cli_ui/slash.py` | Modify | +25 / -0 | Define `SkillEntry` dataclass alongside existing `CommandDef`. Add `SlashItem = CommandDef \| SkillEntry` union. No change to `SLASH_REGISTRY` itself — skills come from a separate source. |
| `opencomputer/cli_ui/slash_picker_source.py` | **New** | ~150 | `UnifiedSlashSource(memory_manager, mru_store)` class. `.iter_items()` yields `SlashItem`s deduped (command name beats skill id on collision). `.rank(prefix)` returns ranked top-N with score-tier classification. Pure logic, no IO except reading from injected dependencies. |
| `opencomputer/cli_ui/slash_mru.py` | **New** | ~80 | `MruStore(path)` — append-only JSON: `[{name: str, ts: float}]`, last-50 entries cap. `.record(name)`, `.recency_bonus(name) -> float`, atomic write via temp file + rename. Tolerates missing/malformed file by returning empty store. |
| `opencomputer/cli_ui/slash_completer.py` | Modify | +50 / -25 | Replace strict `startswith` filter with delegation to `UnifiedSlashSource.rank()`. Render rows with source tag (`(command)` / `(skill)`) and 250-char description trim. Keep the `Completer` interface so `build_prompt_session` callers (legacy path) keep working. |
| `opencomputer/cli_ui/input_loop.py` | Modify | +60 / -10 | Wire `UnifiedSlashSource` into `read_user_input`'s `_refilter`. Render rows in the custom dropdown with source tag. On Enter that picks an item, record to `MruStore` before submitting. Empty `/` returns ALL items sorted by MRU recency first then alphabetical. |
| `tests/test_slash_picker_source.py` | **New** | ~180 | Ranking by tier, dedup (command beats skill), MRU bonus stacks correctly, empty prefix returns all, score ordering deterministic on ties. |
| `tests/test_slash_mru.py` | **New** | ~80 | Append, cap-at-50, atomic write semantics, malformed-file tolerated, recency bonus decays. |
| `tests/test_slash_completer.py` | Modify | +80 | Substring matches, fuzzy matches, source tags, 250-char trim, descriptions truncated at word boundary with ellipsis. |
| `tests/test_input_loop_skill_picker.py` | **New** | ~140 | Integration: `/` shows all, `/re` ranks correctly, MRU bias works after picking, Enter records pick, dropdown stays bounded at 20 visible (`+N more` hint when exceeded). |

**Sub-PR 1 totals: 5 source files (3 new + 2 modified) + 4 test files (3 new + 1 modified). ~700 LOC.**

### 3.4 Ranking algorithm (stdlib only — no new deps)

Score is a float in `[0.0, 1.0]`. Higher wins. Deterministic tie-break: MRU recency, then alphabetical.

| Tier | Match type | Score | Example for prefix `re` |
|---|---|---|---|
| 1 | Canonical name starts with prefix (case-insensitive) | 1.00 | `/rename`, `/reload`, `/reload-mcp`, `/resume` |
| 2 | Alias starts with prefix | 0.85 | `/reset` (alias of `/clear`) |
| 3 | Word-boundary substring (`re` matches start of any `-`-delimited word in name) | 0.70 | `/code-review`, `/refactor-clean` |
| 4 | Anywhere substring | 0.55 | `/recall`, `/learn` (if a skill called `/learn-react` existed) |
| 5 | Fuzzy via `difflib.SequenceMatcher.ratio() >= 0.55` | scaled `0.40 - 0.50` | typo tolerance like `/pad-screener` matching `/pead-screener` |
| - | MRU bonus | `+0.05` (additive, capped at 1.0) | applied if `name` in MRU's last 50 |

Empty prefix (`/` alone): bypass ranking entirely. Return MRU-recent first (top 5), then alphabetical for the rest. This is the "always trigger when user types `/`" behaviour.

`difflib` is stdlib — no new dependency. For ~100-item picker scale, `SequenceMatcher.ratio()` is fast enough (sub-millisecond per item).

### 3.5 Source tag rendering

Each dropdown row gets a column tag so the user knows what they're picking:

```
❯  /code-review              (command)   Review code for quality and security
   /pead-screener            (skill)     Screen post-earnings gap-up stocks for PEAD anomaly
   /failure-recovery-ladder  (skill)     Use when a tool, fetch, search, scrape...
   /refactor-clean           (command)   Tidy up the active branch
```

Tag classes (in the existing `Style.from_dict`): `dd.tag.command` → cyan; `dd.tag.skill` → green. Mirrors Claude Code's source-coloured tags.

### 3.6 Description truncation

250 characters max, cut at the previous word boundary, suffixed with `…`. Matches Claude Code CHANGELOG line 534. Long descriptions ("Use when a tool, fetch, search, scrape...") would otherwise wrap and break the column alignment.

## 4. Data flow — three concrete cases

### 4.1 User types `/` (empty prefix)

1. `input_loop._on_text_changed` fires → `_refilter("/")`.
2. `_refilter` calls `UnifiedSlashSource.rank("")`.
3. `rank("")` short-circuits to: MRU-recent items (up to 5), then alphabetical-by-name for everything else, capped at 20.
4. Dropdown renders. User sees a mixed list: their last-used skills/commands at top, full inventory below.

### 4.2 User types `/re`

1. `_refilter("/re")` → `UnifiedSlashSource.rank("re")`.
2. `rank` walks all 14 commands + 50+ skills, scoring each by tiers 1-5.
3. Tier 1 hits: `/rename`, `/reload`, `/reload-mcp`, `/resume` (4 commands).
4. Tier 3 hits: `/code-review`, `/refactor-clean` (substrings).
5. Tier 5 hits: `/recall`, plus any fuzzy skill matches.
6. MRU-recent items get `+0.05` if applicable (e.g. user used `/code-review` recently → it floats above other tier-3s).
7. Sorted descending by score, capped at 20, returned.

### 4.3 User picks `/pead-screener` and hits Enter

1. `_apply_selection` replaces buffer text with `/pead-screener`.
2. Enter handler:
   - Records `("pead-screener", time.time())` to MRU store.
   - Exits `app.run_async` with the buffer text as result.
3. Chat loop receives `/pead-screener` as the user's submitted message.
4. Standard slash dispatch path runs (`agent/slash_dispatcher.py`):
   - Primary lookup misses.
   - Fallback (archit's `make_skill_fallback`) hits, loads SKILL.md body.
   - Returns `SlashCommandResult(output=SKILL_BODY, handled=True)`.
5. **Sub-PR 1 ends here.** The SKILL.md content lands as the assistant reply text. Functional but not Claude-Code-grade — the agent saw it as text, not as a tool result.
6. **Sub-PR 2 layer**: `loop.py` checks `result.source == "skill"` and wraps as synthetic `SkillTool` tool_use/tool_result, then re-runs the loop so the model treats it as authoritative tool output (Claude Code parity).

## 5. Out of scope (explicit)

- A separate full-screen `/skills` modal (Claude Code has one with token-count sort). Different surface; defer until dogfood demand.
- Persistent **success-rate** ranking (Claude Code's MRU is recency-only, same as ours).
- Per-skill **argument-hint preview pane** (showing the skill's "Use when..." section as a sub-panel below the row). Description tag is enough for v1.
- **Plugin-defined commands** discovery. Already exists via `agent/slash_commands.py`; this PR doesn't change that surface — the `UnifiedSlashSource` reads from `SLASH_REGISTRY` (TUI-side) which is the existing source of truth for the picker. Plugin-defined `agent/slash_commands_impl/*` are dispatched at the agent layer; making those discoverable from the TUI is a follow-up.
- **rapidfuzz** as a new dependency. Stdlib `difflib` is sufficient at picker scale.

## 6. Hybrid dispatch (Sub-PR 2 — small follow-up)

Lands after archit's #225 merges so the `loop.py` shape is final.

```python
# In agent/loop.py, post-dispatch hook (~15 LOC):
import secrets

result = await slash_dispatcher.dispatch(message, slash_commands, runtime, fallback)
if result and getattr(result, "source", "command") == "skill":
    skill_name, args = parse_slash(message)
    call_id = f"skill_{secrets.token_hex(4)}"
    messages.append({
        "role": "assistant",
        "content": [{
            "type": "tool_use",
            "id": call_id,
            "name": "SkillTool",
            "input": {"skill_id": skill_name, "args": args or ""},
        }],
    })
    messages.append({
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": call_id,
            "content": result.output,
        }],
    })
    # don't `return` — fall through and continue the agent loop so the
    # model gets a turn to act on the skill content
```

This requires a tiny tweak to `slash_skill_fallback.py` to set `SlashCommandResult.source = "skill"` on the returned object. That tweak is one line and can land in either Sub-PR 2 or as a coordinated edit to archit's branch — TBD when #225 merges.

## 7. Error handling

- **Skill list fails to load**: log warning at INFO, dropdown shows commands only. Mirrors archit's pattern in `slash_skill_fallback.py:73`.
- **MRU file corrupt or missing**: silently rebuild empty. Not load-bearing — the user still gets a working picker.
- **Skill description >250 chars**: truncate at last word boundary before 250, append `…`.
- **Skill name collides with command name**: command wins, skill is hidden from dropdown. Dev-mode log warning. Mirrors archit's "primary command always wins over fallback" regression.
- **`difflib.SequenceMatcher.ratio()` exceptionally slow on pathological input** (extremely long names): clamp the comparison length at 64 chars before scoring.
- **Empty `MemoryManager`** (e.g. broken profile): dropdown shows commands only, no error surfaced to user.

## 8. Testing strategy

| Layer | Test type | Coverage |
|---|---|---|
| `slash_picker_source.py` | Unit (pure functions) | Ranking by tier, dedup, MRU bonus stacks, empty prefix returns all, ties broken alphabetically |
| `slash_mru.py` | Unit (filesystem in tmp_path) | Append, cap-at-50, atomic write, malformed-file fallback, missing-file silently empty |
| `slash_completer.py` | Unit | Substring matches, fuzzy matches, source tags rendered correctly, 250-char truncation at word boundary |
| `input_loop.py` | Integration (fake MemoryManager + temp profile_home) | `/` shows all, `/re` ranks correctly, MRU updates after Enter, dropdown caps at 20 with `+N more` |
| Snapshot | Locks the rendered output for empty-`/` so silent regressions surface | One frozen-output test that ensures skills don't disappear from the empty-`/` view |

All tests independent — no order dependencies, no shared state, all use `tmp_path` for filesystem. Total expected new test count: ~25 tests. Existing 19 prompt tests + 5443 broader suite must remain green (verified locally before PR).

## 9. Acceptance criteria

Sub-PR 1 ships when:

1. Typing `/` shows a mixed list of commands AND skills, MRU-first, alphabetical-second, capped at 20 with `+N more` hint when truncated.
2. Typing `/re` ranks: tier-1 commands first, then word-boundary skill matches like `/code-review`, then anywhere-substring matches.
3. Selecting a skill row + Enter submits `/<skill-name>` and lands in the existing slash dispatch path — works today via archit's #225 fallback (or via plain "unknown command" message if #225 hasn't merged).
4. The MRU store survives across sessions (saved to `~/.opencomputer/<profile>/slash_mru.json`).
5. Source tags (`(command)` / `(skill)`) render with distinct colours.
6. Descriptions trim at 250 chars on word boundary with `…`.
7. New tests pass + existing tests stay green.
8. Zero touch to `agent/`, `extensions/`, `plugin_sdk/`. Pure `cli_ui/` PR.

Sub-PR 2 ships when (post-archit-#225-merge):

9. `SlashCommandResult` gains a `source: Literal["command", "skill"]` field defaulting to `"command"`.
10. `slash_skill_fallback.py` sets `source="skill"` on returned results.
11. `agent/loop.py` wraps skill-source results as synthetic `SkillTool` `tool_use`/`tool_result` pairs.
12. The model receives the skill body as a tool result on the next turn (Claude-Code parity).
13. Existing tests for slash dispatch stay green.

## 10. Coordination with archit (parallel session)

archit currently has 4 PRs open touching the slash space:

| PR | Layer | Files | Conflict with this work? |
|---|---|---|---|
| #220 | skills hub | `skills_hub/`, `cli_skills_hub.py`, `MemoryManager.list_skills` extension | No — extends my data source helpfully |
| #223 | Tier 2.A — 6 self-contained slash commands | `agent/slash_commands*` only | No |
| #224 | provider runtime flags (`/reasoning` `/fast`) | providers + `runtime_flags.py` | No |
| #225 | `/<skill-name>` auto-dispatch (agent layer) | `agent/loop.py`, `agent/slash_dispatcher.py`, `agent/slash_skill_fallback.py` | No file overlap with Sub-PR 1; complementary |

**Mitigation:**

1. Branch from latest `main` at start of each implementation session, not from any of archit's open branches.
2. Sub-PR 1 touches `cli_ui/*` ONLY. Verified by file-name diff against archit's PR file lists.
3. Re-fetch + re-list archit's open PRs every ~30 minutes during active implementation. If archit pushes anything new to `cli_ui/`, pause and replan.
4. Sub-PR 2 explicitly waits for archit's #225 to merge before opening — avoids three-way conflict in `agent/loop.py`.
5. The 1-line tweak to `slash_skill_fallback.py` (adding `source="skill"`) coordinates with archit at Sub-PR 2 time — either land in our PR or push as a coordinated edit to archit's branch.

## 11. Risks and unknowns

1. **archit lands a `cli_ui/` change while I'm working.** Low likelihood (their audit explicitly listed `cli_ui` as out-of-scope), but possible. Mitigation: 30-minute re-survey cadence.
2. **MemoryManager.list_skills() is slow on large skill counts.** Sub-millisecond per skill today (filesystem read of frontmatter), so 100 skills = ~100ms. Could become noticeable with 1000+ skills. Mitigation: cache the skill list per session, invalidate on a TTL (~5s). Already a clean place to add this in `UnifiedSlashSource`.
3. **`difflib.SequenceMatcher` is slower than rapidfuzz.** On 100 items per keystroke, still imperceptibly fast. Mitigation: if it ever becomes a bottleneck, swap to rapidfuzz behind the same interface — a localized change.
4. **Skill IDs that look like commands cause collisions.** A user could install a skill named `clear` or `help`. Mitigation in §7: command always wins, skill hidden, dev log warning. Acceptable trade.
5. **Hybrid dispatch (Sub-PR 2) requires architectural buy-in on `SlashCommandResult.source`.** That field is a new public-ish contract. Mitigation: default to `"command"` for backward compat; only `slash_skill_fallback` sets `"skill"`; existing tests should stay green.

## 12. Sequencing summary

- **Now**: This spec → user reviews → `writing-plans` produces task plan → `executing-plans` ships Sub-PR 1.
- **After archit's #225 merges**: open Sub-PR 2 (Hybrid wrap in `agent/loop.py`).
- **Dogfood** for 1-2 weeks before considering the deferred follow-ups (separate `/skills` modal, argument-hint pane, rapidfuzz upgrade).

---

## Spec self-review (run before user review)

**1. Placeholder scan.** No "TBD" / "TODO" / "fill in later" remaining. The "TBD when #225 merges" in §6 is a coordination note, not a content gap — the actual code change is fully specified there. **Pass.**

**2. Internal consistency.** §3.4 ranking tiers, §4 worked examples, §8 testing all reference the same tier numbers and bonus values. §3.3 file list and §8 test list match (every new file has a matching test row). §10 coordination matches §3.1 sequencing. **Pass.**

**3. Scope check.** Sub-PR 1 is ~700 LOC across 9 files (5 source + 4 test). Sub-PR 2 is ~30 LOC across 2 files. Both are single-implementation-plan sized. Splitting them is justified by the merge-conflict argument, not by hidden scope. **Pass.**

**4. Ambiguity check.**
- "MRU bonus +0.05" explicit. **Pass.**
- "20-item cap with `+N more` hint" explicit. **Pass.**
- "Description truncated at 250 chars on word boundary with `…`" explicit. **Pass.**
- "Source tag colours: cyan for command, green for skill" explicit. **Pass.**

**Result: spec is internally consistent and unambiguous. Ready for user review.**
