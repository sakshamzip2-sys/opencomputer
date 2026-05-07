import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useApi } from "@/hooks/useApi";

interface RawResp { path: string; text: string; }

export function ConfigPage() {
  const raw = useApi<RawResp>("/api/v1/config/raw");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    if (raw.data) setText(raw.data.text);
  }, [raw.data]);

  async function save() {
    setBusy(true); setMsg(null);
    try {
      const result = await api<{ ok: boolean; backup: string | null }>("/api/v1/config/raw", {
        method: "PUT",
        body: JSON.stringify({ text }),
      });
      setMsg({ kind: "ok", text: `Saved. Backup: ${result.backup ?? "n/a"}` });
      raw.refetch();
    } catch (e) {
      setMsg({ kind: "err", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full flex-col p-6">
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-2xl font-semibold">Config</h1>
        {raw.data && (
          <code className="text-xs text-zinc-500">{raw.data.path}</code>
        )}
        <button
          onClick={save}
          disabled={busy}
          className="ml-auto rounded border border-cyan-700 bg-cyan-950/40 px-3 py-1 text-sm text-cyan-300 hover:bg-cyan-900/40 disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
      {msg && (
        <p
          className={`mb-3 rounded border px-3 py-2 text-sm ${
            msg.kind === "ok"
              ? "border-green-900 bg-green-950/40 text-green-300"
              : "border-red-900 bg-red-950/50 text-red-300"
          }`}
        >
          {msg.text}
        </p>
      )}
      {raw.loading && <p className="text-zinc-500">Loading…</p>}
      {raw.error && <p className="text-red-400">Error: {raw.error.message}</p>}
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        spellCheck={false}
        className="flex-1 rounded border border-zinc-800 bg-zinc-950 p-3 font-mono text-xs"
      />
      <p className="mt-2 text-xs text-zinc-500">
        On save: previous file backed up as <code>config.yaml.bak</code>;
        rolled back automatically if the new YAML fails to parse.
      </p>
    </div>
  );
}
