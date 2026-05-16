import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/models.server'

// Server-only implementation lives in src/server/models.server.ts so its
// node:fs / node:path / node:os imports never reach the client bundle: the
// TanStack Start compiler strips `server.handlers` — and this import with it —
// from the client build.
export const Route = createFileRoute('/api/models')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
