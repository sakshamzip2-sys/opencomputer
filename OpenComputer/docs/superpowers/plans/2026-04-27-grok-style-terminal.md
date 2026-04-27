# Grok-style Terminal Chat Experience

> Single-PR ship of Option B from the brainstorm. Goal: terminal feels closer to Grok / claude.ai with a thinking block, live spinner, inline tool-call status, live markdown rendering, and a token-rate readout.

## Context

Current `_run_turn` in `cli.py`:
- Prints `oc › ` then streams plain-text chunks (no markdown highlight)
- Re-renders Markdown ONLY if streaming was off (so currently never)
- Shows `(N iterations · X in / Y out)` at end
- **Loses thinking content** — `ProviderResponse.reasoning` is captured to DB but never shown
- **Tool calls are silent** — user sees text, then `(5 iterations)` with no idea what ran
- **No spinner** — long delays before first token feel broken

Existing infra we can lean on (verified by Bash inspection):
- `ProviderResponse.reasoning` / `reasoning_details` populated by Anthropic + OpenAI providers
- `HookEvent.PRE_TOOL_USE` / `POST_TOOL_USE` already fire from `loop.py:1312`
- Rich's `console.status()`, `console.Live()`, `Markdown` all in tree (`rich` is a top-level dep)

## File map (single PR)

| File | Modification |
|---|---|
| `opencomputer/cli.py::_run_turn` | Replace plain stream with `Live`-rendered Markdown buffer + spinner + thinking block + live tool status. ~80 LOC delta. |
| `opencomputer/cli_ui/streaming.py` (new) | `StreamingRenderer` class — encapsulates the `Live` + buffer + tool-status panel. ~150 LOC. |
| `opencomputer/cli_ui/__init__.py` (new) | Empty package init. |
| `tests/test_streaming_renderer.py` (new) | Unit tests for the renderer (mocks Rich Live). |

## Tasks

### Task 1 — `StreamingRenderer` skeleton

Create `opencomputer/cli_ui/streaming.py` with:

```python
class StreamingRenderer:
    """Owns the Rich.Live for one turn. Receives chunk + tool callbacks; renders.

    States:
      pre-stream:   spinner "◐ Thinking… 0:Ns" with elapsed-time updater
      streaming:    Markdown buffer of accumulated text, re-rendered at 4 fps
      tool-active:  panel below stream showing "🔧 <tool> args… (Ns)"
      done:         flush final Markdown, drop Live, print token-rate footer
    """
    def __init__(self, console: Console): ...
    def __enter__(self) -> "StreamingRenderer": ...
    def __exit__(self, *exc): ...
    def on_chunk(self, text: str) -> None: ...
    def on_thinking_chunk(self, text: str) -> None: ...
    def on_tool_start(self, name: str, args_preview: str) -> None: ...
    def on_tool_end(self, name: str, ok: bool, dur_s: float) -> None: ...
    def finalize(self, iterations: int, in_tok: int, out_tok: int, elapsed: float) -> None: ...
```

### Task 2 — Wire into `_run_turn`

Replace the `on_chunk` closure in `cli.py:_run_turn` with a `StreamingRenderer` context manager. Pass `renderer.on_chunk` as the existing `stream_callback`. Add hook subscriptions for `PRE_TOOL_USE` / `POST_TOOL_USE` that call `on_tool_start` / `on_tool_end`.

### Task 3 — Thinking block

After `run_conversation` returns, if `result.reasoning` is set, render a dim-bordered Rich `Panel` ABOVE the answer:

```
╭─ 💭 Thinking (2.4s) ─────────────╮
│ The user is asking about X.       │
│ I should check Y first because…   │
╰───────────────────────────────────╯
```

Streaming the thinking chunks live (Anthropic supports this in their SDK) is **deferred** — tag this PR's PRE-LIVE thinking only. Live-thinking-stream is a follow-up if the after-the-fact rendering feels insufficient.

### Task 4 — Tool-call status panel

Hook callbacks render a panel below the stream:

```
🔧 Bash             ls /tmp                    0.4s
✓  Read             /Users/saksham/.zshrc      0.1s
🔧 WebSearch        kubernetes ingress          (running)
```

Last 3 calls visible; older scroll off. Updates every tool start/end.

### Task 5 — Token rate

At `finalize`, compute `out_tok / elapsed_s` and append:

```
(2 iterations · 13909 in / 16 out · 86 tok/s · 0.2s)
```

### Task 6 — Tests

`tests/test_streaming_renderer.py`:
- `on_chunk` accumulates buffer + flushes Markdown at end
- `on_tool_start` / `on_tool_end` produce expected panel rows
- Spinner replaced by stream when first chunk arrives
- `finalize` prints expected footer including tok/s
- Hook subscription order doesn't double-print

