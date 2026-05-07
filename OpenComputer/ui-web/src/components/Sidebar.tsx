import { NavLink } from "react-router-dom";
import { t, type Locale } from "@/i18n";

export interface NavItem { path: string; key: string; }

export const NAV: NavItem[] = [
  { path: "/chat", key: "nav.chat" },
  { path: "/sessions", key: "nav.sessions" },
  { path: "/skills", key: "nav.skills" },
  { path: "/plugins", key: "nav.plugins" },
  { path: "/cron", key: "nav.cron" },
  { path: "/logs", key: "nav.logs" },
  { path: "/models", key: "nav.models" },
  { path: "/profiles", key: "nav.profiles" },
  { path: "/env", key: "nav.env" },
  { path: "/config", key: "nav.config" },
  { path: "/analytics", key: "nav.analytics" },
  { path: "/docs", key: "nav.docs" },
];

export function Sidebar({ locale = "en" }: { locale?: Locale }) {
  return (
    <aside className="flex w-52 shrink-0 flex-col border-r border-zinc-800 bg-zinc-950 p-4">
      <h2 className="mb-1 text-lg font-semibold">OpenComputer</h2>
      <p className="mb-4 text-xs text-zinc-500">Dashboard</p>
      <nav className="flex flex-col gap-0.5 text-sm">
        {NAV.map(({ path, key }) => (
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
            {t(key as Parameters<typeof t>[0], locale)}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
