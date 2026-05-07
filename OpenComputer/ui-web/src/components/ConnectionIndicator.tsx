import { useEffect, useState } from "react";

export function ConnectionIndicator({ wireUrl }: { wireUrl?: string }) {
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!wireUrl) return;
    let ws: WebSocket | null = null;
    let cancelled = false;
    let timer: number | undefined;

    const probe = () => {
      try {
        ws = new WebSocket(wireUrl);
      } catch {
        setConnected(false);
        return;
      }
      ws.onopen = () => {
        if (!cancelled) setConnected(true);
        ws?.close();
      };
      ws.onerror = () => {
        if (!cancelled) setConnected(false);
        ws?.close();
      };
      ws.onclose = () => {
        ws = null;
      };
    };

    probe();
    timer = window.setInterval(probe, 10_000);

    return () => {
      cancelled = true;
      ws?.close();
      if (timer) window.clearInterval(timer);
    };
  }, [wireUrl]);

  return (
    <span
      className={`flex items-center gap-1.5 ${connected ? "text-green-400" : "text-red-400"}`}
      title={connected ? "wire reachable" : "wire unreachable"}
    >
      <span className="h-2 w-2 rounded-full bg-current" />
      {connected ? "live" : "down"}
    </span>
  );
}
