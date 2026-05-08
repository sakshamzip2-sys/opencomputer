# Personality, Skins & `@`-References — Design (2026-05-08)

Status: spec / brainstormed
Branch: `feat/personality-skins-atrefs-2026-05-08`
Worktree: `/Users/saksham/Vscode/claude-doc1-personality-skins-atrefs-2026-05-08`
Source: Hermes docs blocks "Batch / Memory / Memory Providers" + "Context / `@`-refs / Personality / Skins" (2026-05-08 user request)

---

## 1. Problem

Two Hermes documentation blocks request feature parity. Discovery shows the bulk is already shipped (MEMORY.md/USER.md, AGENTS.md priority chain with progressive subdir hints, SOUL.md, Honcho/Mem0 providers, Memory tool). Three real gaps remain:

| Surface | OC state today | What Hermes ships |
|---|---|---|
| `/personality` | Stub — name stored in `runtime.custom`; **no overlay loaded** | 14 named registers, custom from config, prompt slot #7 |
| `/skin` | Stub — name stored in `runtime.custom`; **no rendering** | 9 named themes (colors, spinner verbs, banner art, branding), YAML-loadable custom |
| `@`-references | None | `@file:` (with line range), `@folder:`, `@diff`, `@staged`, `@git:N`, `@url:` with size caps and a blocked-paths policy |

This spec covers those three. The remaining items are explicitly deferred (§9) with rationale, not silently dropped.

## 2. Non-goals

- Memory provider expansion (OpenViking, Hindsight, Holographic, RetainDB, ByteRover, Supermemory). Each provider is a real plugin with an external account/server — skeleton-only stubs would be parity padding. Defer to its own scoped PR with at most 1–2 providers per cycle, picked by user demand.
- Hermes' `batch_runner.py` (parallel agent runner producing ShareGPT trajectories). OC's `opencomputer/batch.py` covers the bulk-Anthropic-API use case. The agent-trajectory generator is a fine-tuning workflow with a different audience; defer until concretely requested.
- Skinning the dashboard / TUI custom-painted views. v1 scope: Rich console primitives in the chat REPL (palette, spinner, branding, banner, tool prefix, tool emojis). Dashboard skinning is a separate effort.
- Per-channel personality (different register on Telegram vs CLI). One personality per session; fine for v1.

## 3. Architecture overview

```
                    ┌─────────────────────────────────────────────┐
                    │          opencomputer/agent/personality/     │
                    │   ┌──────────────────┐   ┌────────────────┐  │
                    │   │ builtins.py      │   │ loader.py      │  │
                    │   │  (14 strings)    │   │ (config + custom│  │
                    │   └─────────┬────────┘   │  override)     │  │
                    │             │            └─────┬──────────┘  │
                    │             └───────┬──────────┘             │
                    │                     ▼                        │
                    │             Personality(name, body)          │
                    └─────────────────────┬────────────────────────┘
                                          │ injected as slot #7
                                          ▼
                    PromptBuilder.build_system(...) ─ slot stack:
                        1 SOUL.md          2 tool guidance
                        3 memory/user      4 skills
                        5 AGENTS.md        6 timestamp/platform
                        7 personality      ← this PR

                    ┌─────────────────────────────────────────────┐
                    │          opencomputer/cli_ui/skin/            │
                    │   ┌──────────────────┐   ┌────────────────┐  │
                    │   │ spec.py          │   │ loader.py      │  │
                    │   │  (SkinSpec)      │   │ (built-in YAML │  │
                    │   └─────────┬────────┘   │  + ~/.opencomputer/
                    │             │            │   skins/ override)│
                    │             ▼            └─────┬──────────┘  │
                    │   apply.py: rich.theme.Theme  │              │
                    │     + spinner verbs           │              │
                    │     + branding strings        │              │
                    │     + banner art              │              │
                    │     + tool prefix + emojis    │              │
                    └───────────────────────────────┘
                                          │
                                          ▼
                    cli_ui/style.py + cli_ui/streaming.py + banner

                    ┌─────────────────────────────────────────────┐
                    │      opencomputer/agent/at_references.py     │
                    │    parse(text) → list[AtRef]                 │
                    │    expand(text, *, ctx) → expanded text +    │
                    │       trailer "--- Attached Context ---"     │
                    │    handlers: file / folder / diff / staged   │
                    │              / git:N / url                   │
                    │    enforces: soft cap (25%), hard cap (50%), │
                    │              blocked paths, max-folder-200,  │
                    │              max-git-10, SSRF guard reuse    │
                    └───────────────────────────────────────────────┘
                                          │
                                          ▼
                    Hooked from cli_ui/input_loop.py before sending
                    user message into the agent loop. CLI surface only.
```

