// OpenComputer TUI — full-app integration harness.
//
// Mounts the REAL <App> against a REAL wire server (URL from argv) via
// ink-testing-library, then drives it with real keystrokes — typing a
// slash command, Enter, ESC — and prints the frame at each stage.
//
// This is the test that exercises the integrated glue the unit smokes
// don't: App mount -> wire connect -> useInput -> editor buffer -> send()
// -> slash routing -> openOverlay -> a live RPC -> overlay render.
//
// Driven by tests/test_ui_tui_integration.py. A build/test artifact.

import { render } from "ink-testing-library";

import { App } from "./app.js";
import { OCWireClient } from "./wireClient.js";

const wsUrl = process.argv[2] ?? "ws://127.0.0.1:18789";
const client = new OCWireClient(wsUrl);
const { stdin, lastFrame, unmount } = render(<App client={client} />);

const pause = (ms: number): Promise<void> =>
  new Promise((res) => setTimeout(res, ms));

function emit(label: string, frame: string): void {
  process.stdout.write(`<<${label}>>\n${frame}\n`);
}

async function run(): Promise<void> {
  await pause(900); // mount + WS connect + hello handshake
  emit("CONNECTED", lastFrame() ?? "");

  stdin.write("/tools"); // type a client-side slash command
  await pause(200);
  emit("TYPED", lastFrame() ?? "");

  stdin.write("\r"); // Enter → send() → openOverlay("tools") → tools.list RPC
  await pause(700);
  emit("OVERLAY", lastFrame() ?? "");

  stdin.write("\x1b"); // ESC → close the overlay
  await pause(250);
  emit("CLOSED", lastFrame() ?? "");

  process.stdout.write("<<END>>\n");
  unmount();
  client.close();
  process.exit(0);
}

void run();
