import { Routes, Route, NavLink } from "react-router-dom";
import { useApi } from "@/hooks/useApi";
import type { StatusResponse } from "@/lib/api";
import { SessionsPage, SessionDetailPage } from "@/pages/SessionsPage";

const Placeholder = ({ name }: { name: string }) => (
  <div className="p-6">
    <h1 className="text-2xl font-semibold">{name}</h1>
    <p className="mt-2 text-sm text-zinc-400">
      This page lands in a follow-up PR. See{" "}
      <code className="rounded bg-zinc-800 px-1 py-0.5">
        docs/superpowers/plans/2026-05-07-dashboard-polish.md
      </code>{" "}
      for the full ship arc.
    </p>
  </div>
);

const NAV: { path: string; label: string }[] = [
  { path: "/chat", label: "Chat" },
  { path: "/sessions", label: "Sessions" },
  { path: "/skills", label: "Skills" },
  { path: "/plugins", label: "Plugins" },
  { path: "/cron", label: "Cron" },
  { path: "/logs", label: "Logs" },
  { path: "/models", label: "Models" },
  { path: "/profiles", label: "Profiles" },
  { path: "/env", label: "Env" },
  { path: "/config", label: "Config" },
  { path: "/analytics", label: "Analytics" },
  { path: "/docs", label: "Docs" },
];

export default function App() {
  const status = useApi<StatusResponse>("/api/v1/status");

  return (
    <div className="flex h-full">
      <aside className="flex w-52 shrink-0 flex-col border-r border-zinc-800 bg-zinc-950 p-4">
        <h2 className="mb-1 text-lg font-semibold">OpenComputer</h2>
        <p className="mb-4 text-xs text-zinc-500">Dashboard</p>
        <nav className="flex flex-col gap-0.5 text-sm">
          {NAV.map(({ path, label }) => (
            <NavLink
              key={path}
              to={path}
              className={({ isActive }) =>
                `rounded px-2 py-1.5 transition-colors ${
                  isActive
                    ? "bg-zinc-800 text-cyan-300"
                    : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
                }`
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto pt-4 text-xs text-zinc-500">
          {status.data ? (
            <>
              <div>
                v<span className="text-zinc-300">{status.data.version}</span>
              </div>
              <div>
                profile{" "}
                <code className="text-zinc-300">{status.data.profile}</code>
              </div>
              <div className="mt-1 truncate" title={status.data.wire_url}>
                wire{" "}
                <code className="text-zinc-400">
                  {status.data.wire_url.replace(/^ws:\/\//, "")}
                </code>
              </div>
            </>
          ) : status.error ? (
            <span className="text-red-400">offline</span>
          ) : (
            <span className="text-zinc-600">…</span>
          )}
        </div>
      </aside>
      <main className="flex-1 overflow-auto">
        <Routes>
          <Route path="/" element={<Placeholder name="Welcome to OpenComputer" />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/sessions/:id" element={<SessionDetailPage />} />
          {NAV.filter(({ path }) => path !== "/sessions").map(({ path, label }) => (
            <Route key={path} path={path} element={<Placeholder name={label} />} />
          ))}
          <Route
            path="*"
            element={<div className="p-6 text-zinc-400">Not found</div>}
          />
        </Routes>
      </main>
    </div>
  );
}