## 4. Personality

### 4.1 Built-ins (14)

Mirror Hermes' set:

```
helpful   concise    technical  creative  teacher  kawaii
catgirl   pirate     shakespeare surfer    noir     uwu
philosopher hype
```

Each is a short imperative paragraph giving the model a register. Kept short (≤ 200 words each, well below the 800-token system-prompt budget).

### 4.2 Custom personalities

YAML config under the active profile's `config.yaml`:

```yaml
agent:
  default_personality: helpful   # NEW — used when no /personality and no flag
  personalities:                 # NEW
    codereviewer: |
      You are a meticulous code reviewer. Identify bugs, security
      issues, and design smells. Cite line numbers. No throat-clearing.
    interviewer: |
      You are a senior engineering interviewer running a live session.
      Ask one focused question at a time; never give away the answer.
```

Custom keys win over built-ins of the same name (override pattern; matches Hermes).

### 4.3 Resolution + persistence

Resolution order at session start:
1. CLI flag `--personality NAME` (highest)
2. `runtime.custom["personality"]` set by `/personality` mid-session
3. `agent.default_personality` from config
4. Implicit `helpful`

`/personality NAME` writes both into `runtime.custom` (immediate) **and** the active profile's `config.yaml` under `agent.default_personality:` (persistent across sessions). `/personality reset` writes the empty value, which collapses to "helpful".

Existing slash command (`opencomputer/agent/slash_commands_impl/skin_personality_cmd.py`) is replaced — but the public name and description survive.

### 4.4 Prompt-stack injection

`PromptBuilder.build_system_prompt(...)` already accepts a `personality: str` argument (line 254). We currently pass through the *name* and never resolve it. Change: PromptBuilder owns resolution by calling `personality.loader.resolve(name)` to get the body, then renders into the existing slot at the end of the system prompt.

### 4.5 Channel applicability

Personality applies on every adapter that talks to the LLM (CLI, Telegram, Discord, etc.) — it is text-only so it round-trips fine. Telegram register would be different in real life, but **per-channel personality is non-goal v1**.

## 5. Skins

### 5.1 SkinSpec dataclass

`opencomputer/cli_ui/skin/spec.py`:

```python
@dataclass(frozen=True, slots=True)
class SkinSpec:
    name: str
    description: str
    colors: dict[str, str]        # hex strings keyed by Rich style names
    spinner: SpinnerSpec          # waiting_faces, thinking_faces, thinking_verbs, wings
    branding: BrandingSpec        # agent_name, welcome, goodbye, response_label, prompt_symbol, help_header
    banner_logo: str              # rich-markup ascii (may be empty)
    banner_hero: str              # rich-markup ascii (may be empty)
    tool_prefix: str = "┊"
    tool_emojis: dict[str, str] = field(default_factory=dict)
```

### 5.2 Built-ins (9)

Bundled as YAML under `opencomputer/cli_ui/skin/builtins/`:

```
default.yaml          ares.yaml          mono.yaml
slate.yaml            daylight.yaml      warm-lightmode.yaml
poseidon.yaml         sisyphus.yaml      charizard.yaml
```

Each is small (≈ 30–60 lines). Missing keys inherit from `default.yaml`.

### 5.3 Custom skins

