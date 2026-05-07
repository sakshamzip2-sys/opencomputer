import type { Locale } from "@/i18n";

export function LanguageSwitcher({
  locale,
  onChange,
}: {
  locale: Locale;
  onChange: (l: Locale) => void;
}) {
  return (
    <select
      value={locale}
      onChange={(e) => onChange(e.target.value as Locale)}
      className="rounded border border-zinc-800 bg-zinc-950 px-1.5 py-0.5 text-xs"
    >
      <option value="en">EN</option>
      <option value="zh">中文</option>
    </select>
  );
}
