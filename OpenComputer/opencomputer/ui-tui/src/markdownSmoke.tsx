// OpenComputer TUI — markdown render smoke check.
//
// Renders <Markdown> with a sample document covering every construct
// (heading, fenced code, bold, inline code, bullet + numbered lists) and
// prints the frame. Verifies the streaming-safe renderer produces output.
//
// Driven by tests/test_ui_tui_integration.py. A build/test artifact.

import { render } from "ink-testing-library";

import { Markdown } from "./markdown.js";

const sample = [
  "# Heading One",
  "",
  "Some **bold words** and `inline code` here.",
  "",
  "- first bullet",
  "- second bullet",
  "",
  "```",
  "const answer = 42;",
  "```",
  "",
  "1. numbered item",
].join("\n");

const { lastFrame, unmount } = render(<Markdown text={sample} />);

setTimeout(() => {
  const frame = lastFrame() ?? "";
  unmount();
  process.stdout.write(`FRAME_START\n${frame}\nFRAME_END\n`);
  process.exit(0);
}, 200);
