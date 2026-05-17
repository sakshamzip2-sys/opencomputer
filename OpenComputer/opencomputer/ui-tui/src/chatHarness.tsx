// OpenComputer TUI — chat-loop integration harness.
//
// Mounts the real <App> against a SCRIPTED mock wire server (URL from
// argv) and drives a full turn: send a message, watch the streamed
// markdown + tool call + tool result, answer a permission prompt, see
// the retry banner, then scroll back and recall input history.
//
// Exercises the conversation-loop fixes the unit smokes can't:
// tool.result, permission.request, stream.retry, scrollback, history.
//
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

  stdin.write("hello there"); // type a message
  await pause(200);
  stdin.write("\r"); // send → mock scripts the turn
  await pause(900); // turn.begin → tool calls → tool.result → permission.request
  emit("MID_TURN"); // tool call + result + the permission prompt

  stdin.write("a"); // allow_once → app sends permission.response
  await pause(450);
  emit("RETRY"); // mock emits stream.retry before finishing

  await pause(1300); // mock finishes: assistant.message + turn.end
  emit("AFTER"); // final markdown reply, prompt + retry cleared

  stdin.write("\x1b[5~"); // PageUp
  await pause(250);
  emit("SCROLLED");

  stdin.write("\x1b[A"); // up arrow at an empty composer → recall history
  await pause(250);
  emit("HISTORY");

  process.stdout.write("<<END>>\n");
  process.exit(0);
}

void run();
