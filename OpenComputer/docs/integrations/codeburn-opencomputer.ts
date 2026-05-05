import { readdir, readFile, stat } from 'fs/promises'
import { basename, join } from 'path'
import { homedir } from 'os'

import { calculateCost, getShortModelName } from '../models.js'
import { extractBashCommands } from '../bash-utils.js'
import {
  isSqliteAvailable,
  getSqliteLoadError,
  openDatabase,
  type SqliteDatabase,
} from '../sqlite.js'
import type {
  Provider,
  SessionSource,
  SessionParser,
  ParsedProviderCall,
} from './types.js'

// OpenComputer (https://github.com/sakshamzip2-sys/opencomputer) stores
// each profile in its own subdirectory under ``~/.opencomputer/``:
//
//   ~/.opencomputer/
//     default/
//       sessions.db          ← SQLite: sessions + messages
//       llm_events.jsonl     ← JSONL: per-LLM-call cost telemetry
//       SOUL.md, MEMORY.md, ...
//     work/
//       sessions.db
//       ...
//
// ``sessions.db`` carries session boundaries and per-message tool_calls
// JSON; ``llm_events.jsonl`` carries the canonical per-call cost data
// emitted by ``opencomputer.observability.llm_events``. We hybrid-load:
// enumerate sessions from SQLite, then for each session pull events
// from the JSONL whose ``ts`` falls inside the session's
// ``[started_at, ended_at]`` window.

type SessionRow = {
  id: string
  started_at: number
  ended_at: number | null
  platform: string
  model: string | null
  title: string | null
  message_count: number
  cwd: string | null
}

type MessageRow = {
  id: number
  role: string
  content: string
  tool_calls: string | null
  timestamp: number
}

type LLMEvent = {
  ts: string
  provider: string
  model: string
  input_tokens: number
  output_tokens: number
  cache_creation_tokens: number
  cache_read_tokens: number
  latency_ms: number
  cost_usd: number | null
  site?: string
}

const toolNameMap: Record<string, string> = {
  Bash: 'Bash',
  Read: 'Read',
  Edit: 'Edit',
  MultiEdit: 'Edit',
  Write: 'Write',
  Glob: 'Glob',
  Grep: 'Grep',
  Skill: 'Skill',
  Delegate: 'Agent',
  TodoWrite: 'TodoWrite',
  WebFetch: 'WebFetch',
  WebSearch: 'WebSearch',
}

function getRoot(rootOverride?: string): string {
  return rootOverride ?? join(homedir(), '.opencomputer')
}

