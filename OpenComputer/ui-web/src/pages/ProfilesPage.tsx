import { useState } from "react";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";

interface ProfileRow {
  name: string;
  dir: string | null;
  active: boolean;
}

interface ProfilesResp {
  items: ProfileRow[];
  active: string | null;
}

export function ProfilesPage() {
  const { data, loading, error, refetch } = useApi<ProfilesResp>("/api/v1/profiles");
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function create() {
    if (!newName.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await api("/api/v1/profiles", {
        method: "POST",
        body: JSON.stringify({ name: newName.trim() }),
      });
      setNewName("");
      refetch();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function del(name: string) {
    if (!confirm(`Delete profile "${name}"? Files in ~/.opencomputer/${name} will be removed.`))
      return;
    try {
      await api(`/api/v1/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
      refetch();
    } catch (e) {
      alert(`Delete failed: ${(e as Error).message}`);
    }
  }

  async function setActive(name: string) {
    try {
      await api(`/api/v1/profiles/active`, {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      refetch();
    } catch (e) {
      alert(`Failed: ${(e as Error).message}`);
    }
  }

  return (
    <div className="p-6">
      <h1 className="mb-4 text-2xl font-semibold">Profiles</h1>
      <div className="mb-4 flex gap-2">
        <input
          type="text"
          placeholder="new profile name"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && create()}
          className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm"
        />
        <button
          onClick={create}
          disabled={busy || !newName.trim()}
          className="rounded border border-cyan-700 bg-cyan-950/40 px-3 py-2 text-sm text-cyan-300 hover:bg-cyan-900/40 disabled:opacity-50"
        >
          {busy ? "Creating…" : "Create"}
        </button>
      </div>
      {err && (
        <p className="mb-3 rounded border border-red-900 bg-red-950/50 px-3 py-2 text-sm text-red-300">
          {err}
        </p>
      )}
      {loading && <p className="text-zinc-500">Loading…</p>}
      {error && <p className="text-red-400">Error: {error.message}</p>}
      {data && (
        <table className="w-full text-sm">
          <thead className="text-left text-zinc-400">
            <tr>
              <th className="py-2 pr-4">Name</th>
              <th className="py-2 pr-4">Directory</th>
              <th className="py-2 pr-4 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((p) => (
              <tr key={p.name} className="border-t border-zinc-800/50">
                <td className="py-2 pr-4">
                  <code className="font-mono">{p.name}</code>
                  {p.active && (
                    <span className="ml-2 rounded bg-cyan-950 px-1.5 py-0.5 text-xs text-cyan-300">
                      active
                    </span>
                  )}
                </td>
                <td className="py-2 pr-4 text-xs text-zinc-500">{p.dir ?? "—"}</td>
                <td className="py-2 pr-4 text-right text-xs">
                  {!p.active && (
                    <button
                      onClick={() => setActive(p.name)}
                      className="mr-3 text-cyan-400 hover:underline"
                    >
                      Set active
                    </button>
                  )}
                  <button
                    onClick={() => del(p.name)}
                    className="text-red-400 hover:underline"
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
