import {
  BEARER_TOKEN,
  CLAUDE_API,
  dashboardFetch,
  ensureGatewayProbed,
  getCapabilities,
} from '@/server/gateway-capabilities'
import { getLocalMessages, getLocalSession } from '@/server/local-session-store'

export type ContextUsageSnapshot = {
  ok: true
  contextPercent: number
  maxTokens: number
  usedTokens: number
  model: string
  staticTokens: number
  conversationTokens: number
}

const MODEL_CONTEXT_WINDOWS: Record<string, number> = {
  'claude-opus-4-6': 200_000,
  'claude-opus-4-5': 200_000,
  'claude-sonnet-4-6': 200_000,
  'claude-sonnet-4-5': 200_000,
  'claude-sonnet-4': 200_000,
  'claude-3-5-sonnet': 200_000,
  'claude-3-opus': 200_000,
  'claude-haiku-3.5': 200_000,
  'gpt-5.4': 1_000_000,
  'gpt-5.2-codex': 1_000_000,
  'gpt-4.1': 1_000_000,
  'gpt-4.1-mini': 1_000_000,
  'gpt-4o': 128_000,
  'gpt-4o-mini': 128_000,
  'gpt-4-turbo': 128_000,
  o1: 200_000,
  'o3-mini': 200_000,
  'gemini-2.5-flash': 1_000_000,
  'gemini-2.5-pro': 1_000_000,
  'kimi-k2.6': 256_000,
}

const CHARS_PER_TOKEN = 3.5

/**
 * Message shape accepted by the local token estimators. Deliberately loose —
 * the same function is called from both gateway-shaped payloads (string
 * `content`) and Anthropic-shaped payloads (structured `content` array).
 *
 * `text` is the legacy top-level field some adapters set in parallel with the
 * structured array; we strip it when it mirrors a content block to avoid
 * counting the same bytes twice.
 */
export type EstimableMessage = {
  content?: string | Array<{ type?: string; text?: string }>
  text?: string
}

/**
 * Estimate context tokens consumed by a message list by serializing each
 * message's content. Structured content arrays (Anthropic-style) are walked
 * block-by-block so tool_result payloads, multi-block text turns, and binary
 * placeholders all contribute their actual character footprint, rather than
 * the route handler's old "string length only" approximation that
 * under-counted by 10-100x on tool-heavy turns.
 *
 * The double-count guard: when `message.text` exactly matches a block's text
 * in the structured array, we drop it (some adapters set both `text` and the
 * matching `content[].text`, so naive summing inflated the estimate).
 *
 * Returns ceil(totalChars / CHARS_PER_TOKEN). Always non-negative.
 */
export function estimateContextTokensFromMessages(
  messages: ReadonlyArray<EstimableMessage>,
): number {
  if (!Array.isArray(messages) || messages.length === 0) return 0

  let totalChars = 0

  for (const message of messages) {
    if (!message || typeof message !== 'object') continue

    let messageChars = 0
    let structuredTexts: Array<string> = []

    if (typeof message.content === 'string') {
      messageChars += message.content.length
    } else if (Array.isArray(message.content)) {
      for (const block of message.content) {
        if (!block || typeof block !== 'object') continue
        const blockText = typeof block.text === 'string' ? block.text : ''
        if (blockText.length > 0) {
          messageChars += blockText.length
          structuredTexts.push(blockText)
        }
      }
    }

    if (typeof message.text === 'string' && message.text.length > 0) {
      const mirrored = structuredTexts.some((t) => t === message.text)
      if (!mirrored) {
        messageChars += message.text.length
      }
    }

    totalChars += messageChars
  }

  if (totalChars <= 0) return 0
  return Math.ceil(totalChars / CHARS_PER_TOKEN)
}

/**
 * Fallback estimator used when we only have a CUMULATIVE cache_read_tokens
 * total (summed across every assistant turn in the session). To approximate
 * the current per-turn context size we divide by the number of assistant
 * turns and apply a 1.2x correction for non-cached prefix tokens.
 *
 * This is intentionally a coarse fallback — callers should prefer
 * `estimateContextTokensFromMessages` when they have the actual message
 * payloads. Returns 0 when inputs are unusable rather than throwing, so
 * surface code can degrade gracefully.
 */
export function estimateContextTokensFromCacheRead(
  cumulativeCacheReadTokens: number,
  assistantTurns: number,
): number {
  if (!Number.isFinite(cumulativeCacheReadTokens) || cumulativeCacheReadTokens <= 0) return 0
  if (!Number.isFinite(assistantTurns) || assistantTurns <= 0) return 0
  return Math.ceil((cumulativeCacheReadTokens / assistantTurns) * 1.2)
}

function getContextWindow(model: string): number {
  if (MODEL_CONTEXT_WINDOWS[model]) return MODEL_CONTEXT_WINDOWS[model]
  for (const [key, value] of Object.entries(MODEL_CONTEXT_WINDOWS)) {
    if (
      model.toLowerCase().includes(key.toLowerCase()) ||
      key.toLowerCase().includes(model.toLowerCase())
    )
      return value
  }
  return 200_000
}

function authHeaders(): Record<string, string> {
  return BEARER_TOKEN ? { Authorization: `Bearer ${BEARER_TOKEN}` } : {}
}

function emptySnapshot(): ContextUsageSnapshot {
  return {
    ok: true,
    contextPercent: 0,
    maxTokens: 0,
    usedTokens: 0,
    model: '',
    staticTokens: 0,
    conversationTokens: 0,
  }
}

