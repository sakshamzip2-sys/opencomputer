import { createFileRoute } from '@tanstack/react-router'
import { handleGet, handlePost } from '../../server/workspace.server'

// Server-only implementation lives in src/server/workspace.server.ts so its
// node:fs / node:path / node:os imports never reach the client bundle: the
// TanStack Start compiler strips `server.handlers` — and these imports with it —
// from the client build.
export const Route = createFileRoute('/api/workspace')({
  server: {
    handlers: {
      GET: handleGet,
      POST: handlePost,
    },
  },
})
