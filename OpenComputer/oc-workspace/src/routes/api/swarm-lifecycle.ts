import { createFileRoute } from '@tanstack/react-router'
import { handleGet, handlePost } from '../../server/swarm-lifecycle.server'

// Server-only implementation lives in src/server/swarm-lifecycle.server.ts so
// its swarm-lifecycle / swarm-foundation imports never reach the client
// bundle: the TanStack Start compiler strips `server.handlers` — and these
// imports with it — from the client build.
export const Route = createFileRoute('/api/swarm-lifecycle')({
  server: {
    handlers: {
      GET: handleGet,
      POST: handlePost,
    },
  },
})
