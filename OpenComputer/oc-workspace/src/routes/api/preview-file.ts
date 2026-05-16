import { createFileRoute } from '@tanstack/react-router'
import { handleGet } from '../../server/preview-file.server'

// Server-only implementation lives in src/server/preview-file.server.ts so its
// node:fs / node:os / node:path imports never reach the client bundle: the
// TanStack Start compiler strips `server.handlers` — and this import with it —
// from the client build.
export const Route = createFileRoute('/api/preview-file')({
  server: {
    handlers: {
      GET: handleGet,
    },
  },
})
