// OpenComputer TUI — wire-protocol constants + types (TypeScript source).
//
// TUI-parity Milestone 2 (spec: docs/superpowers/specs/2026-05-17-tui-parity/
// TUI.md). The M1 audit found OC shipped only a compiled `dist/` with no
// source in the repo; this file + wireClient.ts are the start of OC's
// first real TUI source tree.
//
// This MIRRORS the Python wire definitions — keep in sync with:
//   opencomputer/gateway/protocol.py      (METHOD_* / EVENT_* constants)
//   opencomputer/gateway/protocol_v2.py   (typed param / result schemas)
// The Python test tests/test_ui_tui_wire_client.py enforces that every
// METHOD_* value in protocol.py has a constant here — the client can
// never silently lag the server.

// ─── RPC method names (client → server) ────────────────────────────

export const METHOD = {
  HELLO: "hello",
  CHAT: "chat",
  SESSION_LIST: "sessions.list",
  SEARCH: "search",
  SKILLS_LIST: "skills.list",
  STEER_SUBMIT: "steer.submit",
  SLASH_LIST: "slash.list",
  SLASH_DISPATCH: "slash.dispatch",
  PERMISSION_RESPONSE: "permission.response",
  MEMORY_STATUS: "memory.status",
  EVOLUTION_STATUS: "evolution.status",
  // TUI-parity M1 batches 1-8 — the overlay backend surface.
  SESSION_RESUME: "session.resume",
  SESSION_DELETE: "session.delete",
  MODEL_OPTIONS: "model.options",
  CONFIG_GET: "config.get",
  MODEL_SET: "model.set",
  CONFIG_SET: "config.set",
  SESSION_RENAME: "session.rename",
  SESSION_USAGE: "session.usage",
  SUBAGENTS_LIST: "subagents.list",
  SESSION_MOST_RECENT: "session.most_recent",
  SKILL_SHOW: "skill.show",
  SESSION_FORK: "session.fork",
  SESSION_INTERRUPT: "session.interrupt",
  TOOLS_LIST: "tools.list",
  CHECKPOINTS_LIST: "checkpoints.list",
  CHECKPOINTS_DELETE: "checkpoints.delete",
} as const;

export type MethodName = (typeof METHOD)[keyof typeof METHOD];

// ─── Event names (server → client) ─────────────────────────────────

export const EVENT = {
  TURN_BEGIN: "turn.begin",
  TURN_END: "turn.end",
  TOOL_CALL: "tool.call",
  TOOL_RESULT: "tool.result",
  ASSISTANT_MESSAGE: "assistant.message",
  ERROR: "error",
  PERMISSION_REQUEST: "permission.request",
  MEMORY_WRITE: "memory.write",
  EVOLUTION_TUNING_CHANGED: "evolution.tuning_changed",
  STREAM_RETRY: "stream.retry",
  PROFILE_SWAP: "profile.swap",
} as const;

export type EventName = (typeof EVENT)[keyof typeof EVENT];

// ─── Wire envelope shapes ───────────────────────────────────────────

export interface WireResponse<P = unknown> {
  type: "res";
  id: string;
  ok: boolean;
  payload?: P;
  error?: string;
  code?: string;
}

export interface WireServerEvent<P = unknown> {
  type: "event";
  event: EventName | string;
  payload: P;
  seq?: number;
}

// ─── Result payload types (mirror protocol_v2.py) ───────────────────

export interface HelloResult {
  server: string;
  version?: string;
  methods: string[];
  events: string[];
  gap_warning?: boolean;
  server_last_event_seq?: number | null;
}

export interface SessionRow {
  id: string;
  title?: string;
  started_at?: number;
  message_count?: number;
  preview?: string;
  source?: string;
}

export interface SessionListResult {
  sessions: SessionRow[];
}

export interface TranscriptMessage {
  role: string;
  text?: string;
  name?: string | null;
}

export interface SessionResumeResult {
  session_id: string;
  info: Record<string, unknown>;
  messages: TranscriptMessage[];
  message_count: number;
}

export interface SessionDeleteResult {
  deleted: string;
  found: boolean;
}

export interface ModelProviderOption {
  name: string;
  models: string[];
  is_current: boolean;
}

export interface ModelOptionsResult {
  model: string | null;
  provider: string | null;
  providers: ModelProviderOption[];
}

export interface ConfigGetResult {
  key: string;
  value: unknown;
  found: boolean;
}

export interface ModelSetResult {
  provider: string;
  model: string;
  ok: boolean;
}

export interface ConfigSetResult {
  key: string;
  value: unknown;
  ok: boolean;
}

export interface SessionRenameResult {
  session_id: string;
  title: string;
  ok: boolean;
}

export interface SessionUsageResult {
  session_id: string;
  found: boolean;
  model?: string | null;
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  compactions_count?: number;
  cost_usd?: number | null;
  started_at?: number | null;
  ended_at?: number | null;
}

export interface SubagentInfo {
  agent_id: string;
  goal: string;
  state: string;
  display_state: string;
  role: string;
  depth: number;
  started_at: string;
  parent_session_id?: string | null;
  child_session_id?: string | null;
  ended_at?: string | null;
  error?: string | null;
}

export interface SubagentsListResult {
  subagents: SubagentInfo[];
}

export interface SessionMostRecentResult {
  found: boolean;
  session_id?: string | null;
  title?: string | null;
  started_at?: number | null;
  source?: string | null;
}

export interface SkillShowResult {
  skill_id: string;
  body: string;
  found: boolean;
}

export interface SessionForkResult {
  source_session_id: string;
  new_session_id: string;
  messages_copied: number;
  ok: boolean;
}

export interface SessionInterruptResult {
  session_id: string;
  ok: boolean;
}

export interface ToolInfo {
  name: string;
  description: string;
}

export interface ToolsListResult {
  tools: ToolInfo[];
}

export interface CheckpointInfo {
  id: string;
  session_id: string;
  prompt_index: number;
  label: string;
  created_at: number;
  message_count: number;
}

export interface CheckpointsListResult {
  checkpoints: CheckpointInfo[];
}

export interface CheckpointsDeleteResult {
  checkpoint_id: string;
  found: boolean;
}

export interface SlashCommandInfo {
  name: string;
  description: string;
  aliases?: string[];
}

export interface SlashListResult {
  commands: SlashCommandInfo[];
}

export interface SlashDispatchResult {
  output: string;
  side_effects?: Record<string, unknown>;
}
