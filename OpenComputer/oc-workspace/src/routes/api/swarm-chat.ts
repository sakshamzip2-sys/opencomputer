import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/swarm-chat.server'

// Server-only implementation lives in src/server/swarm-chat.server.ts so its
// node:path import and swarm-chat-reader code never reach the client bundle:
// the TanStack Start compiler strips `server.handlers` — and these imports with
// it — from the client build.
export const Route = createFileRoute('/api/swarm-chat')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