Drop a YAML file at `~/.opencomputer/skins/<name>.yaml` (NOT profile-scoped — skins are per-machine UI preference, not per-profile context). User skins override built-ins of the same name.

### 5.4 Application

`opencomputer/cli_ui/skin/apply.py`:

```python
def apply_skin(spec: SkinSpec, console: Console) -> None:
    # 1. Build rich.theme.Theme from spec.colors and bind to console.
    # 2. Replace spinner verb pool used by streaming.py.
    # 3. Replace branding strings used by greeting / prompt / banner.
    # 4. Set tool_prefix and tool_emojis used by tool render.
```

Application is idempotent — calling it again with a different spec swaps everything.

### 5.5 Resolution + persistence

Resolution order:
1. CLI flag `--skin NAME`
2. `runtime.custom["skin"]` set by `/skin` mid-session
3. `display.skin` from config
4. Implicit `default`

`/skin NAME` swaps the active skin immediately AND writes `display.skin: NAME` into `~/.opencomputer/<profile>/config.yaml` for persistence. Mid-session swap calls `apply_skin` again on the live console.

### 5.6 Channel applicability

Skins apply to the CLI / Rich console only. On Telegram, Discord, etc. there is no Rich console to color. The branding *string* (`agent_name`) is reused as a label by some adapters — that part still respects the skin. Anything else is a no-op.

## 6. `@` references

### 6.1 Reference grammar

```
@file:<path>                 → inject file body
@file:<path>:<a>-<b>         → inject lines a..b (1-indexed, inclusive)
@folder:<path>               → tree listing (≤ 200 entries) with size + mtime
@diff                        → `git diff` (unstaged)
@staged                      → `git diff --staged`
@git:<N>                     → last N commits with patches (clamp N ≤ 10)
@url:<https://...>           → fetch + inject web page (text only)
```

Multiple references in one user message expand all that fit; if the *combined* expansion would exceed limits, refuse the over-budget ones and append a warning trailer.

### 6.2 Size policy

| Limit | Value | Action when exceeded |
|---|---|---|
| Per-ref soft | 25% of model context window | Warning trailer, expansion proceeds |
| Per-ref hard | 50% of model context window | Refuse this one ref; surface `[ref refused: too large]` |
| Folder entries | 200 | Truncate; trailer says how many were dropped |
| Git commits | 10 | Clamp |

