// Minimal i18n stub. EN ships; ZH is structured for forward-compat per
// the design spec. Real string-table swap lands in v2.

import { en } from "./en";
import { zh } from "./zh";

export const LOCALES = { en, zh } as const;
export type Locale = keyof typeof LOCALES;

export function t(key: keyof typeof en, locale: Locale = "en"): string {
  return LOCALES[locale][key] ?? LOCALES.en[key] ?? key;
}
