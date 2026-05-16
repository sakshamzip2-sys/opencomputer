import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/paths.server'

// Server-only implementation lives in src/server/paths.server.ts so its
// node:path / node:os imports never reach the client bundle: the TanStack
// Start compiler strips `server.handlers` — and this import with it — from the
// client build.
export const Route = createFileRoute('/api/paths')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