Mock Rich's `Live` so tests don't depend on TTY.

## Audit (expert critic pass)

### Flawed assumptions

1. **"Rich.Live re-renders Markdown 4× per second cleanly."** Re-rendering Markdown on every chunk would flicker; 4 fps with a buffer is conventional but heavy code blocks (e.g. 200-line Python) can trigger visible lag. **Mitigation:** start at 4 fps; benchmark; downgrade to 2 fps if noticeable. Cap buffer growth at last-N chars to avoid O(n²) re-render.

2. **"Hook subscription is the right surface for tool-call status."** Hooks fire on every tool dispatch but the cli.py registration is process-global — multiple chat turns within one process would re-register. **Mitigation:** register the renderer's hook callbacks in `__enter__` and unregister in `__exit__`. Need a `HookEngine.unsubscribe()` API; check it exists or add a tiny one.

3. **"Anthropic's SDK streams thinking chunks via the same callback."** Possibly false — extended thinking chunks are a different content-block type. The current `stream_callback` may receive only assistant text, not thinking text. **Mitigation:** for v1, render thinking AFTER the turn (post-hoc, not live). Live streaming of thinking is the deferred follow-up.

4. **"Long-running bash will look broken — spinner only shows BEFORE first token."** True. After the first stream chunk, the spinner goes away but a tool-call could still be running silently for minutes. **Mitigation:** keep the tool-status panel persistent during streaming (it serves as the "what's running now" indicator).

### Edge cases

1. **Non-TTY stdin (piping `echo … | opencomputer chat`)** — Rich.Live would print escape codes that pollute the output. **Mitigation:** detect non-TTY and fall through to the OLD plain-streaming path. Test: `printf "hi\n/exit\n" | opencomputer chat` still produces plain output.

2. **Window narrower than panel** — Rich auto-truncates but tool-args preview could wrap badly. **Mitigation:** cap args preview at `console.width - 40`.

3. **Stream cancelled by Ctrl+C mid-turn** — `Live.__exit__` should restore terminal cleanly. Test by sending SIGINT during a fake long stream.

4. **Multiple concurrent tool calls** — OC supports parallel tool dispatch (see `loop.py`). Two `PRE_TOOL_USE` hooks could fire before either completes. **Mitigation:** the tool panel uses a dict keyed by `(tool_name, call_id)` so concurrent calls render distinctly. `POST_TOOL_USE` flips status without removing the row.

5. **Code block split across chunks** — Rich's Markdown renderer chokes on incomplete fences. **Mitigation:** when buffer ends with an unmatched ` ``` ` (odd backtick count in the buffer), close it provisionally before render: `buf + "\n```"`.

### Missing considerations

1. **Theme / colour scheme** — Grok's UI uses a specific dim-grey for thinking. OC has no theme system. Use Rich's `style="dim"` + `border_style="grey50"` — defaults that look fine on most terminals.

2. **CHANGELOG entry** — required per project convention.

3. **Doc / README screenshot** — terminal UX changes deserve a screenshot in `docs/`. Skip for v1; ship a `docs/terminal-ui.md` paragraph.

4. **Test suite cost** — ~10 new tests, ~3 sec wall clock. Acceptable.

### Confidence

| Sub-feature | Confidence | Risk |
|---|---|---|
| Spinner before first token | 95% | Trivial Rich.Status |
| Thinking block (post-hoc, not live) | 90% | Just a Panel render |
| Tool-call status panel | 80% | Hook (un)subscribe + concurrency edge |
| Live markdown stream | 70% | Re-render perf; chunk-split code fences |
| Token rate readout | 95% | One arithmetic line |
| Non-TTY fallback | 90% | Detection is one if-statement |

**Total estimated effort:** 3-4 days per the brainstorm. Realistic given the audit risks.

## Verification

```bash
opencomputer chat
> explain how kubernetes ingress works
# Expected: spinner, then live-rendered markdown w/ syntax highlighting in code blocks,
# thinking panel above answer, "1 iteration · ... · ~80 tok/s · 1.4s" at end

> read /etc/hosts and tell me my hostname
# Expected: "🔧 Read /etc/hosts (0.0s)" panel during; ✓ flips when done

printf "hi\n/exit\n" | opencomputer chat
# Expected: plain stream (no Live escapes), backwards-compat for pipes
```

## Out of scope (deferred)

- Live streaming of THINKING chunks (Anthropic SDK supports it; v1 renders post-hoc)
- Themeable UI / config-driven colour scheme
- Scrollback / interrupt-and-resume / persistent input history (Textual would be the path; Option C from the brainstorm)
- Tool argument richness — for v1 we show a 60-char preview; full nested-args display is overkill
