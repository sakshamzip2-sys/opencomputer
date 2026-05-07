// WireClient — WebSocket JSON-RPC against opencomputer.gateway.wire_server.
// Symmetric with OpenComputer/ui-tui/src/gatewayClient.ts (port — same shape).

export interface WireRequest { id: string; method: string; params?: unknown; }
export interface WireResponse { id: string; ok: boolean; payload?: unknown; error?: string; code?: string; }
export interface WireEvent { event: string; payload?: unknown; }

type EventHandler = (ev: WireEvent) => void;

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void; };

export class WireClient {
  private ws: WebSocket | null = null;
  private pending = new Map<string, Pending>();
  private listeners: EventHandler[] = [];
  private url: string;
  private reconnectMs = 1000;
  private connectedListeners: ((ok: boolean) => void)[] = [];
  private _connected = false;

  constructor(url: string) {
    this.url = url;
    this.connect();
  }

  get connected(): boolean { return this._connected; }

  onConnected(handler: (ok: boolean) => void): () => void {
    this.connectedListeners.push(handler);
    return () => {
      const i = this.connectedListeners.indexOf(handler);
      if (i >= 0) this.connectedListeners.splice(i, 1);
    };
  }

  onEvent(handler: EventHandler): () => void {
    this.listeners.push(handler);
    return () => {
      const i = this.listeners.indexOf(handler);
      if (i >= 0) this.listeners.splice(i, 1);
    };
  }

  call<T>(method: string, params: unknown = {}): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error("wire not connected"));
        return;
      }
      const id = crypto.randomUUID();
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  close(): void {
    this.reconnectMs = 999_999_999; // suppress reconnect
    this.ws?.close();
    this.ws = null;
    this._connected = false;
  }

  private connect(): void {
    try {
      this.ws = new WebSocket(this.url);
    } catch (e) {
      this._scheduleReconnect();
      return;
    }
    this.ws.onopen = () => {
      this.reconnectMs = 1000;
      this._connected = true;
      this.connectedListeners.forEach((h) => h(true));
    };
    this.ws.onmessage = (e: MessageEvent) => {
      let m: WireResponse | WireEvent;
      try { m = JSON.parse(e.data) as WireResponse | WireEvent; }
      catch { return; }
      if ("id" in m) {
        const cb = this.pending.get(m.id);
        if (cb) {
          this.pending.delete(m.id);
          if (m.ok) cb.resolve(m.payload);
          else cb.reject(new Error(m.error || "wire error"));
        }
      } else if ("event" in m) {
        this.listeners.forEach((h) => h(m as WireEvent));
      }
    };
    this.ws.onclose = () => {
      this._connected = false;
      this.connectedListeners.forEach((h) => h(false));
      this._scheduleReconnect();
    };
    this.ws.onerror = () => {
      // ignored — onclose will fire
    };
  }

  private _scheduleReconnect(): void {
    if (this.reconnectMs > 60_000) return; // give up after escalation
    setTimeout(() => this.connect(), this.reconnectMs);
    this.reconnectMs = Math.min(30_000, this.reconnectMs * 2);
  }

  // ----- Convenience method wrappers -----
  hello(): Promise<{ server: string; version: string; methods: string[]; events: string[] }> {
    return this.call("hello");
  }
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
  slashList(): Promise<{ commands: { name: string; description: string; aliases: string[] }[] }> {
    return this.call("slash.list");
  }
  slashDispatch(name: string, args = ""): Promise<{ output: string }> {
    return this.call("slash.dispatch", { name, args });
  }
}

let _singleton: WireClient | null = null;
export function getWire(): WireClient {
  if (_singleton) return _singleton;
  const meta = document.querySelector<HTMLMetaElement>('meta[name="oc-wire-url"]');
  let url = meta?.content || "ws://127.0.0.1:18789";
  if (url.includes("__WIRE_URL__")) url = "ws://127.0.0.1:18789";
  _singleton = new WireClient(url);
  return _singleton;
}
