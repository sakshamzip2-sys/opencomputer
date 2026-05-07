import { useState } from "react";
import { useApi } from "@/hooks/useApi";
import { Markdown } from "@/components/Markdown";

interface DocItem { slug: string; title: string; size: number; }
interface DocsResp { items: DocItem[]; }
interface DocResp { slug: string; path: string; text: string; }

export function DocsPage() {
  const list = useApi<DocsResp>("/api/v1/dashboard/docs");
  const [active, setActive] = useState<string | null>(null);
  const doc = useApi<DocResp>(active ? `/api/v1/dashboard/docs/${active}` : "", [active]);

  return (
    <div className="flex h-full">
      <aside className="w-56 shrink-0 border-r border-zinc-800 bg-zinc-950 p-4">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-400">
          Bundled Docs
        </h2>
        {list.loading && <p className="text-xs text-zinc-500">Loading…</p>}
        {list.error && <p className="text-xs text-red-400">{list.error.message}</p>}
        {list.data && list.data.items.length === 0 && (
          <p className="text-xs text-zinc-500">No docs found.</p>
        )}
        <ul className="space-y-1 text-sm">
          {list.data?.items.map((d) => (
            <li key={d.slug}>
              <button
                onClick={() => setActive(d.slug)}
                className={`block w-full rounded px-2 py-1 text-left ${
                  active === d.slug
                    ? "bg-zinc-800 text-cyan-300"
                    : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
                }`}
              >
                {d.title}
                <span className="ml-2 text-xs text-zinc-500">{(d.size / 1024).toFixed(0)}k</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <main className="flex-1 overflow-auto p-6">
        {!active && <p className="text-zinc-500">Select a doc on the left.</p>}
        {doc.loading && <p className="text-zinc-500">Loading…</p>}
        {doc.error && <p className="text-red-400">{doc.error.message}</p>}
        {doc.data && <Markdown text={doc.data.text} />}
      </main>
    </div>
  );
}
