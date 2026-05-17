// OpenComputer TUI — chat-loop integration harness.
//
// Mounts the real <App> against a SCRIPTED mock wire server (URL from
// argv) and drives a full conversation: tab-completes a slash command,
// sends a message, watches the streamed markdown + tool call + tool
// result + memory event, answers a permission prompt, sees the retry
// banner, queues a follow-up while busy, then scrolls back and recalls
// history.
//
// Exercises every conversation-loop fix the unit smokes can't.
// Driven by tests/test_ui_tui_chat_loop.py. A build/test artifact.

import { render } from "ink-testing-library";

import { App } from "./app.js";
import { OCWireClient } from "./wireClient.js";

const wsUrl = process.argv[2] ?? "ws://127.0.0.1:18789";
const client = new OCWireClient(wsUrl);
const { stdin, lastFrame } = render(<App client={client} />);

const pause = (ms: number): Promise<void> =>
  new Promise((res) => setTimeout(res, ms));

function emit(label: string): void {
  process.stdout.write(`<<${label}>>\n${lastFrame() ?? ""}\n`);
}

async function run(): Promise<void> {
  await pause(900); // connect + hello
  emit("CONNECTED");

  // Tab-completion: type a prefix, Tab completes it from the palette.
  stdin.write("/mod");
  await pause(150);
  stdin.write("\t");
  await pause(200);
  emit("TAB");
  stdin.write("\x1b"); // ESC clears the composer
  await pause(150);

  // Turn 1 — the scripted turn (tool calls, memory event, permission).
  stdin.write("hello there");
  await pause(200);
  stdin.write("\r");
  await pause(950);
  emit("MID_TURN");

  stdin.write("a"); // allow_once → permission.response
  await pause(300);
  emit("RETRY"); // retry banner is visible in this window

  // Queue a follow-up while the turn is still running.
  stdin.write("a queued question");
  await pause(150);
  stdin.write("\r");
  await pause(220);
  emit("QUEUED");

  await pause(1900); // turn 1 ends → queued message drains → turn 2
  emit("AFTER");

  stdin.write("\x1b[5~"); // PageUp
  await pause(250);
  emit("SCROLLED");

  stdin.write("\x1b[A"); // up arrow → recall input history
  await pause(250);
  emit("HISTORY");

  process.stdout.write("<<END>>\n");
  process.exit(0);
}

void run();
