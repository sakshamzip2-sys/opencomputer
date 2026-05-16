import { createFileRoute } from '@tanstack/react-router'
import { handleGet, handlePatch } from '../../server/claude-config.server'

// Server-only implementation lives in src/server/claude-config.server.ts so its
// node:fs / node:os / node:path imports never reach the client bundle: the
// TanStack Start compiler strips `server.handlers` — and this import with it —
// from the client build.
export const Route = createFileRoute('/api/claude-config')({
  server: {
    handlers: {
      GET: handleGet,
      PATCH: handlePatch,
    },
  },
})
