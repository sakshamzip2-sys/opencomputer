import { useEffect, useRef, useState } from "react";
import { getWire, type WireEvent } from "@/lib/wire";

interface Turn {
  role: "user" | "assistant";
  text: string;
}

export function ChatPage() {
  const wire = useRef(getWire());
  const [connected, setConnected] = useState(wire.current.connected);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [streamBuf, setStreamBuf] = useState("");

  useEffect(() => {
    const offConn = wire.current.onConnected(setConnected);
    const offEv = wire.current.onEvent((ev: WireEvent) => {
      const payload = (ev.payload ?? {}) as Record<string, unknown>;
      if (ev.event === "assistant.message" || ev.event === "turn.assistant") {
        const txt = String(payload.text ?? payload.content ?? "");
        setStreamBuf((prev) => prev + txt);
      } else if (ev.event === "turn.end") {
        setStreamBuf((prev) => {
          if (prev) setTurns((t) => [...t, { role: "assistant", text: prev }]);
          return "";
        });
        setBusy(false);
      } else if (ev.event === "error") {
        setTurns((t) => [
          ...t,
          { role: "assistant", text: `[error] ${String(payload.error ?? "")}` },
        ]);
        setBusy(false);
      }
    });
    return () => { offConn(); offEv(); };
  }, []);

  async function send() {
    const msg = input.trim();
    if (!msg || busy || !connected) return;
    setTurns((t) => [...t, { role: "user", text: msg }]);
    setInput("");
    setBusy(true);
    setStreamBuf("");
    try {
      await wire.current.chat(msg);
    } catch (e) {
      setTurns((t) => [
        ...t,
        { role: "assistant", text: `[wire error] ${(e as Error).message}` },
      ]);
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full flex-col p-6">
      <div className="mb-3 flex items-baseline gap-3">
        <h1 className="text-2xl font-semibold">Chat</h1>
        <span
          className={`text-xs ${connected ? "text-green-400" : "text-red-400"}`}
          title={connected ? "wire server connected" : "wire server disconnected"}
        >
          ● {connected ? "connected" : "disconnected"}
        </span>
      </div>
      {!connected && (
        <p className="mb-3 rounded border border-amber-900 bg-amber-950/40 px-3 py-2 text-sm text-amber-300">
          Wire server not reachable at <code>ws://127.0.0.1:18789</code>. Start it
          with <code className="bg-zinc-800 px-1">oc gateway</code> in another
          terminal — this page reconnects automatically.
        </p>
      )}
      <div className="mb-4 flex-1 space-y-3 overflow-auto rounded border border-zinc-800 bg-zinc-950 p-3">
        {turns.length === 0 && (
          <p className="text-sm text-zinc-500">No messages yet.</p>
        )}
        {turns.map((t, i) => (
          <div
            key={i}
            className={`rounded p-2 text-sm ${
              t.role === "user"
                ? "bg-cyan-950/30 text-cyan-100"
                : "bg-zinc-900 text-zinc-200"
            }`}
          >
            <div className="mb-1 text-xs uppercase tracking-wide text-zinc-500">
              {t.role}
            </div>
            <pre className="whitespace-pre-wrap break-words font-mono">{t.text}</pre>
          </div>
        ))}
        {streamBuf && (
          <div className="rounded bg-zinc-900 p-2 text-sm text-zinc-300">
            <div className="mb-1 text-xs uppercase tracking-wide text-zinc-500">
              assistant (streaming)
            </div>
            <pre className="whitespace-pre-wrap break-words font-mono">{streamBuf}</pre>
          </div>
        )}
      </div>
      <div className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && (e.preventDefault(), send())}
          placeholder={connected ? "Type a message…" : "Waiting for wire server…"}
          disabled={!connected || busy}
          className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm focus:border-cyan-400 focus:outline-none disabled:opacity-50"
        />
        <button
          onClick={send}
          disabled={!connected || busy || !input.trim()}
          className="rounded border border-cyan-700 bg-cyan-950/40 px-4 py-2 text-sm text-cyan-300 hover:bg-cyan-900/40 disabled:opacity-50"
        >
          {busy ? "Sending…" : "Send"}
        </button>
      </div>
    </div>
  );
}
