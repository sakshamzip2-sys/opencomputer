import { createFileRoute } from '@tanstack/react-router'
import { handleGet, handlePost } from '../../server/conductor-spawn.server'

// Server-only implementation lives in
// src/server/conductor-spawn.server.ts so its node:fs / node:url imports never
// reach the client bundle: the TanStack Start compiler strips
// `server.handlers` — and this import with it — from the client build.
export const Route = createFileRoute('/api/conductor-spawn')({
  server: {
    handlers: {
      GET: handleGet,
      POST: handlePost,
    },
  },
})
