import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/provider-usage.server'

// Server-only implementation lives in src/server/provider-usage.server.ts so
// its server-only imports never reach the client bundle: the TanStack Start
// compiler strips `server.handlers` — and this import with it — from the
// client build.
export const Route = createFileRoute('/api/provider-usage')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