function sanitize(p: string): string {
  return p.replace(/^\//, '').replace(/\//g, '-')
}

function tsSeconds(t: number): string {
  const ms = t < 1e12 ? t * 1000 : t
  return new Date(ms).toISOString()
}

function isInWindow(eventISO: string, started: number, ended: number | null): boolean {
  const eventMs = Date.parse(eventISO)
  if (Number.isNaN(eventMs)) return false
  const eventSec = eventMs / 1000
  if (eventSec < started - 1) return false
  if (ended !== null && eventSec > ended + 1) return false
  return true
}

function validateSchema(db: SqliteDatabase): boolean {
  try {
    db.query<{ cnt: number }>(
      "SELECT COUNT(*) AS cnt FROM sessions LIMIT 1",
    )
    db.query<{ cnt: number }>(
      "SELECT COUNT(*) AS cnt FROM messages LIMIT 1",
    )
    return true
  } catch {
    return false
  }
}

async function readEvents(jsonlPath: string): Promise<LLMEvent[]> {
  let raw: string
  try {
    raw = await readFile(jsonlPath, 'utf8')
  } catch {
    return []
  }

  const events: LLMEvent[] = []
  for (const line of raw.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    try {
      events.push(JSON.parse(trimmed) as LLMEvent)
    } catch {
      // Corrupt line — skip rather than fail the whole profile.
    }
  }
  return events
}

function extractTools(toolCallsJSON: string | null): {
  tools: string[]
  bashCommands: string[]
} {
  if (!toolCallsJSON) return { tools: [], bashCommands: [] }
  let parsed: unknown
  try {
    parsed = JSON.parse(toolCallsJSON)
  } catch {
    return { tools: [], bashCommands: [] }
  }
  if (!Array.isArray(parsed)) return { tools: [], bashCommands: [] }

  const tools: string[] = []
  const bashCommands: string[] = []
  for (const call of parsed) {
    if (!call || typeof call !== 'object') continue
    const c = call as { name?: string; arguments?: { command?: unknown } }
    const name = typeof c.name === 'string' ? c.name : ''
    if (!name) continue
    tools.push(toolNameMap[name] ?? name)
    if (name === 'Bash' && c.arguments && typeof c.arguments.command === 'string') {
      bashCommands.push(...extractBashCommands(c.arguments.command))
    }
  }
  return { tools, bashCommands }
}

function createParser(source: SessionSource, seenKeys: Set<string>): SessionParser {
  return {
    async *parse(): AsyncGenerator<ParsedProviderCall> {
      if (!isSqliteAvailable()) {
        process.stderr.write(getSqliteLoadError() + '\n')
        return
      }

      // path encoding: `${dbPath}::${sessionId}` (double-colon to avoid Windows
      // drive-letter ambiguity).
      const idx = source.path.lastIndexOf('::')
      if (idx === -1) return
      const dbPath = source.path.slice(0, idx)
      const sessionId = source.path.slice(idx + 2)

      const profileDir = dbPath.replace(/sessions\.db$/, '')
      const eventsPath = join(profileDir, 'llm_events.jsonl')

      let db: SqliteDatabase
      try {
        db = openDatabase(dbPath)
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err)
        process.stderr.write(
          `codeburn: cannot open OpenComputer database: ${msg}\n`,
        )
        return
      }

      try {
        if (!validateSchema(db)) {
          process.stderr.write(
            'codeburn: OpenComputer storage format not recognized. ' +
            'You may need to update CodeBurn.\n',
          )
          return
        }

        const sessionRows = db.query<SessionRow>(
          'SELECT id, started_at, ended_at, platform, model, title, ' +
            'message_count, cwd FROM sessions WHERE id = ? LIMIT 1',
          [sessionId],
        )
        if (sessionRows.length === 0) return
        const session = sessionRows[0]!

        const messages = db.query<MessageRow>(
          'SELECT id, role, content, tool_calls, timestamp FROM messages ' +
            'WHERE session_id = ? ORDER BY timestamp ASC',
          [sessionId],
        )

        let userMessage = ''
        const messageTools: { tools: string[]; bashCommands: string[] }[] = []
        for (const msg of messages) {
          if (msg.role === 'user' && !userMessage) {
            userMessage = String(msg.content).slice(0, 500)
          }
          if (msg.role === 'assistant') {
            messageTools.push(extractTools(msg.tool_calls))
          }
        }

        const events = await readEvents(eventsPath)
        const sessionEvents = events.filter((e) =>
          isInWindow(e.ts, session.started_at, session.ended_at),
        )

        // If no events match the window, fall back to one synthetic call
        // using the session-level totals so cost still shows up.
        if (sessionEvents.length === 0) {
          const startedISO = tsSeconds(session.started_at)
          const dedupKey = `opencomputer:${sessionId}:fallback`
          if (seenKeys.has(dedupKey)) return
          seenKeys.add(dedupKey)
          const sessionTotalRows = db.query<{
            input_tokens: number
            output_tokens: number
            cache_read_tokens: number
            cache_write_tokens: number
          }>(
            'SELECT input_tokens, output_tokens, cache_read_tokens, ' +
              'cache_write_tokens FROM sessions WHERE id = ?',
            [sessionId],
          )
          const totals = sessionTotalRows[0]
          if (!totals) return
          const allZero =
            totals.input_tokens === 0 &&
            totals.output_tokens === 0 &&
            totals.cache_read_tokens === 0 &&
            totals.cache_write_tokens === 0
          if (allZero) return

          const aggTools: string[] = []
          const aggBash: string[] = []
          for (const t of messageTools) {
            aggTools.push(...t.tools)
            aggBash.push(...t.bashCommands)
          }

          const model = session.model ?? 'opencomputer-auto'
          const costUSD = calculateCost(
            model,
            totals.input_tokens,
            totals.output_tokens,
            totals.cache_write_tokens,
            totals.cache_read_tokens,
            0,
          )

          yield {
            provider: 'opencomputer',
            model,
            inputTokens: totals.input_tokens,
            outputTokens: totals.output_tokens,
            cacheCreationInputTokens: totals.cache_write_tokens,
            cacheReadInputTokens: totals.cache_read_tokens,
            cachedInputTokens: totals.cache_read_tokens,
            reasoningTokens: 0,
            webSearchRequests: 0,
            costUSD,
            costIsEstimated: true,
            tools: [...new Set(aggTools)],
            bashCommands: [...new Set(aggBash)],
            timestamp: startedISO,
            speed: 'standard',
            deduplicationKey: dedupKey,
            userMessage,
            sessionId,
          }
          return
        }

        // Per-event yield. Tool/bash attribution is per-session aggregated
        // because the JSONL doesn't carry tool data — the SQLite messages
        // do, and we already counted them once for the whole session.
        // Distribute across events by attaching to the FIRST event so we
        // don't double-count tools.
        for (let i = 0; i < sessionEvents.length; i++) {
          const e = sessionEvents[i]!
          const dedupKey = `opencomputer:${sessionId}:${e.ts}:${i}`
          if (seenKeys.has(dedupKey)) continue
          seenKeys.add(dedupKey)

          const isFirst = i === 0
          const tools: string[] = isFirst
            ? [...new Set(messageTools.flatMap((m) => m.tools))]
            : []
          const bashCommands: string[] = isFirst
            ? [...new Set(messageTools.flatMap((m) => m.bashCommands))]
            : []

          const fallbackCost = calculateCost(
            e.model,
            e.input_tokens,
            e.output_tokens,
            e.cache_creation_tokens,
            e.cache_read_tokens,
            0,
          )
          const costUSD = e.cost_usd != null && e.cost_usd > 0 ? e.cost_usd : fallbackCost

          yield {
            provider: 'opencomputer',
            model: e.model || 'opencomputer-auto',
            inputTokens: e.input_tokens,
            outputTokens: e.output_tokens,
            cacheCreationInputTokens: e.cache_creation_tokens,
            cacheReadInputTokens: e.cache_read_tokens,
            cachedInputTokens: e.cache_read_tokens,
            reasoningTokens: 0,
            webSearchRequests: 0,
            costUSD,
            costIsEstimated: e.cost_usd == null || e.cost_usd <= 0,
            tools,
            bashCommands,
            timestamp: e.ts,
            speed: 'standard',
            deduplicationKey: dedupKey,
            userMessage,
            sessionId,
          }
        }
      } finally {
        db.close()
      }
    },
  }
}

