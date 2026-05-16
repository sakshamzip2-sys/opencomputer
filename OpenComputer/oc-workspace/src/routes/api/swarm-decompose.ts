import { createFileRoute } from '@tanstack/react-router'
import { handlePost } from '../../server/swarm-decompose.server'

// Server-only implementation lives in src/server/swarm-decompose.server.ts so
// its server-only imports never reach the client bundle: the TanStack Start
// compiler strips `server.handlers` — and this import with it — from the
// client build.
export const Route = createFileRoute('/api/swarm-decompose')({
  server: {
    handlers: {
      POST: handlePost,
    },
  },
})
