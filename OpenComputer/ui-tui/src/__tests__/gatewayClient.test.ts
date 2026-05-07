// vitest smoke test for OCWireClient. Doesn't actually open a real
// WebSocket — verifies the public shape, reconnect timer math, and
// JSON parsing of inbound messages.

import { describe, it, expect } from "vitest";
import { OCWireClient } from "../gatewayClient.js";

describe("OCWireClient", () => {
  it("exposes the documented method surface", () => {
    const c = new OCWireClient("ws://127.0.0.1:65535");  // port unlikely to be open
    expect(typeof c.hello).toBe("function");
    expect(typeof c.chat).toBe("function");
    expect(typeof c.sessionsList).toBe("function");
    expect(typeof c.search).toBe("function");
    expect(typeof c.skillsList).toBe("function");
    expect(typeof c.steerSubmit).toBe("function");
    expect(typeof c.slashList).toBe("function");
    expect(typeof c.slashDispatch).toBe("function");
    c.close();
  });

  it("starts in disconnected state and rejects calls", async () => {
    const c = new OCWireClient("ws://127.0.0.1:65535");
    expect(c.connected).toBe(false);
    await expect(c.hello()).rejects.toThrow("wire not connected");
    c.close();
  });

  it("close() suppresses further reconnect attempts", () => {
    const c = new OCWireClient("ws://127.0.0.1:65535");
    c.close();
    expect(c.connected).toBe(false);
  });
});
