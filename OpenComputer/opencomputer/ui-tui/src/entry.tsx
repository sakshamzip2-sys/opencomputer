// OpenComputer TUI — entrypoint (TypeScript source).
//
// Adapted for OpenComputer from hermes-agent/ui-tui.
// Original: MIT License (c) 2025 Nous Research — see THIRD_PARTY_LICENSE_HERMES.
//
// Launched by `oc tui`. Builds the wire client, renders the Ink app.
//   OC_WIRE_URL    — gateway WebSocket URL (default ws://127.0.0.1:18789)
//   OC_TUI_RESUME  — "last", a session id/prefix, or unset for a fresh chat

import { render } from "ink";

import { App } from "./app.js";
import { OCWireClient } from "./wireClient.js";

const client = new OCWireClient(process.env.OC_WIRE_URL);
const resumeSpec = process.env.OC_TUI_RESUME ?? "";

const app = render(<App client={client} resumeSpec={resumeSpec} />);

// Clean shutdown: close the socket once Ink unmounts (ESC / Ctrl+C).
void app.waitUntilExit().then(() => {
  client.close();
});
