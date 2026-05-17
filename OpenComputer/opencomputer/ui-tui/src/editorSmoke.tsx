// OpenComputer TUI — multiline editor smoke check.
//
// Drives the useEditor hook with real keystrokes (typed input, Ctrl+N
// newline, backspace) via ink-testing-library's stdin and prints the
// resulting buffer state. Verifies the editor genuinely EDITS — typecheck
// only proves it compiles.
//
// Driven by tests/test_ui_tui_integration.py. A build/test artifact.

import type { ReactElement } from "react";
import { Box, Text, useInput } from "ink";
import { render } from "ink-testing-library";

import { useEditor } from "./editor.js";

function Probe(): ReactElement {
  const ed = useEditor();
  useInput((raw, key) => {
    ed.onKey(raw, key);
  });
  return (
    <Box flexDirection="column">
      {ed.lines.map((line, r) => (
        <Text key={r}>{`[${line}]`}</Text>
      ))}
      <Text>{`rows=${ed.lines.length} row=${ed.cursorRow} col=${ed.cursorCol}`}</Text>
    </Box>
  );
}

const { stdin, lastFrame, unmount } = render(<Probe />);

const pause = (ms: number): Promise<void> =>
  new Promise((res) => setTimeout(res, ms));

async function run(): Promise<void> {
  await pause(120); // let Ink mount and register useInput before typing
  stdin.write("hello");
  await pause(40);
  stdin.write("\x0e"); // Ctrl+N → newline
  await pause(40);
  stdin.write("world");
  await pause(40);
  stdin.write("\x7f"); // backspace → drops the trailing 'd'
  await pause(60);

  const frame = lastFrame() ?? "";
  unmount();
  process.stdout.write(`FRAME_START\n${frame}\nFRAME_END\n`);
  process.exit(0);
}

void run();
