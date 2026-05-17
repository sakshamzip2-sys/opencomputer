// OpenComputer TUI — multiline editor (TypeScript source).
//
// TUI-parity Milestone 2. A real composer: insert-at-cursor editing,
// cursor movement (←→ within a line, ↑↓ across lines), backspace that
// joins lines, and Ctrl+N to insert a newline (Ctrl+J is avoided — it IS
// the linefeed byte, so terminals/Ink read it as Enter). Mirrors
// hermes-agent's textInput.tsx + editor.ts.
//
// `text` and `cursor` are ONE state object, mutated only through
// functional `setBuf` updaters. That is load-bearing: Ink can deliver
// several keystrokes within a single render tick, and a non-functional
// update would insert every one of them at the *stale* cursor, garbling
// the buffer. The functional updater always sees the latest text+cursor
// together, so the invariant "cursor indexes into text" can't break.

import { useCallback, useState } from "react";
import type { Key } from "ink";

interface Buf {
  text: string;
  cursor: number; // flat index into text; 0..text.length
}

export interface Editor {
  /** Current buffer contents (may contain "\n"). */
  text: string;
  /** ``text`` split on newlines — what the view renders. */
  lines: string[];
  /** Cursor position, derived from the flat index. */
  cursorRow: number;
  cursorCol: number;
  /** Feed a keystroke. Returns true if it was an editing key (consumed). */
  onKey: (input: string, key: Key) => boolean;
  /** Replace the whole buffer (e.g. history recall); cursor parks at end. */
  setText: (next: string) => void;
  /** Empty the buffer. */
  clear: () => void;
}

/** Byte offsets where each line begins. Always length >= 1. */
function lineStarts(t: string): number[] {
  const starts = [0];
  for (let i = 0; i < t.length; i++) {
    if (t[i] === "\n") starts.push(i + 1);
  }
  return starts;
}

/** Resolve a flat cursor index to (row, col). */
function rowCol(text: string, cursor: number): { row: number; col: number } {
  const starts = lineStarts(text);
  let row = 0;
  for (let i = 0; i < starts.length; i++) {
    if (cursor >= starts[i]!) row = i;
  }
  return { row, col: cursor - starts[row]! };
}

/** Move the cursor vertically by ``dir`` (±1), preserving column. */
function moveVertical(b: Buf, dir: number): Buf {
  const starts = lineStarts(b.text);
  const lines = b.text.split("\n");
  const { row, col } = rowCol(b.text, b.cursor);
  const target = row + dir;
  if (target < 0 || target >= lines.length) return b; // at an edge — no-op
  const targetLen = (lines[target] ?? "").length;
  return { text: b.text, cursor: starts[target]! + Math.min(col, targetLen) };
}

export function useEditor(): Editor {
  const [buf, setBuf] = useState<Buf>({ text: "", cursor: 0 });

  const { row, col } = rowCol(buf.text, buf.cursor);
  const lines = buf.text.split("\n");

  const setText = useCallback((next: string) => {
    setBuf({ text: next, cursor: next.length });
  }, []);

  const clear = useCallback(() => {
    setBuf({ text: "", cursor: 0 });
  }, []);

  // Mutations all run through functional `setBuf` updaters, so they never
  // see a stale buffer. The ↑↓ EDGE decision (am I on the first/last
  // line?) needs the current row, so onKey depends on `row`/`lineCount` —
  // that only governs the boolean return, never an insert position, so it
  // can't reintroduce the stale-cursor corruption.
  const lineCount = lines.length;
  const onKey = useCallback(
    (input: string, key: Key): boolean => {
      // Enter is the app's call (submit vs. nothing) — not an editing key.
      if (key.return) return false;

      if (key.ctrl && input === "n") {
        setBuf((b) => ({
          text: b.text.slice(0, b.cursor) + "\n" + b.text.slice(b.cursor),
          cursor: b.cursor + 1,
        }));
        return true;
      }
      if (key.leftArrow) {
        setBuf((b) => ({ ...b, cursor: Math.max(0, b.cursor - 1) }));
        return true;
      }
      if (key.rightArrow) {
        setBuf((b) => ({ ...b, cursor: Math.min(b.text.length, b.cursor + 1) }));
        return true;
      }
      if (key.upArrow) {
        // On the first line ↑ isn't ours — decline it so the app can
        // recall input history.
        if (row === 0) return false;
        setBuf((b) => moveVertical(b, -1));
        return true;
      }
      if (key.downArrow) {
        if (row >= lineCount - 1) return false; // last line — decline
        setBuf((b) => moveVertical(b, 1));
        return true;
      }
      if (key.backspace || key.delete) {
        setBuf((b) =>
          b.cursor > 0
            ? {
                text: b.text.slice(0, b.cursor - 1) + b.text.slice(b.cursor),
                cursor: b.cursor - 1,
              }
            : b,
        );
        return true;
      }
      // Printable input — insert at the cursor. Excludes control chords.
      if (input && !key.ctrl && !key.meta && !key.escape) {
        setBuf((b) => ({
          text: b.text.slice(0, b.cursor) + input + b.text.slice(b.cursor),
          cursor: b.cursor + input.length,
        }));
        return true;
      }
      return false;
    },
    [row, lineCount],
  );

  return {
    text: buf.text,
    lines,
    cursorRow: row,
    cursorCol: col,
    onKey,
    setText,
    clear,
  };
}
