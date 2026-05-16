import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/claude-tasks-assignees.server'

// Server-only implementation lives in src/server/claude-tasks-assignees.server.ts
// so its node:fs / node:os imports never reach the client bundle: the TanStack
// Start compiler strips `server.handlers` — and this import with it — from the
// client build.
export const Route = createFileRoute('/api/claude-tasks-assignees')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
