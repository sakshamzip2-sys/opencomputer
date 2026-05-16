import { createFileRoute } from '@tanstack/react-router'
import { handleGet, handlePost } from '../../server/claude-jobs.server'

// Server-only implementation lives in src/server/claude-jobs.server.ts so its
// gateway-capabilities code never reaches the client bundle: the TanStack
// Start compiler strips `server.handlers` — and these imports with it — from
// the client build.
export const Route = createFileRoute('/api/claude-jobs')({
  server: {
    handlers: {
      GET: handleGet,
      POST: handlePost,
    },
  },
})
