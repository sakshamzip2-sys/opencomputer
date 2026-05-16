import { createFileRoute } from '@tanstack/react-router'
import { handleGet, handlePut } from '../../server/connection-settings.server'

// Server-only implementation lives in src/server/connection-settings.server.ts
// so its gateway-capabilities code never reaches the client bundle: the
// TanStack Start compiler strips `server.handlers` — and these imports with
// it — from the client build.
export const Route = createFileRoute('/api/connection-settings')({
  server: {
    handlers: {
      GET: handleGet,
      PUT: handlePut,
    },
  },
})
