#!/usr/bin/env node
// Adapted for OpenComputer 2026-05-07 from hermes-agent/ui-tui
// Original: MIT License (c) 2025 Nous Research

import { render } from "ink";
import { App } from "./app.js";
import { OCWireClient } from "./gatewayClient.js";

const url = process.env.OC_WIRE_URL || "ws://127.0.0.1:18789";
const client = new OCWireClient(url);

const { waitUntilExit, unmount } = render(<App client={client} />);

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
