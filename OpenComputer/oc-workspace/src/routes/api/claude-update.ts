import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/claude-update.server'

// Server-only implementation lives in src/server/claude-update.server.ts so its
// node:fs / node:child_process / node:path imports never reach the client
// bundle: the TanStack Start compiler strips `server.handlers` — and these
// imports with it — from the client build.
export const Route = createFileRoute('/api/claude-update')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
