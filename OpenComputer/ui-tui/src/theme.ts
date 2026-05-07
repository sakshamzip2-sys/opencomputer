// OC TUI palette. Mirrors the dashboard's palette so the two surfaces
// feel like one product.

export const theme = {
  accent: "cyan",
  user: "cyan",
  assistant: "white",
  tool: "yellow",
  error: "red",
  muted: "gray",
  success: "green",
} as const;

export const banner = `
  ┌─────────────────────────────────────┐
  │         ◯  OpenComputer TUI         │
  │  hermes shell · OC backend (wire)   │
  └─────────────────────────────────────┘
`.trim();
