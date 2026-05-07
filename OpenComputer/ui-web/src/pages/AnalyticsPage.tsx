import { useApi } from "@/hooks/useApi";

interface UsageRow { day: number; input_tokens: number; output_tokens: number; cost_usd: number; calls: number; }
interface UsageResp { items: UsageRow[]; days: number; }

interface ModelRow { provider: string; model: string; input_tokens: number; output_tokens: number; cost_usd: number; calls: number; }
interface ModelsResp { items: ModelRow[]; days: number; }

interface ToolRow { tool: string; calls: number; errors: number; avg_duration_ms: number; }
interface ToolsResp { items: ToolRow[]; days: number; note?: string; }

const fmt = (n: number) => n.toLocaleString();
const fmtUsd = (n: number) => `$${n.toFixed(2)}`;
const fmtDay = (ts: number) => new Date(ts * 1000).toLocaleDateString();

export function AnalyticsPage() {
  const usage = useApi<UsageResp>("/api/v1/analytics/usage?days=30");
  const models = useApi<ModelsResp>("/api/v1/analytics/models?days=30");
  const tools = useApi<ToolsResp>("/api/v1/analytics/tools?days=30");

  const totalCost =
    usage.data?.items.reduce((acc, r) => acc + r.cost_usd, 0) ?? 0;
  const totalCalls =
    usage.data?.items.reduce((acc, r) => acc + r.calls, 0) ?? 0;
  const totalInput =
    usage.data?.items.reduce((acc, r) => acc + r.input_tokens, 0) ?? 0;
  const totalOutput =
    usage.data?.items.reduce((acc, r) => acc + r.output_tokens, 0) ?? 0;

  return (
    <div className="p-6">
      <h1 className="mb-4 text-2xl font-semibold">Analytics</h1>
      <p className="mb-4 text-sm text-zinc-500">Last 30 days</p>

      <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Card label="Total cost" value={fmtUsd(totalCost)} />
        <Card label="Calls" value={fmt(totalCalls)} />
        <Card label="Input tokens" value={fmt(totalInput)} />
        <Card label="Output tokens" value={fmt(totalOutput)} />
      </div>

      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-400">
        Usage by day
      </h2>
      {usage.error && <p className="text-red-400">{usage.error.message}</p>}
      {usage.data && usage.data.items.length === 0 && (
        <p className="mb-6 text-sm text-zinc-500">No usage data yet.</p>
      )}
      {usage.data && usage.data.items.length > 0 && (
        <table className="mb-6 w-full text-sm">
          <thead className="text-left text-zinc-400">
            <tr>
              <th className="py-2 pr-4">Day</th>
              <th className="py-2 pr-4 text-right">Calls</th>
              <th className="py-2 pr-4 text-right">Input</th>
              <th className="py-2 pr-4 text-right">Output</th>
              <th className="py-2 pr-4 text-right">Cost</th>
            </tr>
          </thead>
          <tbody>
            {usage.data.items.map((r) => (
              <tr key={r.day} className="border-t border-zinc-800/50">
                <td className="py-1 pr-4 text-zinc-400">{fmtDay(r.day)}</td>
                <td className="py-1 pr-4 text-right">{fmt(r.calls)}</td>
                <td className="py-1 pr-4 text-right">{fmt(r.input_tokens)}</td>
                <td className="py-1 pr-4 text-right">{fmt(r.output_tokens)}</td>
                <td className="py-1 pr-4 text-right">{fmtUsd(r.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-400">
        By model
      </h2>
      {models.data && models.data.items.length > 0 && (
        <table className="mb-6 w-full text-sm">
          <thead className="text-left text-zinc-400">
            <tr>
              <th className="py-2 pr-4">Provider</th>
              <th className="py-2 pr-4">Model</th>
              <th className="py-2 pr-4 text-right">Calls</th>
              <th className="py-2 pr-4 text-right">Cost</th>
            </tr>
          </thead>
          <tbody>
            {models.data.items.map((r, i) => (
              <tr key={i} className="border-t border-zinc-800/50">
                <td className="py-1 pr-4">{r.provider}</td>
                <td className="py-1 pr-4 font-mono text-xs">{r.model}</td>
                <td className="py-1 pr-4 text-right">{fmt(r.calls)}</td>
                <td className="py-1 pr-4 text-right">{fmtUsd(r.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-400">
        Tools
      </h2>
      {tools.data?.note && <p className="text-xs text-zinc-500">{tools.data.note}</p>}
      {tools.data && tools.data.items.length > 0 && (
        <table className="w-full text-sm">
          <thead className="text-left text-zinc-400">
            <tr>
              <th className="py-2 pr-4">Tool</th>
              <th className="py-2 pr-4 text-right">Calls</th>
              <th className="py-2 pr-4 text-right">Errors</th>
              <th className="py-2 pr-4 text-right">Avg ms</th>
            </tr>
          </thead>
          <tbody>
            {tools.data.items.map((r) => (
              <tr key={r.tool} className="border-t border-zinc-800/50">
                <td className="py-1 pr-4 font-mono">{r.tool}</td>
                <td className="py-1 pr-4 text-right">{fmt(r.calls)}</td>
                <td className="py-1 pr-4 text-right">{fmt(r.errors)}</td>
                <td className="py-1 pr-4 text-right">{r.avg_duration_ms.toFixed(0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Card({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-zinc-800 bg-zinc-900/50 p-3">
      <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
    </div>
  );
}
