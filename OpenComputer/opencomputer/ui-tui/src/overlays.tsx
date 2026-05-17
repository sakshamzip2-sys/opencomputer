// OpenComputer TUI — overlay components (TypeScript source).
//
// TUI-parity Milestone 2. Presentational overlay panels for the app's
// overlay state machine. Each is pure render: app.tsx owns data fetching,
// the selected index, and all keyboard input (Ink's useInput is held by
// app.tsx so only one handler is live). These mirror SessionPicker.
//
// Driven by the M1 wire methods: model.options/set, skills.list/skill.show,
// config.get/set, subagents.list, checkpoints.list/delete, tools.list.

import React from "react";
import { Box, Text } from "ink";

import type { CheckpointInfo, SubagentInfo, ToolInfo } from "./protocol.js";

// A flat (provider, model) row — app.tsx flattens ModelOptionsResult.
export interface ModelRow {
  provider: string;
  model: string;
  isCurrent: boolean;
}

export interface SkillRow {
  id: string;
  name: string;
  description: string;
}

export interface ConfigRow {
  key: string;
  value: string;
}

const PANEL = { borderStyle: "round", marginBottom: 1 } as const;

/** Shared empty-state line so every overlay reads consistently. */
function Empty({ label }: { label: string }): React.ReactElement {
  return <Text color="gray">{`  (${label})`}</Text>;
}

/**
 * Collapse whitespace and clip to ``n`` columns with an ellipsis.
 *
 * Skill descriptions in particular run to full paragraphs with embedded
 * newlines; rendered raw they wrap the overlay into a wall of text. Every
 * variable-length cell goes through this so a panel row stays one line.
 */
function clip(s: string, n: number): string {
  const flat = s.replace(/\s+/g, " ").trim();
  return flat.length > n ? `${flat.slice(0, n - 1)}…` : flat;
}

// ─── model picker ───────────────────────────────────────────────────

export function ModelPickerOverlay({
  rows,
  index,
}: {
  rows: ModelRow[];
  index: number;
}): React.ReactElement {
  return (
    <Box {...PANEL} flexDirection="column" borderColor="cyan">
      <Text color="cyan"> Model picker — ↑↓ move · Enter select · ESC close</Text>
      {rows.length === 0 && <Empty label="no models in the registry" />}
      {rows.slice(0, 15).map((r, i) => (
        <Text key={`${r.provider}/${r.model}`} color={i === index ? "cyan" : "white"}>
          {(i === index ? "› " : "  ") +
            `${r.provider} / ${r.model}` +
            (r.isCurrent ? "  (current)" : "")}
        </Text>
      ))}
    </Box>
  );
}

// ─── skills hub ─────────────────────────────────────────────────────

export function SkillsHubOverlay({
  skills,
  index,
  preview,
}: {
  skills: SkillRow[];
  index: number;
  preview: string;
}): React.ReactElement {
  const selected = skills[index];
  return (
    <Box {...PANEL} flexDirection="column" borderColor="cyan">
      <Text color="cyan"> Skills hub — ↑↓ move · Enter preview · ESC close</Text>
      {skills.length === 0 && <Empty label="no skills found" />}
      {skills.slice(0, 12).map((s, i) => (
        <Text key={s.id} color={i === index ? "cyan" : "white"}>
          {(i === index ? "› " : "  ") + s.name}
          <Text color="gray">{"  — " + clip(s.description, 60)}</Text>
        </Text>
      ))}
      {selected && preview && (
        <Box flexDirection="column" marginTop={1}>
          <Text color="gray">{`  ── ${selected.name} ──`}</Text>
          {preview
            .split("\n")
            .slice(0, 8)
            .map((line, i) => (
              <Text key={i} color="gray">
                {"  " + clip(line, 92)}
              </Text>
            ))}
        </Box>
      )}
    </Box>
  );
}

// ─── settings panel ─────────────────────────────────────────────────

