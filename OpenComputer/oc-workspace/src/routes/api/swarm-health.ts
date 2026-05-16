import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/swarm-health.server'

// Server-only implementation lives in src/server/swarm-health.server.ts so its
// node:fs / node:path imports never reach the client bundle: the TanStack Start
// compiler strips `server.handlers` — and this import with it — from the client
// build.
export const Route = createFileRoute('/api/swarm-health')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
