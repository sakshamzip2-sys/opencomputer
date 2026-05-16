import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/crew-status.server'

// Server-only implementation lives in src/server/crew-status.server.ts so its
// node:fs / node:path / node:child_process imports never reach the client
// bundle: the TanStack Start compiler strips `server.handlers` — and this
// import with it — from the client build.
export const Route = createFileRoute('/api/crew-status')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
