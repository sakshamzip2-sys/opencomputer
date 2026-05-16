import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/media.server'

// Server-only implementation lives in src/server/media.server.ts so its
// node:fs / node:os imports never reach the client bundle: the TanStack Start
// compiler strips `server.handlers` — and this import with it — from the
// client build.
export const Route = createFileRoute('/api/media')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
