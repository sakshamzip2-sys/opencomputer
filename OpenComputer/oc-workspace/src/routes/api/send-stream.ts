import { createFileRoute } from '@tanstack/react-router'
import { handlePost } from '../../server/send-stream.server'

// Server-only implementation lives in src/server/send-stream.server.ts so its
// gateway / session-store / agent-streaming imports never reach the client
// bundle: the TanStack Start compiler strips `server.handlers` — and this
// import with it — from the client build.
export const Route = createFileRoute('/api/send-stream')({
  server: {
    handlers: {
      POST: handlePost,
    },
  },
})
