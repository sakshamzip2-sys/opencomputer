#!/usr/bin/env node
// Adapted for OpenComputer 2026-05-07 from hermes-agent/ui-tui
// Original: MIT License (c) 2025 Nous Research

import { render } from "ink";
import { App } from "./app.js";
import { OCWireClient } from "./gatewayClient.js";

const url = process.env.OC_WIRE_URL || "ws://127.0.0.1:18789";
const client = new OCWireClient(url);

// OC_TUI_RESUME mirrors hermes-agent's HERMES_TUI_RESUME env contract.
// Values:
//   "" / unset       — start fresh (session picker behaviour as before)
//   "last"           — auto-resume the most recent session
//   "<session-id>"   — auto-resume that specific session (or session-id prefix)
// The Python wrapper (cli_tui.py) sets this from the user's
// OPENCOMPUTER_TUI_RESUME env var or `oc tui --continue` / `oc tui --resume <id>`.
const resumeSpec = process.env.OC_TUI_RESUME || "";

const { waitUntilExit, unmount } = render(<App client={client} resumeSpec={resumeSpec} />);

const cleanup = () => {
  client.close();
  unmount();
};
process.on("SIGINT", cleanup);
process.on("SIGTERM", cleanup);

waitUntilExit().then(() => {
  client.close();
  process.exit(0);
});
