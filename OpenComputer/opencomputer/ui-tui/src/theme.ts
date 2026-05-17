// OpenComputer TUI — theme tokens (TypeScript source).
//
// TUI-parity Milestone 2. A single source of colour tokens for the app,
// overlays, and the markdown renderer. Values are chalk/Ink colour names
// (so they render consistently across truecolor and 256-colour terminals).

export const theme = {
  // ── base ──
  accent: "cyan",
  fg: "white",
  muted: "gray",

  // ── conversation roles ──
  user: "white",
  assistant: "green",
  tool: "yellow",
  system: "gray",

  // ── status ──
  ok: "green",
  warn: "yellow",
  error: "red",

  // ── markdown ──
  heading: "cyanBright",
  code: "magenta",
  codeBlock: "gray",
  bullet: "cyan",
  bold: "whiteBright",

  // ── chrome ──
  border: "gray",
  borderActive: "cyan",
} as const;

export type ThemeToken = keyof typeof theme;
