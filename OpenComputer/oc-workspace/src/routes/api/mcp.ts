import { createFileRoute } from '@tanstack/react-router'
import { handleGet, handlePost } from '../../server/mcp.server'

// Server-only implementation lives in src/server/mcp.server.ts so its
// gateway / config-write code never reaches the client bundle: the TanStack
// Start compiler strips `server.handlers` — and these imports with it — from
// the client build.
export const Route = createFileRoute('/api/mcp')({
  server: {
    handlers: {
      GET: handleGet,
      POST: handlePost,
    },
  },
})
