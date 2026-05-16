'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { HugeiconsIcon } from '@hugeicons/react'
import {
  PaintBrush01Icon,
  PlayIcon,
  RefreshIcon,
  StopIcon,
} from '@hugeicons/core-free-icons'
import { toast } from '@/components/ui/toast'

type DesignStatus = {
  running: boolean
  pid: number | null
  port: number
  url: string
  home: string | null
  log_path: string
  /** True when GET / returns the built Next.js SPA. False = daemon is
   *  up but apps/web/out is missing → iframe would 404. */
  web_served: boolean
  error: string | null
}

type DesignResponse = {
  ok: boolean
  status?: DesignStatus
  error?: string
}

const STATUS_QUERY_KEY = ['design', 'status'] as const

async function fetchStatus(): Promise<DesignResponse> {
  const res = await fetch('/api/design')
  return (await res.json()) as DesignResponse
}

async function postAction(
  action: 'start' | 'stop' | 'restart',
): Promise<DesignResponse> {
  const res = await fetch('/api/design', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ action }),
  })
  return (await res.json()) as DesignResponse
}

function notify(
  message: string,
  type: 'success' | 'error' | 'info' = 'info',
): void {
  toast(message, { type })
}

export function DesignScreen() {
  const qc = useQueryClient()
  const iframeRef = useRef<HTMLIFrameElement | null>(null)
  const [iframeKey, setIframeKey] = useState(0)
  const [frameError, setFrameError] = useState<string | null>(null)

  // Polling tiers: 2s while spinning up (daemon down OR up-but-SPA-not-yet
  // served — usually a 1-2s race when daemon binds before the build
  // finishes); 30s in steady state (running + web served). Background
  // refetches are off by default in react-query so we don't burn CPU
  // when the tab is hidden.
  const statusQuery = useQuery({
    queryKey: STATUS_QUERY_KEY,
    queryFn: fetchStatus,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      const fullyReady = query.state.data?.ok && status?.running && status?.web_served
      return fullyReady ? 30_000 : 2_000
    },
    staleTime: 1_000,
  })

  const startMutation = useMutation({
    mutationFn: () => postAction('start'),
    onSuccess: (resp) => {
      if (!resp.ok) {
        notify(resp.error ?? 'Failed to start daemon', 'error')
        return
      }
      qc.setQueryData(STATUS_QUERY_KEY, resp)
      notify('Open Design daemon started', 'success')
    },
    onError: (err) => {
      notify(err instanceof Error ? err.message : 'Start failed', 'error')
    },
  })

  const stopMutation = useMutation({
    mutationFn: () => postAction('stop'),
    onSuccess: (resp) => {
      if (!resp.ok) {
        notify(resp.error ?? 'Failed to stop daemon', 'error')
        return
      }
      qc.setQueryData(STATUS_QUERY_KEY, resp)
      notify('Open Design daemon stopped', 'success')
    },
  })

  const restartMutation = useMutation({
    mutationFn: () => postAction('restart'),
    onSuccess: (resp) => {
      if (!resp.ok) {
        notify(resp.error ?? 'Failed to restart daemon', 'error')
        return
      }
      qc.setQueryData(STATUS_QUERY_KEY, resp)
      setIframeKey((k) => k + 1) // remount iframe after restart
      notify('Open Design daemon restarted', 'success')
    },
  })

  const refreshFrame = useCallback(() => {
    setFrameError(null)
    setIframeKey((k) => k + 1)
  }, [])

  // Clear frame error state when daemon status flips to running — gives
  // the iframe a fresh attempt without the user having to click refresh.
  const isRunning = statusQuery.data?.status?.running ?? false
  useEffect(() => {
    if (isRunning) setFrameError(null)
  }, [isRunning])

  const status = statusQuery.data?.status
  const ocError = !statusQuery.data?.ok ? statusQuery.data?.error : null
  const url = status?.url ?? 'http://127.0.0.1:7456'

  return (
    <div className="flex h-full min-h-0 flex-col bg-[color:var(--theme-bg)] text-[color:var(--theme-text)]">
      {/* ── Header ───────────────────────────────────────────────── */}
      <header className="flex flex-wrap items-center gap-3 border-b border-[color:var(--theme-border)] px-3 py-2 md:px-4 md:py-3">
        <div className="flex items-center gap-2">
          <HugeiconsIcon
            icon={PaintBrush01Icon}
            size={20}
            strokeWidth={1.5}
            className="text-[color:var(--theme-accent)]"
          />
          <div>
            <h1 className="text-base font-medium leading-none md:text-lg">
              Design
            </h1>
            <p className="mt-1 hidden text-xs text-[color:var(--theme-muted)] sm:block">
              Open Design sidecar — local-first design with your coding-agent CLI.
            </p>
          </div>
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          {status?.running && status?.web_served ? (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/10 px-2.5 py-1 text-[11px] font-medium text-emerald-500">
              <span className="size-1.5 rounded-full bg-emerald-500" />
              Ready · port {status.port}
            </span>
          ) : status?.running ? (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-500/10 px-2.5 py-1 text-[11px] font-medium text-amber-500">
              <span className="size-1.5 rounded-full bg-amber-500" />
              SPA missing · port {status.port}
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-500/10 px-2.5 py-1 text-[11px] font-medium text-amber-500">
              <span className="size-1.5 rounded-full bg-amber-500" />
              Stopped
            </span>
          )}

          {status?.running ? (
            <>
              <button
                type="button"
                onClick={() => restartMutation.mutate()}
                disabled={restartMutation.isPending}
                className="inline-flex items-center gap-1.5 rounded-full border border-[color:var(--theme-border)] bg-[color:var(--theme-card)] px-3 py-1.5 text-xs font-medium text-[color:var(--theme-text)] transition-colors hover:bg-[color:var(--theme-hover)] disabled:opacity-50"
                aria-label="Restart daemon"
              >
                <HugeiconsIcon icon={RefreshIcon} size={13} strokeWidth={2} />
                Restart
              </button>
              <button
                type="button"
                onClick={() => stopMutation.mutate()}
                disabled={stopMutation.isPending}
                className="inline-flex items-center gap-1.5 rounded-full bg-red-500/10 px-3 py-1.5 text-xs font-medium text-red-500 transition-colors hover:bg-red-500/20 disabled:opacity-50"
                aria-label="Stop daemon"
              >
                <HugeiconsIcon icon={StopIcon} size={13} strokeWidth={2} />
                Stop
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => startMutation.mutate()}
              disabled={startMutation.isPending}
              className="inline-flex items-center gap-1.5 rounded-full bg-[color:var(--theme-accent)] px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-all hover:scale-[1.02] disabled:opacity-50"
              aria-label="Start daemon"
            >
              <HugeiconsIcon icon={PlayIcon} size={13} strokeWidth={2} />
              {startMutation.isPending ? 'Starting…' : 'Start daemon'}
            </button>
          )}
        </div>
      </header>

      {/* ── Body ─────────────────────────────────────────────────── */}
      <div className="relative min-h-0 flex-1">
        {ocError ? (
          <CenteredCard
            title="Cannot reach OpenComputer"
            body={ocError}
            hint="The `oc` CLI must be on PATH for Hermes to drive the design daemon. If `oc design status` works in your terminal, restart the workspace."
          />
        ) : !status?.running ? (
          <CenteredCard
            title="Open Design daemon is not running"
            body={
              status?.error ??
              'Click "Start daemon" above. First start downloads no extra dependencies — open-design must already be installed.'
            }
            hint={
              status?.home
                ? `Source tree: ${status.home}`
                : 'OPEN_DESIGN_HOME is not set. See the open-design plugin README for setup.'
            }
          />
        ) : !status.web_served ? (
          <CenteredCard
            title="Daemon up, but the SPA isn't built"
            body={
              status.error ??
              'The Next.js export at apps/web/out is missing. The daemon serves the API on this port but cannot render the UI.'
            }
            hint={
              status.home
                ? `Run "pnpm --filter @open-design/web build" in ${status.home}, then click Restart.`
                : 'See the open-design plugin README for build steps.'
            }
            actionLabel="Restart daemon"
            onAction={() => restartMutation.mutate()}
          />
        ) : frameError ? (
          <CenteredCard
            title="Design surface cannot be embedded"
            body={frameError}
            hint={`Open ${url} in a new tab, or set OD_ALLOWED_FRAME_ANCESTORS to permit ${typeof window !== 'undefined' ? window.location.origin : ''}.`}
            actionLabel="Open in new tab"
            onAction={() => window.open(url, '_blank', 'noopener')}
          />
        ) : (
          <iframe
            key={iframeKey}
            ref={iframeRef}
            src={url}
            title="Open Design"
            // Browsers ignore the inner page's CSP frame-ancestors only when
            // the iframe is same-origin OR the daemon explicitly allows the
            // Hermes origin. We retry up to 3s before giving up.
            onLoad={() => setFrameError(null)}
            onError={() =>
              setFrameError('The daemon refused to be framed (CSP / X-Frame-Options).')
            }
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals"
            className="absolute inset-0 size-full border-0"
            referrerPolicy="no-referrer"
          />
        )}

        {/* Floating Refresh — even when running, agent state may stale */}
        {status?.running && !frameError && (
          <button
            type="button"
            onClick={refreshFrame}
            className="absolute bottom-3 right-3 inline-flex size-9 items-center justify-center rounded-full bg-[color:var(--theme-card)] text-[color:var(--theme-text)] shadow-lg ring-1 ring-[color:var(--theme-border)] transition-all hover:scale-105"
            aria-label="Refresh design surface"
            title="Refresh design surface"
          >
            <HugeiconsIcon icon={RefreshIcon} size={15} strokeWidth={2} />
          </button>
        )}
      </div>
    </div>
  )
}

type CenteredCardProps = {
  title: string
  body: string
  hint?: string
  actionLabel?: string
  onAction?: () => void
}

function CenteredCard({ title, body, hint, actionLabel, onAction }: CenteredCardProps) {
  return (
    <div className="absolute inset-0 flex items-center justify-center p-6">
      <div className="max-w-md rounded-2xl border border-[color:var(--theme-border)] bg-[color:var(--theme-card)] p-5 text-center shadow-md">
        <h2 className="text-base font-semibold text-[color:var(--theme-text)]">
          {title}
        </h2>
        <p className="mt-2 text-sm text-[color:var(--theme-muted)]">{body}</p>
        {hint ? (
          <p className="mt-3 text-xs text-[color:var(--theme-muted)]/80">{hint}</p>
        ) : null}
        {onAction ? (
          <button
            type="button"
            onClick={onAction}
            className="mt-4 inline-flex items-center gap-1.5 rounded-full bg-[color:var(--theme-accent)] px-4 py-1.5 text-xs font-medium text-white hover:scale-[1.02]"
          >
            {actionLabel ?? 'Open'}
          </button>
        ) : null}
      </div>
    </div>
  )
}