export async function readContextUsage(
  sessionId = '',
): Promise<ContextUsageSnapshot> {
  try {
    let sessionData: Record<string, unknown> | null = null
    const explicitSessionId = sessionId.trim()
    const capabilities = await ensureGatewayProbed()

    if (explicitSessionId) {
      const localSession = getLocalSession(explicitSessionId)
      if (localSession) {
        const messages = getLocalMessages(explicitSessionId)
        const totalChars = messages.reduce(
          (sum, msg) => sum + (msg.content || '').length,
          0,
        )
        const usedTokens = Math.ceil(totalChars / CHARS_PER_TOKEN)
        const model = localSession.model || 'gpt-5.5'
        const maxTokens = getContextWindow(model)
        const contextPercent =
          maxTokens > 0 ? Math.round((usedTokens / maxTokens) * 1000) / 10 : 0
        return {
          ok: true,
          contextPercent,
          maxTokens,
          usedTokens,
          model,
          staticTokens: 0,
          conversationTokens: usedTokens,
        }
      }
    }

    if (explicitSessionId) {
      try {
        const res = capabilities.dashboard.available
          ? await dashboardFetch(`/api/sessions/${encodeURIComponent(explicitSessionId)}`, {
              signal: AbortSignal.timeout(3000),
            })
          : await fetch(
              `${CLAUDE_API}/api/sessions/${encodeURIComponent(explicitSessionId)}`,
              {
                headers: authHeaders(),
                signal: AbortSignal.timeout(3000),
              },
            )
        if (res.ok) {
          const data = (await res.json()) as {
            session?: Record<string, unknown>
          } & Record<string, unknown>
          sessionData = capabilities.dashboard.available ? data : (data.session ?? null)
        }
      } catch {
        /* ignore */
      }
    }

    // If the caller asked for a specific session and neither the local store nor
    // the gateway has it, return empty. Falling back to the latest session makes
    // new/portable chats inherit unrelated large context usage in the UI.
    if (explicitSessionId && !sessionData) return emptySnapshot()

    if (!sessionData) {
      try {
        const listRes = capabilities.dashboard.available
          ? await dashboardFetch('/api/sessions?limit=1', {
              signal: AbortSignal.timeout(3000),
            })
          : await fetch(`${CLAUDE_API}/api/sessions?limit=1`, {
              headers: authHeaders(),
              signal: AbortSignal.timeout(3000),
            })
        if (listRes.ok) {
          const listData = (await listRes.json()) as {
            items?: Array<Record<string, unknown>>
            sessions?: Array<Record<string, unknown>>
          }
          const sessions = capabilities.dashboard.available
            ? (listData.sessions ?? [])
            : (listData.items ?? [])
          if (sessions.length > 0) {
            sessionData = sessions[0]
          }
        }
      } catch {
        /* ignore */
      }
    }

    if (!sessionData) return emptySnapshot()

    const model = String(sessionData.model || '')
    const maxTokens = getContextWindow(model)
    const cacheReadTokens = Number(sessionData.cache_read_tokens) || 0
    const messageCount = Number(sessionData.message_count) || 0

    let usedTokens = 0
    const assistantTurns = Math.max(1, Math.ceil(messageCount / 2))

    if (cacheReadTokens > 0 && assistantTurns > 0) {
      usedTokens = Math.ceil((cacheReadTokens / assistantTurns) * 1.2)
    } else if (messageCount > 0) {
      try {
        const targetSessionId = explicitSessionId || String(sessionData.id || '')
        if (targetSessionId) {
          const capabilitiesNow = getCapabilities()
          const msgRes = capabilitiesNow.dashboard.available
            ? await dashboardFetch(
                `/api/sessions/${encodeURIComponent(targetSessionId)}/messages`,
                {
                  signal: AbortSignal.timeout(5000),
                },
              )
            : await fetch(
                `${CLAUDE_API}/api/sessions/${encodeURIComponent(targetSessionId)}/messages`,
                {
                  headers: authHeaders(),
                  signal: AbortSignal.timeout(5000),
                },
              )
          if (msgRes.ok) {
            const msgData = (await msgRes.json()) as {
              items?: Array<{
                content?: string
                tool_calls?: unknown
                reasoning?: string
              }>
              messages?: Array<{
                content?: string
                tool_calls?: unknown
                reasoning?: string
              }>
            }
            const messages = capabilitiesNow.dashboard.available
              ? (msgData.messages ?? [])
              : (msgData.items ?? [])
            let totalChars = 0
            for (const msg of messages) {
              totalChars += (msg.content || '').length
              if (msg.reasoning) totalChars += msg.reasoning.length
              if (msg.tool_calls) totalChars += JSON.stringify(msg.tool_calls).length
            }
            usedTokens = Math.ceil(totalChars / CHARS_PER_TOKEN)
          }
        }
      } catch {
        /* ignore */
      }
    }

    usedTokens = Math.min(usedTokens, maxTokens)
    const contextPercent =
      maxTokens > 0 ? Math.round((usedTokens / maxTokens) * 1000) / 10 : 0

    return {
      ok: true,
      contextPercent,
      maxTokens,
      usedTokens,
      model,
      staticTokens: 0,
      conversationTokens: usedTokens,
    }
  } catch {
    return {
      ok: true,
      contextPercent: 0,
      maxTokens: 128_000,
      usedTokens: 0,
      model: '',
      staticTokens: 0,
      conversationTokens: 0,
    }
  }
}
