import { useState } from "react";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";

interface Job {
  id: string;
  name: string;
  schedule: string;
  command: string;
  enabled: boolean;
  last_run: number | null;
  next_run: number | null;
}
interface JobsResp { items: Job[]; }

export function CronPage() {
  const list = useApi<JobsResp>("/api/v1/cron/jobs");
  const [name, setName] = useState("");
  const [schedule, setSchedule] = useState("0 9 * * *");
  const [command, setCommand] = useState("");

  async function create() {
    if (!name || !schedule || !command) return;
    try {
      await api("/api/v1/cron/jobs", {
        method: "POST",
        body: JSON.stringify({ name, schedule, command, enabled: true }),
      });
      setName(""); setCommand("");
      list.refetch();
    } catch (e) { alert((e as Error).message); }
  }

  async function action(id: string, op: "pause" | "resume" | "trigger") {
    try {
      await api(`/api/v1/cron/jobs/${encodeURIComponent(id)}/${op}`, { method: "POST" });
      list.refetch();
    } catch (e) { alert((e as Error).message); }
  }

  async function del(id: string) {
    if (!confirm(`Delete job ${id}?`)) return;
    try {
      await api(`/api/v1/cron/jobs/${encodeURIComponent(id)}`, { method: "DELETE" });
      list.refetch();
    } catch (e) { alert((e as Error).message); }
  }

  return (
    <div className="p-6">
      <h1 className="mb-4 text-2xl font-semibold">Cron Jobs</h1>
      <div className="mb-4 grid grid-cols-1 gap-2 sm:grid-cols-4">
        <input
          placeholder="name" value={name} onChange={(e) => setName(e.target.value)}
          className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm"
        />
        <input
          placeholder="schedule (cron)" value={schedule} onChange={(e) => setSchedule(e.target.value)}
          className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm font-mono"
        />
        <input
          placeholder="command" value={command} onChange={(e) => setCommand(e.target.value)}
          className="sm:col-span-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm"
        />
        <button
          onClick={create}
          disabled={!name || !command}
          className="rounded border border-cyan-700 bg-cyan-950/40 px-3 py-1 text-sm text-cyan-300 hover:bg-cyan-900/40 disabled:opacity-50"
        >
          Create
        </button>
      </div>
      {list.loading && <p className="text-zinc-500">Loading…</p>}
      {list.error && <p className="text-red-400">Error: {list.error.message}</p>}
      {list.data && list.data.items.length === 0 && (
        <p className="text-sm text-zinc-500">No cron jobs.</p>
      )}
      {list.data && list.data.items.length > 0 && (
        <table className="w-full text-sm">
          <thead className="text-left text-zinc-400">
            <tr>
              <th className="py-2 pr-4">Name</th>
              <th className="py-2 pr-4">Schedule</th>
              <th className="py-2 pr-4">Command</th>
              <th className="py-2 pr-4">Enabled</th>
              <th className="py-2 pr-4 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {list.data.items.map((j) => (
              <tr key={j.id} className="border-t border-zinc-800/50">
                <td className="py-2 pr-4">{j.name}</td>
                <td className="py-2 pr-4 font-mono text-xs">{j.schedule}</td>
                <td className="py-2 pr-4 font-mono text-xs text-zinc-400 truncate max-w-[200px]">{j.command}</td>
                <td className="py-2 pr-4">{j.enabled ? "✓" : "—"}</td>
                <td className="py-2 pr-4 text-right text-xs space-x-3">
                  <button onClick={() => action(j.id, j.enabled ? "pause" : "resume")} className="text-cyan-400 hover:underline">{j.enabled ? "Pause" : "Resume"}</button>
                  <button onClick={() => action(j.id, "trigger")} className="text-cyan-400 hover:underline">Trigger</button>
                  <button onClick={() => del(j.id)} className="text-red-400 hover:underline">Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