async function discoverFromDb(dbPath: string): Promise<SessionSource[]> {
  let db: SqliteDatabase
  try {
    db = openDatabase(dbPath)
  } catch {
    return []
  }

  try {
    if (!validateSchema(db)) return []
    const rows = db.query<SessionRow>(
      'SELECT id, started_at, ended_at, platform, model, title, ' +
        'message_count, cwd FROM sessions ORDER BY started_at DESC',
    )
    return rows.map((row) => ({
      path: `${dbPath}::${row.id}`,
      project: row.cwd
        ? sanitize(row.cwd)
        : row.title
          ? sanitize(row.title)
          : 'opencomputer-default',
      provider: 'opencomputer',
    }))
  } catch {
    return []
  } finally {
    db.close()
  }
}

async function listProfileDirs(root: string): Promise<string[]> {
  let entries: string[]
  try {
    entries = await readdir(root)
  } catch {
    return []
  }

  const dirs: string[] = []
  for (const entry of entries) {
    const dbPath = join(root, entry, 'sessions.db')
    try {
      const s = await stat(dbPath)
      if (s.isFile()) dirs.push(dbPath)
    } catch {
      // not a profile dir; skip
    }
  }
  // Also handle the legacy single-DB layout where sessions.db lives at
  // the root (~/.opencomputer/sessions.db).
  const rootDb = join(root, 'sessions.db')
  try {
    const s = await stat(rootDb)
    if (s.isFile()) dirs.push(rootDb)
  } catch {
    // no legacy DB
  }
  return dirs
}

export function createOpenComputerProvider(rootOverride?: string): Provider {
  const root = getRoot(rootOverride)

  return {
    name: 'opencomputer',
    displayName: 'OpenComputer',

    modelDisplayName(model: string): string {
      const stripped = model.replace(/^[^/]+\//, '')
      return getShortModelName(stripped)
    },

    toolDisplayName(rawTool: string): string {
      return toolNameMap[rawTool] ?? rawTool
    },

    async discoverSessions(): Promise<SessionSource[]> {
      if (!isSqliteAvailable()) return []
      const dbPaths = await listProfileDirs(root)
      if (dbPaths.length === 0) return []

      const sessions: SessionSource[] = []
      for (const dbPath of dbPaths) {
        sessions.push(...(await discoverFromDb(dbPath)))
      }
      return sessions
    },

    createSessionParser(
      source: SessionSource,
      seenKeys: Set<string>,
    ): SessionParser {
      return createParser(source, seenKeys)
    },
  }
}

export const opencomputer = createOpenComputerProvider()
