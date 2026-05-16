import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/connection-status.server'

// Server-only implementation lives in src/server/connection-status.server.ts so
// its node:fs / node:os imports never reach the client bundle: the TanStack
// Start compiler strips `server.handlers` — and this import with it — from the
// client build.
export const Route = createFileRoute('/api/connection-status')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
