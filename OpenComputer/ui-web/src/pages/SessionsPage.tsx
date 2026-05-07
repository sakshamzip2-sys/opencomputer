import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useApi } from "@/hooks/useApi";
import { api, type ApiError } from "@/lib/api";

interface Session {
  id: string;
  title: string | null;
  platform: string;
  model: string | null;
  message_count: number;
  started_at: number | null;
  ended_at: number | null;
  vibe: string | null;
  goal_text: string | null;
}
interface SessionsResp { items: Session[]; limit: number; }

interface MessageRow {
  seq: number;
  id: number;
  role: string;
  content: string;
  tool_call_id: string | null;
  tool_calls: string | null;
  name: string | null;
  timestamp: number | null;
}
interface MessagesResp { items: MessageRow[]; limit: number; offset: number; total: number; }

function fmtTs(ts: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

export function SessionsPage() {
  const [q, setQ] = useState("");
  const path = q.trim()
    ? `/api/v1/sessions/search?q=${encodeURIComponent(q.trim())}&limit=100`
    : `/api/v1/sessions?limit=200`;
  const { data, error, loading, refetch } = useApi<SessionsResp>(path, [q]);

  async function onDelete(id: string) {
    if (!confirm(`Delete session "${id}"? This is irreversible.`)) return;
    try {
      await api(`/api/v1/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
      refetch();
    } catch (e) {
      alert(`Delete failed: ${(e as Error).message}`);
    }
  }

  return (
    <div className="p-6">
      <div className="mb-4 flex items-baseline gap-3">
        <h1 className="text-2xl font-semibold">Sessions</h1>
        {data && <span className="text-sm text-zinc-500">{data.items.length} shown</span>}
      </div>
      <input
        type="text"
        placeholder="Search messages (FTS5 syntax) — try `hello` or `error*`"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        className="mb-4 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm focus:border-cyan-400 focus:outline-none"
      />
      {loading && <p className="text-sm text-zinc-500">Loading…</p>}
      {error && (
        <p className="text-sm text-red-400">
          Error {error.status}: {error.message}
        </p>
      )}
      {data && data.items.length === 0 && (
        <p className="text-sm text-zinc-500">
          No sessions yet. Try <code className="rounded bg-zinc-800 px-1 py-0.5">oc chat hi</code>.
        </p>
      )}
      {data && data.items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b border-zinc-800 text-left text-zinc-400">
              <tr>
                <th className="py-2 pr-4">Title</th>
                <th className="py-2 pr-4">Platform</th>
                <th className="py-2 pr-4">Model</th>
                <th className="py-2 pr-4 text-right">Msgs</th>
                <th className="py-2 pr-4">Vibe</th>
                <th className="py-2 pr-4">Started</th>
                <th className="py-2 pr-4 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((s) => (
                <tr key={s.id} className="border-t border-zinc-800/50 hover:bg-zinc-900/50">
                  <td className="py-2 pr-4">
                    <Link
                      to={`/sessions/${encodeURIComponent(s.id)}`}
                      className="text-cyan-400 hover:underline"
                    >
                      {s.title || s.id}
                    </Link>
                  </td>
                  <td className="py-2 pr-4 text-zinc-300">{s.platform}</td>
                  <td className="py-2 pr-4 font-mono text-xs text-zinc-400">
                    {s.model ?? "—"}
                  </td>
                  <td className="py-2 pr-4 text-right text-zinc-300">{s.message_count}</td>
                  <td className="py-2 pr-4 text-zinc-400">{s.vibe ?? "—"}</td>
                  <td className="py-2 pr-4 text-xs text-zinc-500">{fmtTs(s.started_at)}</td>
                  <td className="py-2 pr-4 text-right">
                    <button
                      onClick={() => onDelete(s.id)}
                      className="text-xs text-red-400 hover:underline"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function SessionDetailPage() {
  const { id } = useParams<{ id: string }>();
  const meta = useApi<Session>(`/api/v1/sessions/${encodeURIComponent(id ?? "")}`, [id]);
  const msgs = useApi<MessagesResp>(
    `/api/v1/sessions/${encodeURIComponent(id ?? "")}/messages?limit=500`,
    [id],
  );

  return (
    <div className="p-6">
      <Link to="/sessions" className="text-sm text-cyan-400 hover:underline">
        ← All sessions
      </Link>
      {meta.loading && <p className="mt-4 text-zinc-500">Loading…</p>}
      {meta.error && <ErrorView err={meta.error} />}
      {meta.data && (
        <header className="mt-4 mb-6">
          <h1 className="text-2xl font-semibold">{meta.data.title || meta.data.id}</h1>
          <div className="mt-1 flex flex-wrap gap-x-4 text-sm text-zinc-400">
            <span>Platform <code className="text-zinc-300">{meta.data.platform}</code></span>
            {meta.data.model && (
              <span>Model <code className="font-mono text-xs text-zinc-300">{meta.data.model}</code></span>
            )}
            <span>Started {fmtTs(meta.data.started_at)}</span>
            <span>{meta.data.message_count} msgs</span>
          </div>
        </header>
      )}
      {msgs.loading && <p className="text-zinc-500">Loading messages…</p>}
      {msgs.error && <ErrorView err={msgs.error} />}
      {msgs.data && (
        <div className="space-y-4">
          {msgs.data.items.map((m) => (
            <div key={m.id} className="rounded border border-zinc-800 bg-zinc-900/50 p-3">
              <div className="mb-1 flex items-center gap-2 text-xs text-zinc-500">
                <span
                  className={
                    m.role === "user"
                      ? "rounded bg-cyan-900/40 px-1.5 py-0.5 text-cyan-300"
                      : m.role === "assistant"
                      ? "rounded bg-zinc-800 px-1.5 py-0.5 text-zinc-200"
                      : "rounded bg-amber-900/40 px-1.5 py-0.5 text-amber-300"
                  }
                >
                  {m.role}
                </span>
                {m.name && <span>{m.name}</span>}
                {m.timestamp && <span>{fmtTs(m.timestamp)}</span>}
              </div>
              <pre className="whitespace-pre-wrap break-words font-mono text-sm text-zinc-200">
                {m.content}
              </pre>
            </div>
          ))}
          {msgs.data.items.length === 0 && (
            <p className="text-sm text-zinc-500">No messages.</p>
          )}
        </div>
      )}
    </div>
  );
}

function ErrorView({ err }: { err: ApiError }) {
  return (
    <p className="mt-4 text-sm text-red-400">
      Error {err.status}: {err.message}
    </p>
  );
}
