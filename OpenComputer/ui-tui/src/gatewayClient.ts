// Adapted for OpenComputer 2026-05-07 from hermes-agent/ui-tui
// Original: MIT License (c) 2025 Nous Research — see THIRD_PARTY_LICENSE_HERMES
//
// Wire client for the Ink TUI. Speaks JSON-RPC over WebSocket against
// opencomputer.gateway.wire_server (port 18789). Symmetric with the
// dashboard's lib/wire.ts so behavior is identical across surfaces.

import WebSocket from "ws";

export interface WireRequest { id: string; method: string; params?: unknown; }
export interface WireResponse { id: string; ok: boolean; payload?: unknown; error?: string; code?: string; }
export interface WireEvent { event: string; payload?: unknown; }

type EventHandler = (ev: WireEvent) => void;
type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void; };

export interface SlashCommand {
  name: string;
  description: string;
  aliases: string[];
}

export interface HelloResult {
  server: string;
  version: string;
  methods: string[];
  events: string[];
}

export class OCWireClient {
  private ws: WebSocket | null = null;
  private pending = new Map<string, Pending>();
  private listeners: EventHandler[] = [];
  private connectedListeners: ((ok: boolean) => void)[] = [];
  private url: string;
  private reconnectMs = 1000;
  private _connected = false;

  constructor(url: string = process.env.OC_WIRE_URL || "ws://127.0.0.1:18789") {
    this.url = url;
    this.connect();
  }

  get connected(): boolean { return this._connected; }
  get serverUrl(): string { return this.url; }

  onEvent(handler: EventHandler): () => void {
    this.listeners.push(handler);
    return () => {
      const i = this.listeners.indexOf(handler);
      if (i >= 0) this.listeners.splice(i, 1);
    };
  }

  onConnected(handler: (ok: boolean) => void): () => void {
    this.connectedListeners.push(handler);
    return () => {
      const i = this.connectedListeners.indexOf(handler);
      if (i >= 0) this.connectedListeners.splice(i, 1);
    };
  }

  call<T = unknown>(method: string, params: unknown = {}): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error("wire not connected"));
        return;
      }
      const id = String(Math.random()).slice(2);
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
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
      this._scheduleReconnect();
      return;
    }
    this.ws.on("open", () => {
      this.reconnectMs = 1000;
      this._connected = true;
      for (const h of this.connectedListeners) h(true);
    });
    this.ws.on("message", (raw: WebSocket.RawData) => {
      let m: WireResponse | WireEvent;
      try {
        m = JSON.parse(raw.toString()) as WireResponse | WireEvent;
      } catch {
        return;
      }
      if ("id" in m && this.pending.has(m.id)) {
        const cb = this.pending.get(m.id)!;
        this.pending.delete(m.id);
        if (m.ok) cb.resolve(m.payload);
        else cb.reject(new Error(m.error || "wire error"));
      } else if ("event" in m) {
        for (const h of this.listeners) h(m as WireEvent);
      }
    });
    this.ws.on("close", () => {
      this._connected = false;
      for (const h of this.connectedListeners) h(false);
      this._scheduleReconnect();
    });
    this.ws.on("error", () => {
      // onclose will fire
    });
  }

  private _scheduleReconnect(): void {
    if (this.reconnectMs > 60_000) return;
    setTimeout(() => this.connect(), this.reconnectMs);
    this.reconnectMs = Math.min(30_000, this.reconnectMs * 2);
  }

  // ----- Convenience method wrappers -----
  hello(): Promise<HelloResult> { return this.call("hello"); }
  chat(message: string, sessionId?: string): Promise<unknown> {
    return this.call("chat", { message, session_id: sessionId });
  }
  sessionsList(limit = 20): Promise<{ sessions: unknown[] }> {
    return this.call("sessions.list", { limit });
  }
  search(query: string, limit = 20): Promise<{ hits: unknown[] }> {
    return this.call("search", { query, limit });
  }
  skillsList(): Promise<{ skills: unknown[] }> {
    return this.call("skills.list");
  }
  steerSubmit(sessionId: string, prompt: string): Promise<unknown> {
    return this.call("steer.submit", { session_id: sessionId, prompt });
  }
  slashList(): Promise<{ commands: SlashCommand[] }> { return this.call("slash.list"); }
  slashDispatch(name: string, args = ""): Promise<{ output: string }> {
    return this.call("slash.dispatch", { name, args });
  }
}
