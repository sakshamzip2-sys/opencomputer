import { createFileRoute } from '@tanstack/react-router'
import { handlePost } from '../../server/swarm-checkpoint.server'

// Server-only implementation lives in src/server/swarm-checkpoint.server.ts so
// its node:fs imports never reach the client bundle: the TanStack Start
// compiler strips `server.handlers` — and this import with it — from the
// client build.
export const Route = createFileRoute('/api/swarm-checkpoint')({
  server: {
    handlers: {
      POST: handlePost,
    },
  },
})
