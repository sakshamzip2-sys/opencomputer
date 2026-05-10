// Pure-logic tests for the memory panel formatting helpers.
// React rendering is intentionally not tested — convention in this
// project is logic-only vitest (see gatewayClient.test.ts).

import { describe, it, expect } from "vitest";
import {
  capPercent,
  statusColor,
  statusTag,
  formatStatusLine,
  seedFromStatusEntry,
  sortedEntries,
  type MemoryWritePayload,
  type MemoryStatusEntryShape,
} from "../components/memoryPanel.js";

const base = (overrides: Partial<MemoryWritePayload> = {}): MemoryWritePayload => ({
  action: "append",
  target: "MEMORY.md",
  content_size: 1000,
  cap_limit: 4000,
  compaction_delta: 0,
  dropped_paragraphs: 0,
  ...overrides,
});

describe("capPercent", () => {
  it("rounds down for friendly display", () => {
    expect(capPercent(base({ content_size: 3504 }))).toBe(87);
  });

  it("returns 0 on empty file", () => {
    expect(capPercent(base({ content_size: 0 }))).toBe(0);
  });

  it("can exceed 100 mid-compaction", () => {
    expect(capPercent(base({ content_size: 5000 }))).toBe(125);
  });

  it("guards against zero cap (defensive)", () => {
    expect(capPercent(base({ cap_limit: 0 }))).toBe(0);
  });
});

describe("statusColor", () => {
  it("is gray below 80%", () => {
    expect(statusColor(base({ content_size: 1000 }))).toBe("gray");
  });

  it("is yellow at 80% threshold", () => {
    expect(statusColor(base({ content_size: 3200 }))).toBe("yellow");
  });

  it("escalates to red on any compaction drop, regardless of pct", () => {
    expect(
      statusColor(base({ content_size: 100, dropped_paragraphs: 1 }))
    ).toBe("red");
  });
});

describe("statusTag", () => {
  it("uses the action verb on a clean write", () => {
    expect(statusTag(base({ action: "replace" }))).toBe("replace");
  });

  it("uses singular noun for one drop", () => {
    expect(statusTag(base({ dropped_paragraphs: 1 }))).toContain("1 entry");
  });

  it("uses plural noun for multiple drops", () => {
    expect(statusTag(base({ dropped_paragraphs: 2 }))).toContain("2 entries");
  });

  it("includes the COMPACTED keyword the user looks for", () => {
    expect(statusTag(base({ dropped_paragraphs: 5 }))).toContain("COMPACTED");
  });
});

describe("formatStatusLine", () => {
  it("includes target, pct, and size on a normal write", () => {
    const line = formatStatusLine(
      base({ target: "USER.md", cap_limit: 2000, content_size: 1785 })
    );
    expect(line).toContain("USER.md");
    expect(line).toContain("89%");
    expect(line).toContain("1785/2000");
  });

  it("escalates clearly on compaction", () => {
    const line = formatStatusLine(
      base({ content_size: 3480, dropped_paragraphs: 2 })
    );
    expect(line).toContain("COMPACTED");
    expect(line).toContain("dropped 2");
  });

  it("uses 'idle' tag for seeded initial-state entries (no action yet)", () => {
    // After memory.status RPC seed, no write has happened so action="".
    // The panel should still render usefully — "idle" describes the state.
    const line = formatStatusLine(base({ action: "" }));
    expect(line).toContain("idle");
  });
});

describe("seedFromStatusEntry", () => {
  // Adapter from the wire memory.status entry shape into the panel's
  // MemoryWritePayload shape. Must zero-fill the action / compaction
  // fields so the panel renders an unambiguous "no recent write" line.
  it("zero-fills compaction and action for fresh seed", () => {
    const entry: MemoryStatusEntryShape = {
      target: "MEMORY.md",
      content_size: 2821,
      cap_limit: 4000,
      pct: 0.7053,
      paragraph_count: 5,
    };
    const seeded = seedFromStatusEntry(entry);
    expect(seeded.action).toBe("");
    expect(seeded.compaction_delta).toBe(0);
    expect(seeded.dropped_paragraphs).toBe(0);
    expect(seeded.target).toBe("MEMORY.md");
    expect(seeded.content_size).toBe(2821);
    expect(seeded.cap_limit).toBe(4000);
  });

  it("preserves cap_limit so the panel renders correct percentages", () => {
    // USER.md uses 2000 not 4000 — adapter must not synthesize the wrong cap.
    const entry: MemoryStatusEntryShape = {
      target: "USER.md",
      content_size: 1785,
      cap_limit: 2000,
      pct: 0.8925,
      paragraph_count: 4,
    };
    const seeded = seedFromStatusEntry(entry);
    expect(seeded.cap_limit).toBe(2000);
    expect(capPercent(seeded)).toBe(89);
  });
});

describe("sortedEntries", () => {
  it("returns entries alphabetically by target — MEMORY before USER", () => {
    const entries: Record<string, MemoryWritePayload> = {
      "USER.md": base({ target: "USER.md", cap_limit: 2000, content_size: 100 }),
      "MEMORY.md": base({ target: "MEMORY.md" }),
    };
    const sorted = sortedEntries(entries);
    expect(sorted.map((e) => e.target)).toEqual(["MEMORY.md", "USER.md"]);
  });

  it("returns empty array for empty record (panel renders nothing)", () => {
    expect(sortedEntries({})).toEqual([]);
  });

  it("is stable across repeated calls", () => {
    const entries: Record<string, MemoryWritePayload> = {
      "USER.md": base({ target: "USER.md" }),
      "MEMORY.md": base({ target: "MEMORY.md" }),
    };
    const a = sortedEntries(entries).map((e) => e.target);
    const b = sortedEntries(entries).map((e) => e.target);
    expect(a).toEqual(b);
  });
});
