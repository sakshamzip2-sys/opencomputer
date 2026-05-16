import { createFileRoute } from '@tanstack/react-router'
import {
  handleDelete,
  handleGet,
  handlePatch,
  handlePost,
} from '../../server/claude-tasks.$taskId.server'

// Server-only implementation lives in src/server/claude-tasks.$taskId.server.ts
// so its claude-tasks-backend code never reaches the client bundle: the
// TanStack Start compiler strips `server.handlers` — and these imports with
// it — from the client build.
export const Route = createFileRoute('/api/claude-tasks/$taskId')({
  server: {
    handlers: {
      GET: handleGet,
      PATCH: handlePatch,
      DELETE: handleDelete,
      POST: handlePost,
    },
  },
})
