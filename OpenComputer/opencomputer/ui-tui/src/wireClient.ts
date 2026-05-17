// OpenComputer TUI — WebSocket JSON-RPC wire client (TypeScript source).
//
// Adapted for OpenComputer from hermes-agent/ui-tui.
// Original: MIT License (c) 2025 Nous Research — see THIRD_PARTY_LICENSE_HERMES.
//
// TUI-parity Milestone 2. Speaks the JSON-RPC-over-WebSocket protocol
// served by opencomputer.gateway.wire_server (default ws://127.0.0.1:18789).
// The compiled artifact ships at ui-tui/dist/gatewayClient.js — this is its
// source. Symmetric with the dashboard SPA's lib/wire.ts.
//
// The M1 batches grew the wire from 11 RPC methods to 27; this client
// exposes a typed wrapper for every one. tests/test_ui_tui_wire_client.py
// enforces full coverage so the client can never silently lag the server.

import WebSocket from "ws";

import {
  METHOD,
  type CheckpointsDeleteResult,
  type CheckpointsListResult,
  type ConfigGetResult,
  type ConfigSetResult,
  type HelloResult,
  type ModelOptionsResult,
  type ModelSetResult,
  type SessionDeleteResult,
  type SessionForkResult,
  type SessionInterruptResult,
  type SessionListResult,
  type SessionMostRecentResult,
  type SessionRenameResult,
  type SessionResumeResult,
  type SessionUsageResult,
  type SkillShowResult,
  type SlashDispatchResult,
  type SlashListResult,
  type SubagentsListResult,
  type ToolsListResult,
  type WireServerEvent,
} from "./protocol.js";

type EventHandler = (ev: WireServerEvent) => void;
type ConnectedHandler = (connected: boolean) => void;

interface PendingCall {
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
}

/**
 * Wire client for the Ink TUI. One instance per `oc tui` process.
 *
 * Reconnects automatically with exponential backoff. RPC calls made
 * while disconnected reject immediately rather than queueing — the TUI
 * surfaces that as a transient error and the user retries.
 */
export class OCWireClient {
  private ws: WebSocket | null = null;
  private readonly pending = new Map<string, PendingCall>();
  private readonly listeners: EventHandler[] = [];
  private readonly connectedListeners: ConnectedHandler[] = [];
  private readonly url: string;
  private reconnectMs = 1000;
  private _connected = false;

  constructor(url: string = process.env.OC_WIRE_URL || "ws://127.0.0.1:18789") {
    this.url = url;
    this.connect();
  }

  get connected(): boolean {
    return this._connected;
  }

  get serverUrl(): string {
    return this.url;
  }

  /** Subscribe to server-pushed events. Returns an unsubscribe fn. */
  onEvent(handler: EventHandler): () => void {
    this.listeners.push(handler);
    return () => {
      const i = this.listeners.indexOf(handler);
      if (i >= 0) this.listeners.splice(i, 1);
    };
  }

  /** Subscribe to connect/disconnect transitions. Returns an unsubscribe fn. */
  onConnected(handler: ConnectedHandler): () => void {
    this.connectedListeners.push(handler);
    return () => {
      const i = this.connectedListeners.indexOf(handler);
      if (i >= 0) this.connectedListeners.splice(i, 1);
    };
  }

