import { createFileRoute } from '@tanstack/react-router'
import {
  handleGet,
  handlePatch,
  handlePost,
} from '../../server/swarm-kanban.server'

// Server-only implementation lives in src/server/swarm-kanban.server.ts so its
// kanban-backend imports never reach the client bundle: the TanStack Start
// compiler strips `server.handlers` — and these imports with it — from the
// client build.
export const Route = createFileRoute('/api/swarm-kanban')({
  server: {
    handlers: {
      GET: handleGet,
      POST: handlePost,
      PATCH: handlePatch,
    },
  },
})
