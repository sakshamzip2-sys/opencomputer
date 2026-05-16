import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/system-metrics.server'

// Server-only implementation lives in src/server/system-metrics.server.ts so
// its node:fs / node:os imports never reach the client bundle: the TanStack
// Start compiler strips `server.handlers` — and this import with it — from the
// client build.
export const Route = createFileRoute('/api/system-metrics')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