The context-window value comes from `opencomputer.agent.model_capabilities`; default 200K when unknown. (We already have a model-agnostic context-window dictionary from PR #343, so this just reads it.)

### 6.3 Blocked paths

Always refuse:

```
~/.ssh/    ~/.aws/    ~/.gnupg/    ~/.kube/
~/.netrc   ~/.pgpass  ~/.bashrc    ~/.zshrc
$OPENCOMPUTER_HOME/.env
$OPENCOMPUTER_HOME/skills/.hub/    (matches Hermes' equivalent)
```

Plus any file matching `*.pem`, `*.key`, `id_rsa*`. Refusal goes through a single helper so the policy is one place to audit.

### 6.4 SSRF for `@url:`

Reuse `opencomputer.agent.link_understanding` SSRF guard (Tier-S, PR #171). Refuse private network ranges, file://, etc. Timeout 5s. Strip HTML to text.

### 6.5 Hook point

`opencomputer/cli_ui/input_loop.py` already does pre-send transformations (file completer, slash dispatch). Add a single call after slash dispatch and before message construction:

```python
if "@" in user_text:
    user_text = at_references.expand(user_text, ctx=at_ref_context)
```

Channel adapters do **not** expand `@` refs (matching Hermes' "CLI-only" rule). The `@` syntax is plausibly meaningful in user chat content, so silent expansion on Telegram would be a surprise.

## 7. Failure modes & fallbacks

| Surface | Failure | Behavior |
|---|---|---|
| Personality | Custom YAML malformed / key not found | Log warning; fall back to `helpful`; never crash session |
| Personality | Built-in name typo | `/personality typo` → "Unknown; available: ..."; current value unchanged |
| Skin | Skin YAML malformed | Log warning; fall back to `default`; never crash startup |
| Skin | Color hex invalid | Use `default.yaml`'s value for that key; warn once |
| Skin | Apply called before console exists (early CLI) | Resolution stored; first `apply_skin` later succeeds |
| `@file:` | Path missing | Expand to `[file not found: <path>]`; message still sends |
| `@file:` | Path blocked | Expand to `[blocked path: <path>]`; audit log entry |
| `@file:` | File too large (hard cap) | Expand to `[ref refused: <path> exceeds hard cap]` |
| `@url:` | Timeout / 5xx | Expand to `[fetch timed out]` / `[fetch failed: <status>]` |
| `@url:` | SSRF refused | Expand to `[blocked: private network]` |
| `@diff` etc | Not a git repo | Expand to `[not a git repository]` |
| `@git:50` | N too large | Clamp to 10; trailer notes clamp |

## 8. Tests

Per surface, table-driven tests. The big ones:

- `tests/test_personality_loader.py` — built-in lookup, custom override, resolution chain (CLI flag > runtime > config > default), reset path.
- `tests/test_personality_prompt_injection.py` — PromptBuilder injects body into slot #7; SOUL.md still in slot #1; companion-mode (`active_persona_id == "companion"`) still wins where it should.
- `tests/test_skin_loader.py` — built-in YAML parse, custom override, missing-key inheritance, malformed YAML fail-soft.
- `tests/test_skin_apply.py` — `apply_skin` swaps Rich theme + spinner verbs idempotently.
- `tests/test_at_references_parser.py` — grammar table, line-range edge cases, multi-ref in one message.
- `tests/test_at_references_expand.py` — file/folder/diff/staged/git/url, blocked paths, soft/hard cap, SSRF, missing-file, not-a-repo.
- `tests/test_at_references_input_loop.py` — integration: input-loop calls expander, expanded text reaches agent loop.

Plus a `--personality` and `--skin` CLI flag smoke test that verifies persistence across processes.

## 9. Honest deferrals

The user prompt names larger surfaces. Not in this PR:

| Item | Why deferred | Suggested next step |
|---|---|---|
| Six new memory providers | Each is a plugin with an external service; stubs without working backends are parity padding | One PR per provider, demand-driven |
| `batch_runner` ShareGPT trajectory generator | Fine-tuning data ops, distinct audience; existing `batch.py` covers user-facing bulk Anthropic API | Build under `extensions/agent-batch-runner/` if asked |
| Dashboard skinning | TUI dashboard has many custom-painted views; v1 chat-REPL skinning is the natural primitive | Tier-2 PR after v1 lands |
| Per-channel personality | Adds a multiplexer where one knob will do | Wait for concrete request |
| Personality plugin SDK exports | Plugin authors don't need to author personalities programmatically; YAML covers it | Add only if a real plugin needs it |
| `HERMES.md` priority entry in context-file chain | OC uses `OPENCOMPUTER.md`; adding `HERMES.md` would muddle the OC convention | Only if a Hermes-shared workspace use case appears |
| Security scanner for context files | `subdirectory_hints.py` already has the no-op placeholder; threat model is bigger than this PR | Separate security pass |

## 10. Risks (accepted)

- **Skin scope creep**: an enthusiastic reviewer might want full TUI dashboard skinning. Pre-empt by stating in the PR body that v1 is chat-REPL only.
- **Custom personality conflict with built-in name**: silent override could surprise. Mitigation: when a custom name shadows a built-in, log one-line info on first resolve.
- **`@url:` cost**: a hostile prompt with `@url:malicious.example` would fetch. Mitigations: SSRF guard reuse, 5s timeout, hard cap. Acceptable.
- **`@file:` line ranges off-by-one**: easy to get wrong. Tests cover boundary, off-end, reverse range.

## 11. Out-of-scope cleanup tracked elsewhere

None for this PR.

---

This spec is the contract for `feat/personality-skins-atrefs-2026-05-08`.
