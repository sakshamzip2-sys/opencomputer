import { useEffect, useRef, useState } from "react";

interface LogEntry {
  seq: number;
  ts: number;
  level: string;
  logger: string;
  msg: string;
}

const LEVEL_COLORS: Record<string, string> = {
  ERROR: "text-red-400",
  WARNING: "text-amber-400",
  INFO: "text-zinc-300",
  DEBUG: "text-zinc-500",
};

export function LogsPage() {
  const [level, setLevel] = useState<string>("ALL");
  const [paused, setPaused] = useState(false);
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const esRef = useRef<EventSource | null>(null);
  const seenSeq = useRef<number>(-1);

  useEffect(() => {
    if (paused) return;
    const url =
      level === "ALL"
        ? `/api/v1/logs?since=${seenSeq.current + 1}`
        : `/api/v1/logs?level=${level}&since=${seenSeq.current + 1}`;
    const es = new EventSource(url);
    esRef.current = es;
    es.addEventListener("log", (e: MessageEvent) => {
      try {
        const entry = JSON.parse(e.data) as LogEntry;
        seenSeq.current = Math.max(seenSeq.current, entry.seq);
        setEntries((prev) => [...prev.slice(-499), entry]);
      } catch {
        // swallow
      }
    });
    es.onerror = () => {
      // Browser will auto-reconnect; nothing to do
    };
    return () => {
      es.close();
      esRef.current = null;
    };
  }, [level, paused]);

  return (
    <div className="flex h-full flex-col p-6">
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-2xl font-semibold">Logs</h1>
        <select
          value={level}
          onChange={(e) => {
            setLevel(e.target.value);
            setEntries([]);
            seenSeq.current = -1;
          }}
          className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm"
        >
          {["ALL", "DEBUG", "INFO", "WARNING", "ERROR"].map((lv) => (
            <option key={lv} value={lv}>{lv}</option>
          ))}
        </select>
        <button
          onClick={() => setPaused((p) => !p)}
          className="rounded border border-zinc-700 px-2 py-1 text-sm hover:bg-zinc-800"
        >
          {paused ? "Resume" : "Pause"}
        </button>
        <button
          onClick={() => {
            setEntries([]);
            seenSeq.current = -1;
          }}
          className="rounded border border-zinc-700 px-2 py-1 text-sm hover:bg-zinc-800"
        >
          Clear
        </button>
        <span className="ml-auto text-xs text-zinc-500">
          {entries.length} lines · {paused ? "paused" : "live"}
        </span>
      </div>
      <pre className="flex-1 overflow-auto rounded border border-zinc-800 bg-zinc-950 p-3 text-xs font-mono">
        {entries.length === 0 ? (
          <span className="text-zinc-500">Waiting for log events…</span>
        ) : (
          entries.map((e) => (
            <div key={e.seq} className={LEVEL_COLORS[e.level] ?? "text-zinc-300"}>
              <span className="text-zinc-600">
                {new Date(e.ts * 1000).toLocaleTimeString()}
              </span>{" "}
              <span className="text-zinc-600">[{e.level}]</span>{" "}
              <span className="text-zinc-500">{e.logger}</span>: {e.msg}
            </div>
          ))
        )}
      </pre>
    </div>
  );
}
