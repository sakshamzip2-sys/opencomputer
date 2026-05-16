import { createFileRoute } from '@tanstack/react-router'
import { handleGet, handlePost } from '../../server/swarm-memory.server'

// Server-only implementation lives in src/server/swarm-memory.server.ts so its
// swarm-memory imports never reach the client bundle: the TanStack Start
// compiler strips `server.handlers` — and these imports with it — from the
// client build.
export const Route = createFileRoute('/api/swarm-memory')({
  server: {
    handlers: {
      GET: handleGet,
      POST: handlePost,
    },
  },
})
