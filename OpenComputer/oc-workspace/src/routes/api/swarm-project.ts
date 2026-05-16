import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/swarm-project.server'

// Server-only implementation lives in src/server/swarm-project.server.ts so
// its node:fs / node:child_process imports never reach the client bundle: the
// TanStack Start compiler strips `server.handlers` — and this import with it —
// from the client build.
export const Route = createFileRoute('/api/swarm-project')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
