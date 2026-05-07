import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { useState } from "react";

interface PluginRow {
  name: string;
  version: string;
  kind: string;
  description: string;
  enabled?: boolean;
  path?: string;
}

interface PluginsResp {
  items: PluginRow[];
  discovered: PluginRow[];
}

export function PluginsPage() {
  const { data, loading, error, refetch } = useApi<PluginsResp>("/api/v1/plugins");
  const [busy, setBusy] = useState<string | null>(null);

  async function toggle(name: string, want: boolean) {
    setBusy(name);
    try {
      await api(
        `/api/v1/plugins/${encodeURIComponent(name)}/${want ? "enable" : "disable"}`,
        { method: "POST" },
      );
      refetch();
    } catch (e) {
      alert(`Failed: ${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="p-6">
      <h1 className="mb-4 text-2xl font-semibold">Plugins</h1>
      {loading && <p className="text-zinc-500">Loading…</p>}
      {error && <p className="text-red-400">Error: {error.message}</p>}
      {data && (
        <>
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-400">
            Loaded ({data.items.length})
          </h2>
          {data.items.length === 0 ? (
            <p className="mb-6 text-sm text-zinc-500">No plugins loaded.</p>
          ) : (
            <table className="mb-6 w-full text-sm">
              <thead className="text-left text-zinc-400">
                <tr>
                  <th className="py-2 pr-4">Name</th>
                  <th className="py-2 pr-4">Version</th>
                  <th className="py-2 pr-4">Kind</th>
                  <th className="py-2 pr-4">Description</th>
                  <th className="py-2 pr-4 text-right">Enabled</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((p) => (
                  <tr key={p.name} className="border-t border-zinc-800/50">
                    <td className="py-2 pr-4 font-mono">{p.name}</td>
                    <td className="py-2 pr-4 text-zinc-400">{p.version}</td>
                    <td className="py-2 pr-4 text-zinc-400">{p.kind}</td>
                    <td className="py-2 pr-4 text-xs text-zinc-500">{p.description}</td>
                    <td className="py-2 pr-4 text-right">
                      <input
                        type="checkbox"
                        checked={p.enabled ?? true}
                        disabled={busy === p.name}
                        onChange={(e) => toggle(p.name, e.target.checked)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-400">
            Discovered ({data.discovered.length})
          </h2>
          {data.discovered.length > 0 && (
            <ul className="space-y-1 text-xs text-zinc-400">
              {data.discovered.map((p) => (
                <li key={p.name}>
                  <code className="font-mono">{p.name}</code> ({p.kind}) — {p.description}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
