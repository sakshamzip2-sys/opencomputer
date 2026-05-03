# Thinking History v3 — Claude.ai Visual Parity

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refactor the v2 thinking-history UI (PR #387) so it matches Claude.ai's web UX exactly:

- **Collapsed (Image #10):** Just the AI summary + chevron `›`. No metadata clutter (no `· 💭 Thought for X · turn #N — /reasoning show to expand`).
- **Expanded (Image #9):** Header = same summary + chevron `⌄`. Children = each step with a SEMANTIC icon (`⏰` thinking, `📄` file ops, `⚙️` shell, `🔧` other), an action description (not tool-args literal), and an attachment chip below file ops (e.g. `openclaw-always-running.md`). Final `⊘ Done` row.

**Audit-confirmed honest scope:**
- True click-to-toggle interactivity is **out of scope** (needs Textual migration). The chevron is a visual hint only — `Ctrl+X Ctrl+R` (already bound) and `/reasoning show` remain the actual triggers.
- Per-action AI-generated descriptions (Claude.ai shows "Create a focused, stripped-down doc covering only..." — not extractable from tool args alone) are **out of scope**: would require an LLM call per action. v3 uses tool-args + path extraction as the cheap-but-meaningful approximation.
- All v3 changes are PURELY presentational. No data-model changes; no new LLM calls; no new daemon threads. Just rendering refactors of `streaming.py.finalize` collapsed-line + `reasoning_store.render_turn_tree`.

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `opencomputer/cli_ui/reasoning_store.py` | Modify | Cleaner `render_turn_tree` (semantic icons + chips + Done row); add `_action_icon` + `_action_label` + `_extract_path_chip` helpers |
| `opencomputer/cli_ui/streaming.py` | Modify | Cleaner collapsed-line format (just summary + chevron when summary present; metadata-only fallback otherwise) |
| `tests/test_reasoning_store.py` | Modify | Update render tests for new format |
| `tests/test_streaming_thinking.py` | Modify | Update collapsed-line format tests |

---

## Task 0: Worktree

Already done at `/Users/saksham/.config/superpowers/worktrees/claude/thinking-history-v3` (branch `feat/thinking-history-v3` from main `a382a65e`).

---

## Task 1: Cleaner collapsed-line format

**Files:** `opencomputer/cli_ui/streaming.py`, `tests/test_streaming_thinking.py`

Current v2 format (with summary):
```
Wrote a haiku · 💭 Thought for 0.8s · turn #5 · 3 actions — /reasoning show to expand
```

New v3 format (with summary):
```
Wrote a haiku ›
```
(That's it. Summary + chevron. Metadata moves to the expanded view's subtitle.)

Without summary (graceful fallback — model has no thinking, or summary failed):
```
💭 Thought for 0.8s · turn #5 · 3 actions — /reasoning show
```
(Today's metadata-only format kept as fallback.)

- [ ] **Step 1: Update tests in `test_streaming_thinking.py`.**

Replace the assertion in `test_finalize_collapsed_line_includes_summary_when_available` to check the new clean format:

```python
def test_finalize_collapsed_line_v3_summary_only_when_present() -> None:
    """v3: when summary lands, the collapsed line is JUST the summary
    + chevron — no metadata clutter (Claude.ai parity, Image #10)."""
    from unittest.mock import patch
    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    out = io.StringIO()
    store = ReasoningStore()
    with patch(
        "opencomputer.agent.reasoning_summary.generate_summary",
        return_value="Wrote a haiku about sloths",
    ):
        renderer = StreamingRenderer(
            Console(file=out, force_terminal=False), reasoning_store=store
        )
        with renderer:
            renderer.on_thinking_chunk("let me think about haikus")
            renderer.finalize(
                reasoning="let me think about haikus",
                iterations=1, in_tok=1, out_tok=1, elapsed_s=0.1,
                show_reasoning=False,
            )
    text = out.getvalue()
    # The clean v3 format: summary + chevron, no metadata clutter.
    assert "Wrote a haiku about sloths" in text
    assert "›" in text  # chevron-right indicator
    # Metadata clutter must NOT appear when summary is present.
    assert "Thought for" not in text
    assert "turn #" not in text
    assert "actions" not in text
    assert "/reasoning show to expand" not in text
```

Update the existing fallback test to check that today's format STILL appears when summary is missing:

```python
def test_finalize_collapsed_line_v3_fallback_keeps_metadata_format() -> None:
    """When summary is unavailable (None), the collapsed line falls
    back to the metadata-only format (today's behavior)."""
    from unittest.mock import patch
    from opencomputer.cli_ui.reasoning_store import ReasoningStore

    out = io.StringIO()
    store = ReasoningStore()
    with patch(
        "opencomputer.agent.reasoning_summary.generate_summary",
        return_value=None,
    ):
        renderer = StreamingRenderer(
            Console(file=out, force_terminal=False), reasoning_store=store
        )
        with renderer:
            renderer.on_thinking_chunk("hmm")
            renderer.finalize(
                reasoning="hmm",
                iterations=1, in_tok=1, out_tok=1, elapsed_s=0.1,
                show_reasoning=False,
            )
    text = out.getvalue()
    # Fallback format: metadata + reasoning-show hint.
    assert "Thought for" in text
    assert "/reasoning show" in text
```

The OLD `test_finalize_collapsed_line_includes_summary_when_available` and `test_finalize_collapsed_line_falls_back_when_summary_unavailable` get replaced by these two.

- [ ] **Step 2: Refactor `streaming.py`'s collapsed-line printing.**

In `streaming.py.finalize`, find the `_summary_str` branch and replace it with the v3 clean format:

```python
                if _summary_str:
                    # v3 (Claude.ai parity, Image #10): just summary +
                    # chevron-right. Metadata clutter moves to the
                    # expanded tree's subtitle line so the collapsed
                    # form reads like a section heading. Use Ctrl+X
                    # Ctrl+R or /reasoning show <N> to expand.
                    self.console.print(
                        f"[bold]{_summary_str}[/bold] [dim]›[/dim]"
                    )
                else:
                    # Fallback when summary is unavailable: today's
                    # metadata-only format (turn id, action count,
                    # how-to-expand hint).
                    self.console.print(
                        f"[dim cyan]{meta} — /reasoning show to expand[/dim cyan]"
                    )
```

- [ ] **Step 3: Run tests + commit.**

---

## Task 2: Semantic action icons + chips in expanded tree

**Files:** `opencomputer/cli_ui/reasoning_store.py`, `tests/test_reasoning_store.py`

Today's `render_turn_tree` shows each tool action as `🔧 Edit(file_path=...) ✓ 0.05s`. v3 makes it semantic:

```
🧠 The user wants me to extract the daemon mechanics from...
📄 Edit
   foo.md
⚙️ Bash · ls -la
🔧 WebFetch · https://example.com/...
⊘ Done · 3 actions in 1.8s
```

Where:
- 📄 = file-targeted tools (Edit, Write, Read, MultiEdit, NotebookEdit)
- ⚙️ = shell tools (Bash, BashTool)
- 🔧 = anything else
- The file-path "chip" appears as an indented child under file-targeted tools, in a different style.
- 🧠 replaces the verbose "🧠 Reasoning:" label.
- ⊘ Done row appears at the end with action count + total duration.

- [ ] **Step 1: Add helpers near top of `reasoning_store.py`:**

```python
_FILE_TOOLS = frozenset({"Edit", "Write", "Read", "MultiEdit", "NotebookEdit"})
_SHELL_TOOLS = frozenset({"Bash", "BashTool"})


def _action_icon(tool_name: str) -> str:
    """Map a tool name to its semantic icon for the expanded tree."""
    if tool_name in _FILE_TOOLS:
        return "📄"
    if tool_name in _SHELL_TOOLS:
        return "⚙️"
    return "🔧"


def _extract_path_chip(action: ToolAction) -> str | None:
    """Extract a single file path from a file-tool's args_preview for
    the chip display. Best-effort; returns None when no clean path is
    extractable.

    Args previews look like ``"file_path=/tmp/foo.md, content=..."`` or
    ``"path=foo.md"`` — pluck the value of the path-ish key.
    """
    if action.name not in _FILE_TOOLS:
        return None
    preview = action.args_preview or ""
    for key in ("file_path", "path", "notebook_path"):
        marker = f"{key}="
        if marker in preview:
            tail = preview.split(marker, 1)[1]
            # Path ends at the first comma or end of string.
            value = tail.split(",", 1)[0].strip().strip('"').strip("'")
            return value or None
    return None
```

- [ ] **Step 2: Refactor `render_turn_tree`** to use the new helpers and produce the v3 layout. Replace the body with:

```python
def render_turn_tree(turn: ReasoningTurn) -> Tree:
    """Render one ReasoningTurn as a Rich Tree matching Claude.ai's
    web UX (Image #9):

        Wrote a haiku about sloths ⌄                  (header)
        ├── 🧠 The user wants me to think about...    (reasoning text)
        ├── 📄 Edit                                    (file action)
        │       foo.md                                 (path chip)
        ├── ⚙️ Bash · ls -la                          (shell action)
        └── ⊘ Done · 3 actions in 1.8s                (footer)

    No-thinking and no-action turns get a single placeholder child
    each so users see the structure even when the turn is sparse.
    """
    s = "" if turn.action_count == 1 else "s"

    if turn.summary:
        # Header is just the summary + chevron-down (expanded) — Image #9.
        header = Text.assemble((turn.summary, "bold"), ("  ⌄", "dim"))
    else:
        # No summary → fall back to today's metadata header.
        header = Text.assemble(
            ("💭 ", "dim cyan"),
            (f"Turn #{turn.turn_id}", "bold cyan"),
            ("  ·  ", "dim"),
            (f"Thought for {_fmt_duration(turn.duration_s)}", "dim cyan"),
            ("  ·  ", "dim"),
            (f"{turn.action_count} action{s}", "dim cyan"),
        )

    tree = Tree(header, guide_style="grey50")

    # Reasoning text node — semantic icon (clock = "the AI was thinking").
    if turn.thinking:
        thinking_node = tree.add(
            Text.assemble(("🧠 ", "dim"), (turn.thinking.split("\n")[0], "dim"))
        )
        for line in turn.thinking.splitlines()[1:]:
            thinking_node.add(Text(line, style="dim"))
    else:
        tree.add(Text("(no extended thinking)", style="italic dim"))

    # Tool actions — semantic icons + path chips.
    if turn.tool_actions:
        for action in turn.tool_actions:
            mark = "✓" if action.ok else "✗"
            mark_style = "green" if action.ok else "red"
            icon = _action_icon(action.name)
            chip = _extract_path_chip(action)
            if chip:
                # File action: action name on top, chip indented below.
                action_node = tree.add(
                    Text.assemble(
                        (f"{icon} ", "dim"),
                        (action.name, "bold"),
                        ("  ", ""),
                        (mark, mark_style),
                        (f"  {_fmt_duration(action.duration_s)}", "dim"),
                    )
                )
                action_node.add(Text(chip, style="italic dim"))
            else:
                # Shell or other: show args inline with the action.
                args_brief = (action.args_preview or "").strip()
                if len(args_brief) > 60:
                    args_brief = args_brief[:57] + "..."
                tree.add(
                    Text.assemble(
                        (f"{icon} ", "dim"),
                        (action.name, "bold"),
                        ((f" · {args_brief}" if args_brief else ""), "dim"),
                        ("  ", ""),
                        (mark, mark_style),
                        (f"  {_fmt_duration(action.duration_s)}", "dim"),
                    )
                )
        # Done footer with totals — Claude.ai parity (Image #9).
        total_dur = sum(a.duration_s for a in turn.tool_actions)
        tree.add(
            Text.assemble(
                ("⊘ ", "dim green"),
                ("Done", "bold green"),
                (
                    f"  ·  {turn.action_count} action{s}"
                    f" in {_fmt_duration(total_dur)}",
                    "dim",
                ),
            )
        )
    else:
        tree.add(Text("(no tool actions)", style="italic dim"))

    return tree
```

- [ ] **Step 3: Update tests in `test_reasoning_store.py`.**

The existing tests check for "Reasoning:" prefix and specific arg formats. Update them to check for the new format:

```python
def test_render_turn_tree_v3_uses_semantic_icons():
    """v3: file-tools get 📄, shell-tools get ⚙️, others get 🔧."""
    import io
    from rich.console import Console

    store = ReasoningStore()
    store.append(
        thinking="x", duration_s=0.1,
        tool_actions=[
            ToolAction(name="Edit", args_preview="file_path=foo.py", ok=True, duration_s=0.05),
            ToolAction(name="Bash", args_preview="ls -la", ok=True, duration_s=0.02),
            ToolAction(name="WebFetch", args_preview="url=https://x.com", ok=True, duration_s=0.5),
        ],
    )
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(
        render_turn_tree(store.get_latest())
    )
    text = out.getvalue()
    assert "📄" in text  # Edit gets file icon
    assert "⚙️" in text  # Bash gets shell icon
    assert "🔧" in text  # WebFetch gets default icon


def test_render_turn_tree_v3_extracts_path_chip_from_file_tool_args():
    """v3: file tools display the path as an indented chip below."""
    import io
    from rich.console import Console

    store = ReasoningStore()
    store.append(
        thinking="x", duration_s=0.1,
        tool_actions=[
            ToolAction(name="Edit", args_preview="file_path=foo.md, content=hello", ok=True, duration_s=0.05),
        ],
    )
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(
        render_turn_tree(store.get_latest())
    )
    text = out.getvalue()
    assert "foo.md" in text


def test_render_turn_tree_v3_done_footer_with_totals():
    """v3: tree ends with '⊘ Done · K actions in X.Xs' row."""
    import io
    from rich.console import Console

    store = ReasoningStore()
    store.append(
        thinking="x", duration_s=0.1,
        tool_actions=[
            ToolAction(name="Edit", args_preview="path=a.py", ok=True, duration_s=0.05),
            ToolAction(name="Bash", args_preview="ls", ok=True, duration_s=0.02),
        ],
    )
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(
        render_turn_tree(store.get_latest())
    )
    text = out.getvalue()
    assert "Done" in text
    assert "2 actions" in text


def test_render_turn_tree_v3_summary_header_uses_chevron_down():
    """v3: when summary present, the tree header is the summary + ⌄
    (expanded chevron) instead of the metadata-bold layout."""
    import io
    from rich.console import Console

    store = ReasoningStore()
    store.append(thinking="x", duration_s=0.5, tool_actions=[])
    store.update_summary(turn_id=1, summary="Wrote a poem about sloths")
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=120).print(
        render_turn_tree(store.get_latest())
    )
    text = out.getvalue()
    assert "Wrote a poem about sloths" in text
    assert "⌄" in text  # expanded chevron
    # The verbose metadata moves out of the header in v3.
    # (turn-id and "Thought for" should NOT appear in the header line.)
```

Update the existing `test_render_turn_tree_returns_rich_tree_with_expected_nodes` to expect "Edit" + "foo.py" without the parenthesized "args=" form, and update `test_render_turn_tree_handles_no_thinking` / `_no_actions` similarly if their assertions are too tight.

- [ ] **Step 4: Run tests + commit.**

---

## Task 3: Full suite + lint + push + PR + merge

- [ ] **Step 1: Full pytest** + ruff. Expected: green.
- [ ] **Step 2: Manual smoke** — start `oc chat`, ask "write a haiku", confirm collapsed line is just `<summary> ›` and `/reasoning show` shows the v3 tree with semantic icons + chips + Done footer.
- [ ] **Step 3: CHANGELOG entry** — "Thinking History v3: Claude.ai visual parity".
- [ ] **Step 4: Push + open PR + wait CI green + merge.**

---

## Audit log

| # | Issue | Resolution |
|---|---|---|
| 1 | True click-to-toggle interactivity needs Textual. | Out of scope — chevron is visual hint only; existing `Ctrl+X Ctrl+R` and `/reasoning show` remain the actual triggers. Documented honestly. |
| 2 | Per-action AI-generated descriptions (Claude.ai's "Create a focused, stripped-down doc...") aren't extractable from tool args. | Out of scope — would require an LLM call per action. v3 uses tool name + extracted file path as the cheap-but-meaningful approximation. |
| 3 | Existing tests reference "Reasoning:" prefix and exact arg formats. Refactor will break them. | Tests are updated in Task 2 step 3 to match the new format. Old assertions removed deliberately. |
| 4 | Some chevron unicode chars (⌄, ›) might not render on all terminals. | Both are common terminal-renderable codepoints (curly arrow + single right-pointing angle quote). Fallback would be ASCII `>` but those work in 99%+ of modern terminals. |
| 5 | `_extract_path_chip` is best-effort; some args_previews may be too truncated. | Returns None when no key matches → tree falls through to args-inline format. Graceful degradation. |
| 6 | What if `thinking.splitlines()[0]` is very long? | Rich's Text wrapping handles overflow; the body lines still indent under the first one. Acceptable. |
