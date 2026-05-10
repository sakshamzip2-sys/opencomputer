import { useApi } from "@/hooks/useApi";
import type { StatusResponse } from "@/lib/api";
import { ConnectionIndicator } from "./ConnectionIndicator";
import { LanguageSwitcher } from "./LanguageSwitcher";
import type { Locale } from "@/i18n";

export function StatusBar({
  locale,
  onLocaleChange,
}: {
  locale: Locale;
  onLocaleChange: (l: Locale) => void;
}) {
  const status = useApi<StatusResponse>("/api/v1/status");

  return (
    <header className="flex items-center justify-between border-b border-zinc-800 bg-zinc-950 px-4 py-2 text-xs">
      <div className="flex items-center gap-3">
        {status.data ? (
          <>
            <span className="text-zinc-500">v</span>
            <span className="font-mono text-zinc-300">{status.data.version}</span>
            <span className="text-zinc-700">·</span>
            <span className="text-zinc-500">profile</span>
            <code className="text-zinc-300">{status.data.profile}</code>
            <span className="text-zinc-700">·</span>
            <span className="text-zinc-500">wire</span>
            <code className="text-zinc-400">
              {status.data.wire_url.replace(/^ws:\/\//, "")}
            </code>
          </>
        ) : status.error ? (
          <span className="text-red-400">offline</span>
        ) : (
          <span className="text-zinc-600">loading…</span>
        )}
      </div>
      <div className="flex items-center gap-3">
        <ConnectionIndicator wireUrl={status.data?.wire_url} />
        <LanguageSwitcher locale={locale} onChange={onLocaleChange} />
      </div>
    </header>
  );
}
