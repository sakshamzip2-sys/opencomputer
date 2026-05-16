import { createFileRoute } from '@tanstack/react-router'
import {
  handleDelete,
  handleGet,
  handlePatch,
  handlePost,
} from '../../server/sessions.server'

// Server-only implementation lives in src/server/sessions.server.ts so its
// node:crypto import and gateway/local-session-store code never reach the
// client bundle: the TanStack Start compiler strips `server.handlers` — and
// these imports with it — from the client build.
export const Route = createFileRoute('/api/sessions')({
  server: {
    handlers: {
      GET: handleGet,
      POST: handlePost,
      PATCH: handlePatch,
      DELETE: handleDelete,
    },
  },
})
