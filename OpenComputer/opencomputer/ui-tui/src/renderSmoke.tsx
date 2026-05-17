// OpenComputer TUI — render smoke check.
//
// Mounts <App> with a wire client pointed at a dead port (so it never
// connects) and prints the first rendered frame. Verifies the Ink app
// genuinely RENDERS — typecheck + bundle prove it compiles; this proves
// the component tree mounts and produces output without throwing.
//
// Driven by tests/test_ui_tui_integration.py. Not part of the shipped
// TUI — a build/test artifact.

import { render } from "ink-testing-library";

import { App } from "./app.js";
import { OCWireClient } from "./wireClient.js";

// Port 9 is "discard" — nothing listens, the client stays disconnected,
// so <App> renders its initial (disconnected) frame deterministically.
const client = new OCWireClient("ws://127.0.0.1:9");
const { lastFrame, unmount } = render(<App client={client} />);

setTimeout(() => {
  const frame = lastFrame() ?? "";
  unmount();
  client.close();
  process.stdout.write(`FRAME_START\n${frame}\nFRAME_END\n`);
  process.exit(0);
}, 300);
