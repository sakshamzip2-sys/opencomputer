import { useEffect, useState } from "react";
import { Routes, Route } from "react-router-dom";
import { SessionsPage, SessionDetailPage } from "@/pages/SessionsPage";
import { LogsPage } from "@/pages/LogsPage";
import { ModelsPage } from "@/pages/ModelsPage";
import { PluginsPage } from "@/pages/PluginsPage";
import { ProfilesPage } from "@/pages/ProfilesPage";
import { SkillsPage } from "@/pages/SkillsPage";
import { CronPage } from "@/pages/CronPage";
import { ConfigPage } from "@/pages/ConfigPage";
import { EnvPage } from "@/pages/EnvPage";
import { ChatPage } from "@/pages/ChatPage";
import { AnalyticsPage } from "@/pages/AnalyticsPage";
import { DocsPage } from "@/pages/DocsPage";
import { Sidebar } from "@/components/Sidebar";
import { StatusBar } from "@/components/StatusBar";
import { ToastProvider } from "@/components/Toast";
import type { Locale } from "@/i18n";

const LOCALE_STORAGE_KEY = "oc-dashboard-locale";

export default function App() {
  const [locale, setLocale] = useState<Locale>(() => {
    const stored = localStorage.getItem(LOCALE_STORAGE_KEY);
    return stored === "en" || stored === "zh" ? stored : "en";
  });

  useEffect(() => {
    localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  }, [locale]);

  return (
    <ToastProvider>
      <div className="flex h-full flex-col">
        <StatusBar locale={locale} onLocaleChange={setLocale} />
        <div className="flex flex-1 overflow-hidden">
          <Sidebar locale={locale} />
          <main className="flex-1 overflow-auto">
            <Routes>
              <Route path="/" element={<Welcome />} />
              <Route path="/sessions" element={<SessionsPage />} />
              <Route path="/sessions/:id" element={<SessionDetailPage />} />
              <Route path="/logs" element={<LogsPage />} />
              <Route path="/models" element={<ModelsPage />} />
              <Route path="/plugins" element={<PluginsPage />} />
              <Route path="/profiles" element={<ProfilesPage />} />
              <Route path="/skills" element={<SkillsPage />} />
              <Route path="/cron" element={<CronPage />} />
              <Route path="/config" element={<ConfigPage />} />
              <Route path="/env" element={<EnvPage />} />
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/analytics" element={<AnalyticsPage />} />
              <Route path="/docs" element={<DocsPage />} />
              <Route
                path="*"
                element={<div className="p-6 text-zinc-400">Not found</div>}
              />
            </Routes>
          </main>
        </div>
      </div>
    </ToastProvider>
  );
}

function Welcome() {
  return (
    <div className="p-8">
      <h1 className="mb-4 text-3xl font-semibold">OpenComputer Dashboard</h1>
      <p className="mb-6 text-zinc-400">
        Personal AI agent control panel. Pick a section from the sidebar.
      </p>
      <ul className="space-y-1 text-sm text-zinc-500">
        <li>• <strong className="text-cyan-400">Chat</strong> — talk to the agent live (needs <code className="bg-zinc-800 px-1">oc gateway</code>)</li>
        <li>• <strong className="text-cyan-400">Sessions</strong> — browse + search past conversations</li>
        <li>• <strong className="text-cyan-400">Logs</strong> — live log feed</li>
        <li>• <strong className="text-cyan-400">Models / Plugins / Profiles / Skills</strong> — configuration</li>
        <li>• <strong className="text-cyan-400">Cron / Config / Env</strong> — automation + settings</li>
        <li>• <strong className="text-cyan-400">Analytics / Docs</strong> — usage + documentation</li>
      </ul>
    </div>
  );
}
