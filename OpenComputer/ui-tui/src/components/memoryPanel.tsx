// Tier-C of 2026-05-10 memory-observability design — surfaces the in-process
// MemoryWriteEvent over the wire so the user sees compaction in real time.
// Subscribes to the OCWireClient event stream, filters for "memory.write",
// renders one status line per declarative-memory file under the chat header.
//
// Tier-C+ (2026-05-10) extension: panel state is keyed by `target` so both
// MEMORY.md and USER.md render simultaneously rather than the last-written
// file replacing the other. Initial state seeded by `client.memoryStatus()`
// on connect; subsequent memory.write events update the matching entry.
//
// No emojis introduced here beyond mirroring the existing in-band warning
// in opencomputer/agent/memory_cap.py — same UX, second surface.

import React from "react";
import { Box, Text } from "ink";
import { theme } from "../theme.js";

/**
 * Wire shape of a memory.write event payload — a superset of the wire
 * memory.status entry (those have no `action` / `compaction_delta` /
 * `dropped_paragraphs`; see `seedFromStatusEntry` for the upgrade path).
 */
export interface MemoryWritePayload {
  action: string;            // "append" | "replace" | "remove" | "" if seeded
  target: string;            // "MEMORY.md" | "USER.md"
  content_size: number;      // post-write byte count
  cap_limit: number;         // 4000 for MEMORY.md, 2000 for USER.md
  compaction_delta: number;  // bytes freed by compaction (0 if none / seeded)
  dropped_paragraphs: number;// paragraphs dropped (0 if none / seeded)
}

/**
 * Wire shape from `memory.status` RPC — narrower than MemoryWritePayload
 * because seeding from disk has no associated action/compaction event.
 */
export interface MemoryStatusEntryShape {
  target: string;
  content_size: number;
  cap_limit: number;
  pct: number;
  paragraph_count: number;
}

// ─── Pure formatting + adapter helpers (testable in isolation) ──────

/**
 * Promote a `memory.status` entry into the panel's MemoryWritePayload
 * shape. Action defaults to empty string (no event), compaction fields
 * default to zero (no compaction occurred). The panel renders a clean
 * "no recent action" line for these initial-state entries.
 */
export function seedFromStatusEntry(entry: MemoryStatusEntryShape): MemoryWritePayload {
  return {
    action: "",
    target: entry.target,
    content_size: entry.content_size,
    cap_limit: entry.cap_limit,
    compaction_delta: 0,
    dropped_paragraphs: 0,
  };
}

/** 0..100+, rounded down so 87.6% renders as "87%". */
export function capPercent(payload: MemoryWritePayload): number {
  if (payload.cap_limit <= 0) return 0;
  return Math.floor((payload.content_size / payload.cap_limit) * 100);
}

/** Color tier for the status line. red on drop, yellow at ≥80%, gray below. */
export function statusColor(payload: MemoryWritePayload): string {
  if (payload.dropped_paragraphs > 0) return theme.error;
  if (capPercent(payload) >= 80) return theme.tool;
  return theme.muted;
}

/** Right-hand tag — compaction status > action verb > nothing (initial-state). */
export function statusTag(payload: MemoryWritePayload): string {
  if (payload.dropped_paragraphs > 0) {
    const noun = payload.dropped_paragraphs === 1 ? "entry" : "entries";
    return `🛑 COMPACTED — dropped ${payload.dropped_paragraphs} ${noun}`;
  }
  if (payload.action) return payload.action;
  return "idle";
}

/** Full single-line status string for one file. */
export function formatStatusLine(payload: MemoryWritePayload): string {
  const pct = capPercent(payload);
  return `memory: ${payload.target} ${pct}% (${payload.content_size}/${payload.cap_limit}) · ${statusTag(payload)}`;
}

/**
 * Stable rendering order — alphabetical by target name. Matches the
 * server-side ordering in `_collect_memory_status` so the wire and UI
 * agree on which file appears first (MEMORY.md before USER.md).
 */
export function sortedEntries(entries: Record<string, MemoryWritePayload>): MemoryWritePayload[] {
  return Object.values(entries).sort((a, b) => (a.target < b.target ? -1 : a.target > b.target ? 1 : 0));
}

// ─── React component ────────────────────────────────────────────────

interface Props {
  /**
   * Map of target filename → latest known status. Empty until the panel
   * receives either an initial `memory.status` RPC response or a
   * `memory.write` event.
   */
  entries: Record<string, MemoryWritePayload>;
}

export const MemoryPanel: React.FC<Props> = ({ entries }) => {
  const list = sortedEntries(entries);
  if (list.length === 0) return null;
  return (
    <Box flexDirection="column">
      {list.map((payload) => (
        <Text key={payload.target} color={statusColor(payload)}>
          {formatStatusLine(payload)}
        </Text>
      ))}
    </Box>
  );
};