export function SettingsOverlay({
  entries,
  index,
}: {
  entries: ConfigRow[];
  index: number;
}): React.ReactElement {
  return (
    <Box {...PANEL} flexDirection="column" borderColor="cyan">
      <Text color="cyan"> Settings — ↑↓ move · ESC close (read-only view)</Text>
      {entries.length === 0 && <Empty label="no config values" />}
      {entries.map((e, i) => (
        <Text key={e.key} color={i === index ? "cyan" : "white"}>
          {(i === index ? "› " : "  ") + e.key}
          <Text color="gray">{" = " + e.value}</Text>
        </Text>
      ))}
    </Box>
  );
}

// ─── agents overlay ─────────────────────────────────────────────────

export function AgentsOverlay({
  subagents,
  index,
}: {
  subagents: SubagentInfo[];
  index: number;
}): React.ReactElement {
  const stateColor = (s: string): string =>
    s === "running"
      ? "yellow"
      : s === "completed"
        ? "green"
        : s === "orphaned"
          ? "red"
          : "gray";
  return (
    <Box {...PANEL} flexDirection="column" borderColor="cyan">
      <Text color="cyan"> Subagents — ↑↓ move · ESC close</Text>
      {subagents.length === 0 && <Empty label="no subagents spawned" />}
      {subagents.slice(0, 15).map((a, i) => (
        <Text key={a.agent_id} color={i === index ? "cyan" : "white"}>
          {(i === index ? "› " : "  ") + a.goal.slice(0, 48)}
          <Text color={stateColor(a.display_state)}>{"  [" + a.display_state + "]"}</Text>
        </Text>
      ))}
    </Box>
  );
}

// ─── rollback overlay ───────────────────────────────────────────────

export function RollbackOverlay({
  checkpoints,
  index,
}: {
  checkpoints: CheckpointInfo[];
  index: number;
}): React.ReactElement {
  return (
    <Box {...PANEL} flexDirection="column" borderColor="cyan">
      <Text color="cyan"> Checkpoints — ↑↓ move · Del delete · ESC close</Text>
      {checkpoints.length === 0 && <Empty label="no checkpoints for this session" />}
      {checkpoints.slice(0, 15).map((c, i) => (
        <Text key={c.id} color={i === index ? "cyan" : "white"}>
          {(i === index ? "› " : "  ") + (c.label || c.id.slice(0, 12))}
          <Text color="gray">{`  (turn ${c.prompt_index}, ${c.message_count} msgs)`}</Text>
        </Text>
      ))}
    </Box>
  );
}

// ─── permission prompt ──────────────────────────────────────────────

export function PermissionPrompt({
  capabilityId,
  context,
  scope,
}: {
  capabilityId: string;
  context: string;
  scope?: string | null;
}): React.ReactElement {
  return (
    <Box {...PANEL} flexDirection="column" borderColor="yellow">
      <Text color="yellow"> Permission needed — the agent is paused</Text>
      <Text>{`  capability: ${capabilityId}`}</Text>
      {scope ? <Text color="gray">{`  scope: ${scope}`}</Text> : null}
      {context ? <Text color="gray">{`  ${clip(context, 92)}`}</Text> : null}
      <Text color="cyan">
        {"  [a] allow once   [A] allow always   [d] deny"}
      </Text>
    </Box>
  );
}

// ─── tools inspector ────────────────────────────────────────────────

export function ToolsOverlay({
  tools,
  index,
}: {
  tools: ToolInfo[];
  index: number;
}): React.ReactElement {
  return (
    <Box {...PANEL} flexDirection="column" borderColor="cyan">
      <Text color="cyan"> Tools — ↑↓ move · ESC close</Text>
      {tools.length === 0 && <Empty label="no tools registered" />}
      {tools.slice(0, 15).map((t, i) => (
        <Text key={t.name} color={i === index ? "cyan" : "white"}>
          {(i === index ? "› " : "  ") + t.name}
          <Text color="gray">{" — " + t.description}</Text>
        </Text>
      ))}
    </Box>
  );
}
