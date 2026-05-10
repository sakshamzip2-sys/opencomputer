import { useState } from "react";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";

interface SkillRow {
  name: string;
  version: string;
  description: string;
  enabled: boolean;
  path: string;
}
interface SkillsResp { items: SkillRow[]; }

interface HubResult {
  id: string;
  name: string;
  description: string;
  source: string;
}

export function SkillsPage() {
  const installed = useApi<SkillsResp>("/api/v1/skills");
  const [hubQ, setHubQ] = useState("");
  const hub = useApi<{ items: HubResult[] }>(
    hubQ.trim() ? `/api/v1/skills/search?q=${encodeURIComponent(hubQ.trim())}` : "",
    [hubQ],
  );

  async function toggle(name: string, want: boolean) {
    try {
      await api("/api/v1/skills/toggle", {
        method: "PUT",
        body: JSON.stringify({ name, enabled: want }),
      });
      installed.refetch();
    } catch (e) {
      alert(`Toggle failed: ${(e as Error).message}`);
    }
  }

  return (
    <div className="p-6">
      <h1 className="mb-4 text-2xl font-semibold">Skills</h1>
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-400">
        Installed
      </h2>
      {installed.loading && <p className="text-zinc-500">Loading…</p>}
      {installed.error && <p className="text-red-400">Error: {installed.error.message}</p>}
      {installed.data && installed.data.items.length === 0 && (
        <p className="mb-6 text-sm text-zinc-500">No skills installed yet.</p>
      )}
      {installed.data && installed.data.items.length > 0 && (
        <table className="mb-6 w-full text-sm">
          <thead className="text-left text-zinc-400">
            <tr>
              <th className="py-2 pr-4">Name</th>
              <th className="py-2 pr-4">Description</th>
              <th className="py-2 pr-4 text-right">Enabled</th>
            </tr>
          </thead>
          <tbody>
            {installed.data.items.map((s) => (
              <tr key={s.name} className="border-t border-zinc-800/50">
                <td className="py-2 pr-4 font-mono">{s.name}</td>
                <td className="py-2 pr-4 text-xs text-zinc-500">{s.description}</td>
                <td className="py-2 pr-4 text-right">
                  <input
                    type="checkbox"
                    checked={s.enabled}
                    onChange={(e) => toggle(s.name, e.target.checked)}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-400">
        Browse hub
      </h2>
      <input
        type="text"
        placeholder="Search agentskills.io…"
        value={hubQ}
        onChange={(e) => setHubQ(e.target.value)}
        className="mb-3 w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm"
      />
      {hub.loading && hubQ && <p className="text-zinc-500">Searching…</p>}
      {hub.error && <p className="text-red-400">Hub: {hub.error.message}</p>}
      {hub.data && (
        <ul className="space-y-1 text-sm">
          {hub.data.items.map((r) => (
            <li key={r.id} className="rounded border border-zinc-800 bg-zinc-900/50 p-2">
              <div className="font-mono text-xs text-cyan-400">{r.id}</div>
              <div className="text-zinc-300">{r.name}</div>
              <div className="text-xs text-zinc-500">{r.description}</div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
