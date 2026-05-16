import { createFileRoute } from '@tanstack/react-router'
import { handleGet, handlePost } from '../../server/swarm-runtime.server'

// Server-only implementation lives in src/server/swarm-runtime.server.ts so its
// node:fs / node:child_process / node:path imports never reach the client
// bundle: the TanStack Start compiler strips `server.handlers` — and these
// imports with it — from the client build.
export const Route = createFileRoute('/api/swarm-runtime')({
  server: {
    handlers: {
      GET: handleGet,
      POST: handlePost,
    },
  },
})
