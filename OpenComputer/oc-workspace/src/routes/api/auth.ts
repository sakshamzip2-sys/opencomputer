import { createFileRoute } from '@tanstack/react-router'
import { handlePost } from '../../server/auth.server'

// Server-only implementation lives in src/server/auth.server.ts so its
// auth-middleware / rate-limit code never reaches the client bundle: the
// TanStack Start compiler strips `server.handlers` — and this import with it —
// from the client build.
export const Route = createFileRoute('/api/auth')({
  server: {
    handlers: {
      POST: handlePost,
    },
  },
})
