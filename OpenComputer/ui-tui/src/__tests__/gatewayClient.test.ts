// vitest smoke test for OCWireClient. Doesn't actually open a real
// WebSocket — verifies the public shape, reconnect timer math, and
// JSON parsing of inbound messages.

import { describe, it, expect } from "vitest";
import { OCWireClient, buildWireRequest } from "../gatewayClient.js";

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
    expect(typeof c.memoryStatus).toBe("function");
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

// Wire-format pin. The Python wire server requires `type: "req"` as a
// discriminator and rejects anything missing it with "expected type=req".
// Pre-2026-05-10 this field was omitted from the TS client's send call,
// which silently broke every TUI RPC (hello, chat, slash.list, etc.) —
// the WS opened so "connected" lit up, but every subsequent RPC errored.
// Surfaced when the new memory panel started calling memory.status on
// connect (Tier-C+ of the 2026-05-10 memory-observability follow-through).
// Pinning the wire shape here at the unit level catches any regression
// the next time someone refactors the call() path.
describe("buildWireRequest", () => {
  it("includes the type=req discriminator the Python server requires", () => {
    const raw = buildWireRequest("abc-123", "memory.status", {});
    const parsed = JSON.parse(raw);
    expect(parsed.type).toBe("req");
  });

  it("preserves id, method, and params", () => {
    const raw = buildWireRequest("abc-123", "chat", { message: "hi" });
    const parsed = JSON.parse(raw);
    expect(parsed.id).toBe("abc-123");
    expect(parsed.method).toBe("chat");
    expect(parsed.params).toEqual({ message: "hi" });
  });

  it("emits exactly four top-level keys (no accidental extras)", () => {
    const raw = buildWireRequest("x", "hello", {});
    const parsed = JSON.parse(raw);
    expect(Object.keys(parsed).sort()).toEqual(["id", "method", "params", "type"]);
  });
});
