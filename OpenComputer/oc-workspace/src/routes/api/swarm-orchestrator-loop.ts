import { createFileRoute } from '@tanstack/react-router'
import { handlePost } from '../../server/swarm-orchestrator-loop.server'

// Server-only implementation lives in
// src/server/swarm-orchestrator-loop.server.ts so its node:fs imports never
// reach the client bundle: the TanStack Start compiler strips
// `server.handlers` — and this import with it — from the client build.
export const Route = createFileRoute('/api/swarm-orchestrator-loop')({
  server: {
    handlers: {
      POST: handlePost,
    },
  },
})
