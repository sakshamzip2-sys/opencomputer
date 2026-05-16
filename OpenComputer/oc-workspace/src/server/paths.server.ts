import path from 'node:path'
import os from 'node:os'
import { json } from '@tanstack/react-start'
import { isAuthenticated } from './auth-middleware'

const CLAUDE_HOME =
  process.env.HERMES_HOME ||
  process.env.CLAUDE_HOME ||
  path.join(os.homedir(), '.hermes')

export async function handleGet({ request }: { request: Request }) {
  if (!isAuthenticated(request)) {
    return json({ ok: false, error: 'Unauthorized' }, { status: 401 })
  }
  return json({
    ok: true,
    claudeHome: CLAUDE_HOME,
    memoriesDir: path.join(CLAUDE_HOME, 'memories'),
    skillsDir: path.join(CLAUDE_HOME, 'skills'),
  })
}
