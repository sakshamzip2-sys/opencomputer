import { createFileRoute } from '@tanstack/react-router'
import { handleGet, handlePost } from '../../server/design.server'

// Server-only implementation lives in src/server/design.server.ts so its
// node:child_process / node:util imports never reach the client bundle: the
// TanStack Start compiler strips `server.handlers` — and these imports with it —
// from the client build.
export const Route = createFileRoute('/api/design')({
  server: {
    handlers: {
      GET: handleGet,
      POST: handlePost,
    },
  },
})
