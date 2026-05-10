import { useState } from "react";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";

interface EnvKey {
  key: string;
  set: boolean;
  length: number;
  hint: string;
}
interface EnvResp { items: EnvKey[]; }

export function EnvPage() {
  const list = useApi<EnvResp>("/api/v1/env");
  const [newKey, setNewKey] = useState("");
  const [newVal, setNewVal] = useState("");
  const [revealed, setRevealed] = useState<Record<string, string>>({});

  async function put() {
    if (!newKey) return;
    try {
      await api("/api/v1/env", {
        method: "PUT",
        body: JSON.stringify({ key: newKey, value: newVal }),
      });
      setNewKey(""); setNewVal("");
      list.refetch();
    } catch (e) { alert((e as Error).message); }
  }

  async function del(key: string) {
    if (!confirm(`Delete env var ${key}?`)) return;
    try {
      await api(`/api/v1/env?key=${encodeURIComponent(key)}`, { method: "DELETE" });
      list.refetch();
    } catch (e) { alert((e as Error).message); }
  }

  async function reveal(key: string) {
    if (!confirm(`Reveal ${key}? This will show the secret value on screen.`)) return;
    try {
      const r = await api<{ value: string }>("/api/v1/env/reveal", {
        method: "POST",
        body: JSON.stringify({ key, value: "" }),
        headers: { "X-OC-Confirm": "yes" } as Record<string, string>,
      });
      setRevealed((prev) => ({ ...prev, [key]: r.value }));
      // Auto-clear after 30s
      setTimeout(() => {
        setRevealed((prev) => {
          const { [key]: _, ...rest } = prev;
          return rest;
        });
      }, 30_000);
    } catch (e) { alert((e as Error).message); }
  }

  return (
    <div className="p-6">
      <h1 className="mb-4 text-2xl font-semibold">Env</h1>
      <p className="mb-4 text-sm text-zinc-500">
        Profile-local <code className="bg-zinc-800 px-1 py-0.5 rounded">.env</code> file.
        Values never leave loopback. Reveal requires explicit confirmation; auto-clears after 30s.
      </p>
      <div className="mb-4 flex gap-2">
        <input
          placeholder="KEY"
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm font-mono"
        />
        <input
          type="password"
          placeholder="value"
          value={newVal}
          onChange={(e) => setNewVal(e.target.value)}
          className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm"
        />
        <button
          onClick={put}
          disabled={!newKey}
          className="rounded border border-cyan-700 bg-cyan-950/40 px-3 py-1 text-sm text-cyan-300 disabled:opacity-50"
        >
          Set
        </button>
      </div>
      {list.loading && <p className="text-zinc-500">Loading…</p>}
      {list.error && <p className="text-red-400">Error: {list.error.message}</p>}
      {list.data && list.data.items.length === 0 && (
        <p className="text-sm text-zinc-500">No env vars configured.</p>
      )}
      {list.data && list.data.items.length > 0 && (
        <table className="w-full text-sm">
          <thead className="text-left text-zinc-400">
            <tr>
              <th className="py-2 pr-4">Key</th>
              <th className="py-2 pr-4">Value</th>
              <th className="py-2 pr-4 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {list.data.items.map((e) => (
              <tr key={e.key} className="border-t border-zinc-800/50">
                <td className="py-2 pr-4 font-mono">{e.key}</td>
                <td className="py-2 pr-4 font-mono text-xs">
                  {revealed[e.key] ? (
                    <span className="text-amber-300">{revealed[e.key]}</span>
                  ) : (
                    <span className="text-zinc-500">{e.hint}</span>
                  )}
                </td>
                <td className="py-2 pr-4 text-right text-xs space-x-3">
                  <button onClick={() => reveal(e.key)} className="text-amber-400 hover:underline">Reveal</button>
                  <button onClick={() => del(e.key)} className="text-red-400 hover:underline">Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
