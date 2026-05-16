import { createFileRoute } from '@tanstack/react-router'
import {
  handleDelete,
  handleGet,
  handlePatch,
  handlePost,
} from '../../server/claude-jobs.$jobId.server'

// Server-only implementation lives in src/server/claude-jobs.$jobId.server.ts
// so its gateway-capabilities code never reaches the client bundle: the
// TanStack Start compiler strips `server.handlers` — and these imports with
// it — from the client build.
export const Route = createFileRoute('/api/claude-jobs/$jobId')({
  server: {
    handlers: {
      GET: handleGet,
      POST: handlePost,
      PATCH: handlePatch,
      DELETE: handleDelete,
    },
  },
})
