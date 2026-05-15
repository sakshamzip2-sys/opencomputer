/**
 * Hermes Workspace → Open Design daemon bridge.
 *
 * GET  /api/design        Returns daemon health snapshot via `oc design status --json`.
 *                         Shape: { running, pid, port, url, home, log_path, error? }.
 * POST /api/design/start  Spawns `oc design start` (long-running daemon launches detached).
 * POST /api/design/stop   Stops the daemon via `oc design stop`.
 *
 * The plugin's `oc design …` Typer subcommand is the source of truth for
 * process lifecycle; this route is a thin proxy so the React Design tab
 * can drive it without shelling out from the browser.
 */
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { createFileRoute } from '@tanstack/react-router'
import { json } from '@tanstack/react-start'

const execFileAsync = promisify(execFile)

// Timebox each `oc design` invocation so a stuck daemon-spawn doesn't
// hold the request open indefinitely. status is fast (<200ms); start
// waits up to 5s for the listener to come up before returning.
const STATUS_TIMEOUT_MS = 5_000
const ACTION_TIMEOUT_MS = 15_000

type DesignStatus = {
  running: boolean
  pid: number | null
  port: number
  url: string
  home: string | null
  log_path: string
  error: string | null
}

type DesignResponse = {
  ok: boolean
  status?: DesignStatus
  error?: string
}

function ocBinary(): string {
  return process.env.OC_BIN ?? 'oc'
}

async function ocDesignStatus(): Promise<DesignStatus> {
  const { stdout } = await execFileAsync(
    ocBinary(),
    ['design', 'status', '--json'],
    { timeout: STATUS_TIMEOUT_MS, maxBuffer: 1_000_000 },
  )
  const trimmed = stdout.trim()
  if (!trimmed) {
    throw new Error('oc design status returned empty output')
  }
  const parsed = JSON.parse(trimmed) as DesignStatus
  return parsed
}

// Allow-list verbs we proxy through. execFile already escapes args, but
// hard-typing the array prevents a future refactor from passing
// user-controlled strings as the verb. Anything outside this set throws.
const ALLOWED_VERBS = new Set(['start', 'stop', 'restart'] as const)
type DesignVerb = typeof ALLOWED_VERBS extends Set<infer T> ? T : never

function isDesignVerb(value: unknown): value is DesignVerb {
  return typeof value === 'string' && ALLOWED_VERBS.has(value as DesignVerb)
}

async function ocDesignAction(verb: DesignVerb): Promise<DesignStatus> {
  if (!isDesignVerb(verb)) {
    throw new Error(`refusing verb: ${String(verb)}`)
  }
  await execFileAsync(
    ocBinary(),
    ['design', verb],
    { timeout: ACTION_TIMEOUT_MS, maxBuffer: 1_000_000 },
  ).catch((err: NodeJS.ErrnoException & { stderr?: string; stdout?: string }) => {
    // Surface the daemon's stderr (e.g. "open-design not installed") to
    // the caller rather than swallowing under "command failed".
    const detail = err.stderr?.trim() || err.stdout?.trim() || err.message
    throw new Error(detail || `oc design ${verb} failed`)
  })
  // After a start/restart, the daemon may need a beat to expose its
  // port. ocDesignStatus probes the URL; we already wait up to 5s
  // inside `oc design start`, so a single status call after the action
  // returns is sufficient — no extra retry loop here.
  return ocDesignStatus()
}

export const Route = createFileRoute('/api/design')({
  server: {
    handlers: {
      GET: async () => {
        try {
          const status = await ocDesignStatus()
          return json<DesignResponse>({ ok: true, status })
        } catch (err) {
          const error = err instanceof Error ? err.message : String(err)
          // Don't 500 — surface a 200 with a structured error so the
          // React screen can render a "daemon unreachable" CTA instead
          // of an opaque toast.
          return json<DesignResponse>({ ok: false, error })
        }
      },
      POST: async ({ request }) => {
        let action: DesignVerb = 'start'
        try {
          const body = (await request.json().catch(() => ({}))) as Record<string, unknown>
          if (isDesignVerb(body.action)) {
            action = body.action
          }
        } catch {
          // body absent or malformed → default to start
        }
        try {
          const status = await ocDesignAction(action)
          return json<DesignResponse>({ ok: true, status })
        } catch (err) {
          const error = err instanceof Error ? err.message : String(err)
          return json<DesignResponse>({ ok: false, error })
        }
      },
    },
  },
})