  /** Low-level JSON-RPC call. Prefer the typed wrappers below. */
  call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error("wire not connected"));
        return;
      }
      const id = String(Math.random()).slice(2);
      this.pending.set(id, {
        resolve: (v) => resolve(v as T),
        reject,
      });
      this.ws.send(JSON.stringify({ type: "req", id, method, params }));
    });
  }

  close(): void {
    this.reconnectMs = Number.MAX_SAFE_INTEGER; // suppress reconnect
    this.ws?.close();
    this.ws = null;
    this._connected = false;
  }

  private connect(): void {
    try {
      this.ws = new WebSocket(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws.on("open", () => {
      this.reconnectMs = 1000;
      this._connected = true;
      for (const h of this.connectedListeners) h(true);
    });
    this.ws.on("message", (raw: WebSocket.RawData) => {
      let m: Record<string, unknown>;
      try {
        m = JSON.parse(raw.toString());
      } catch {
        return;
      }
      if ("id" in m && this.pending.has(m.id as string)) {
        const cb = this.pending.get(m.id as string)!;
        this.pending.delete(m.id as string);
        if (m.ok) cb.resolve(m.payload);
        else cb.reject(new Error((m.error as string) || "wire error"));
      } else if ("event" in m) {
        for (const h of this.listeners) h(m as unknown as WireServerEvent);
      }
    });
    this.ws.on("close", () => {
      this._connected = false;
      for (const h of this.connectedListeners) h(false);
      this.scheduleReconnect();
    });
    this.ws.on("error", () => {
      // 'close' fires next; reconnect is handled there.
    });
  }

  private scheduleReconnect(): void {
    if (this.reconnectMs > 60_000) return;
    setTimeout(() => this.connect(), this.reconnectMs);
    this.reconnectMs = Math.min(30_000, this.reconnectMs * 2);
  }

  // ───────────────────────────────────────────────────────────────
  //  Typed RPC wrappers — one per server method (protocol.py METHOD_*)
  // ───────────────────────────────────────────────────────────────

  hello(sessionId?: string, lastEventSeq?: number): Promise<HelloResult> {
    return this.call<HelloResult>(METHOD.HELLO, {
      client: "opencomputer-tui",
      session_id: sessionId,
      last_event_seq: lastEventSeq,
    });
  }

  chat(message: string, sessionId?: string): Promise<unknown> {
    return this.call(METHOD.CHAT, { message, session_id: sessionId });
  }

  sessionsList(limit = 20): Promise<SessionListResult> {
    return this.call<SessionListResult>(METHOD.SESSION_LIST, { limit });
  }

  search(query: string, limit = 20): Promise<unknown> {
    return this.call(METHOD.SEARCH, { query, limit });
  }

  skillsList(): Promise<unknown> {
    return this.call(METHOD.SKILLS_LIST);
  }

  steerSubmit(sessionId: string, prompt: string): Promise<unknown> {
    return this.call(METHOD.STEER_SUBMIT, { session_id: sessionId, prompt });
  }

  slashList(): Promise<SlashListResult> {
    return this.call<SlashListResult>(METHOD.SLASH_LIST);
  }

  slashDispatch(name: string, args = ""): Promise<SlashDispatchResult> {
    return this.call<SlashDispatchResult>(METHOD.SLASH_DISPATCH, {
      name,
      args,
    });
  }

  permissionResponse(
    requestId: string,
    sessionId: string,
    capabilityId: string,
    decision: "allow_once" | "allow_always" | "deny",
  ): Promise<unknown> {
    return this.call(METHOD.PERMISSION_RESPONSE, {
      request_id: requestId,
      session_id: sessionId,
      capability_id: capabilityId,
      decision,
    });
  }

  memoryStatus(): Promise<unknown> {
    return this.call(METHOD.MEMORY_STATUS);
  }

  evolutionStatus(): Promise<unknown> {
    return this.call(METHOD.EVOLUTION_STATUS);
  }

  // ── M1 batch 1 — session lifecycle ──
  sessionResume(sessionId: string): Promise<SessionResumeResult> {
    return this.call<SessionResumeResult>(METHOD.SESSION_RESUME, {
      session_id: sessionId,
    });
  }

  sessionDelete(sessionId: string): Promise<SessionDeleteResult> {
    return this.call<SessionDeleteResult>(METHOD.SESSION_DELETE, {
      session_id: sessionId,
    });
  }

  // ── M1 batch 2 — settings read ──
  modelOptions(): Promise<ModelOptionsResult> {
    return this.call<ModelOptionsResult>(METHOD.MODEL_OPTIONS);
  }

  configGet(key: string): Promise<ConfigGetResult> {
    return this.call<ConfigGetResult>(METHOD.CONFIG_GET, { key });
  }

  // ── M1 batch 3 — settings write ──
  modelSet(provider: string, model: string): Promise<ModelSetResult> {
    return this.call<ModelSetResult>(METHOD.MODEL_SET, { provider, model });
  }

  configSet(key: string, value: unknown): Promise<ConfigSetResult> {
    return this.call<ConfigSetResult>(METHOD.CONFIG_SET, { key, value });
  }

  // ── M1 batch 4 — session metadata ──
  sessionRename(sessionId: string, title: string): Promise<SessionRenameResult> {
    return this.call<SessionRenameResult>(METHOD.SESSION_RENAME, {
      session_id: sessionId,
      title,
    });
  }

  sessionUsage(sessionId: string): Promise<SessionUsageResult> {
    return this.call<SessionUsageResult>(METHOD.SESSION_USAGE, {
      session_id: sessionId,
    });
  }

  // ── M1 batch 5 — subagents + recent ──
  subagentsList(limit = 50, runningOnly = false): Promise<SubagentsListResult> {
    return this.call<SubagentsListResult>(METHOD.SUBAGENTS_LIST, {
      limit,
      running_only: runningOnly,
    });
  }

  sessionMostRecent(): Promise<SessionMostRecentResult> {
    return this.call<SessionMostRecentResult>(METHOD.SESSION_MOST_RECENT);
  }

  // ── M1 batch 6 — skill preview + fork ──
  skillShow(skillId: string): Promise<SkillShowResult> {
    return this.call<SkillShowResult>(METHOD.SKILL_SHOW, { skill_id: skillId });
  }

  sessionFork(
    sessionId: string,
    title = "",
    recordParent = false,
  ): Promise<SessionForkResult> {
    return this.call<SessionForkResult>(METHOD.SESSION_FORK, {
      session_id: sessionId,
      title,
      record_parent: recordParent,
    });
  }

  // ── M1 batch 7 — interrupt + tools ──
  sessionInterrupt(sessionId: string): Promise<SessionInterruptResult> {
    return this.call<SessionInterruptResult>(METHOD.SESSION_INTERRUPT, {
      session_id: sessionId,
    });
  }

  toolsList(): Promise<ToolsListResult> {
    return this.call<ToolsListResult>(METHOD.TOOLS_LIST);
  }

  // ── M1 batch 8 — prompt checkpoints ──
  checkpointsList(sessionId: string, limit = 50): Promise<CheckpointsListResult> {
    return this.call<CheckpointsListResult>(METHOD.CHECKPOINTS_LIST, {
      session_id: sessionId,
      limit,
    });
  }

  checkpointsDelete(checkpointId: string): Promise<CheckpointsDeleteResult> {
    return this.call<CheckpointsDeleteResult>(METHOD.CHECKPOINTS_DELETE, {
      checkpoint_id: checkpointId,
    });
  }
}
