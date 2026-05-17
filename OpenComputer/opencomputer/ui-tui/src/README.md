# OpenComputer TUI — source

The source for OpenComputer's terminal UI (`oc tui`). Until 2026-05-17 the
repo shipped only the compiled `../dist/` artifact with no source — the M1
audit (`docs/refs/hermes-tui-protocol-vs-oc-wire.md`) flagged that. This
directory is the start of OC's real, readable TUI source tree.

## Layout

```
src/
├── protocol.ts     wire-protocol constants + result types
│                   (mirrors opencomputer/gateway/protocol.py + protocol_v2.py)
├── wireClient.ts   OCWireClient — JSON-RPC-over-WebSocket client,
│                   one typed wrapper per server RPC method (27 today)
├── package.json    build manifest (ws + TypeScript)
└── tsconfig.json   emits to ../dist/
```

## Build

```bash
cd opencomputer/ui-tui/src
npm install
npm run build      # tsc → ../dist/
npm run typecheck  # type-check without emit
```

`../dist/` is a git-ignored build artifact (force-included into the wheel
via `pyproject.toml`). `scripts/bootstrap_worktree.sh` symlinks it across
worktrees so a fresh checkout can run without building first.

## The client ↔ server contract

`wireClient.ts` must expose a wrapper for every RPC the Python wire server
(`gateway/protocol.py`) defines. `tests/test_ui_tui_wire_client.py` enforces
this — add a `METHOD_*` on the Python side and that test fails until
`protocol.ts` + `wireClient.ts` catch up.

## Status (TUI-parity milestones)

- **M1 — backend wire expansion:** done. 27 RPC methods power every overlay
  (model picker, session picker, settings, agents, skills hub, rollback,
  tools, interrupt).
- **M2 — OC-native TUI frontend:** in progress. This source tree + the
  typed wire client are the foundation. Ink components (overlays, streaming
  markdown, multiline editor) are the remaining work.
