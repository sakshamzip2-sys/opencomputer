import { useState } from "react";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";

interface ProviderEntry {
  provider: string;
  models: string[];
}
interface ModelsResp { providers: ProviderEntry[]; }
interface InfoResp { provider: string | null; model: string | null; }

export function ModelsPage() {
  const list = useApi<ModelsResp>("/api/v1/models");
  const info = useApi<InfoResp>("/api/v1/models/info");
  const aux = useApi<InfoResp>("/api/v1/models/auxiliary");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function setDefault(provider: string, model: string) {
    setBusy(`${provider}/${model}`);
    setErr(null);
    try {
      await api("/api/v1/models/set", {
        method: "POST",
        body: JSON.stringify({ provider, model }),
      });
      info.refetch();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="p-6">
      <h1 className="mb-4 text-2xl font-semibold">Models</h1>
      {info.data && (
        <p className="mb-1 text-sm text-zinc-400">
          Default:{" "}
          <code className="font-mono text-cyan-400">
            {info.data.provider}/{info.data.model}
          </code>
        </p>
      )}
      {aux.data && aux.data.provider && (
        <p className="mb-4 text-xs text-zinc-500">
          Auxiliary (cheap-route):{" "}
          <code className="font-mono">
            {aux.data.provider}/{aux.data.model}
          </code>
        </p>
      )}
      {err && (
        <p className="mb-3 rounded border border-red-900 bg-red-950/50 px-3 py-2 text-sm text-red-300">
          {err}
        </p>
      )}
      {list.loading && <p className="text-zinc-500">Loading…</p>}
      {list.error && <p className="text-red-400">Error: {list.error.message}</p>}
      {list.data && (
        <div className="space-y-6">
          {list.data.providers.map((p) => (
            <div key={p.provider}>
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-400">
                {p.provider}
              </h2>
              <ul className="grid gap-1 sm:grid-cols-2 lg:grid-cols-3">
                {p.models.map((m) => {
                  const isCurrent =
                    info.data?.provider === p.provider && info.data?.model === m;
                  const id = `${p.provider}/${m}`;
                  return (
                    <li
                      key={m}
                      className={`flex items-center justify-between rounded border px-3 py-2 text-sm ${
                        isCurrent
                          ? "border-cyan-700 bg-cyan-950/30"
                          : "border-zinc-800 bg-zinc-900/50"
                      }`}
                    >
                      <code className="font-mono text-xs">{m}</code>
                      {isCurrent ? (
                        <span className="text-xs text-cyan-400">(current)</span>
                      ) : (
                        <button
                          disabled={busy === id}
                          onClick={() => setDefault(p.provider, m)}
                          className="text-xs text-cyan-400 hover:underline disabled:opacity-50"
                        >
                          {busy === id ? "Setting…" : "Set default"}
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
