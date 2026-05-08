# Hermes TUI components — vendored as reference

The 22 `.tsx` files in this directory are vendored verbatim from
`hermes-agent/ui-tui/src/components/` (MIT, Nous Research). Imports
have been rewritten from `@hermes/ink` to `@oc/ink` (the
workspace package at `../packages/oc-ink/`).

**Status: NOT wired into the active TUI build.** They are excluded
from `tsconfig.build.json` because they depend on Hermes-side
helpers we haven't ported yet:

- `src/lib/` — terminalSetup, viewportStore, virtualHeights, wheelAccel,
  editor, history, fpsStore, terminalParity, text, todo, etc. (~3.5k LOC)
- `src/types.ts` (~200 LOC) + `src/theme.ts` shape extensions (~570 LOC)
- `src/hooks/`, `src/domain/`, `src/protocol/`, `src/content/`, etc.

Full integration is genuine 1-2 days of work and was scoped out of the
initial dashboard-polish-2026-05-07 PR. The components ship in the tree
so a future session can pick up where this one stopped without
re-vendoring.

## To wire one in

1. Pick a component (e.g. `messageLine.tsx`).
2. Trace its imports: `grep "from '\\.\\." messageLine.tsx`.
3. Vendor each missing helper from `~/.hermes/hermes-agent/ui-tui/src/`
   into the corresponding OC path.
4. Repeat transitively until tsc is happy.
5. Remove `src/components` from the `exclude` list in
   `tsconfig.build.json`.
6. Use the component in `src/app.tsx`.

## What's actively used today

The minimal TUI (`src/entry.tsx` + `src/app.tsx` + `src/gatewayClient.ts`
+ `src/theme.ts`) is ~400 LOC of from-scratch Ink code that connects to
the OC wire server and ships a working chat + slash palette UX. That's
what `oc tui` runs.
