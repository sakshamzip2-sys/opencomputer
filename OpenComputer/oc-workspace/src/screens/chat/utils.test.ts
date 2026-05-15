import { describe, expect, it } from 'vitest'

import type { ChatMessage } from './types'
import { textFromMessage } from './utils'

function userMessage(text: string): ChatMessage {
  return {
    id: 'm1',
    role: 'user',
    content: [{ type: 'text', text }],
    timestamp: Date.now(),
  } as ChatMessage
}

describe('textFromMessage — workspace_context stripping', () => {
  it('strips a <workspace_context active="true" .../> prefix from user text', () => {
    const raw =
      '<workspace_context active="true" name="Home" path="/Users/saksham/workspace" />\n\nhi'
    expect(textFromMessage(userMessage(raw))).toBe('hi')
  })

  it('strips when path contains escaped quotes', () => {
    const raw =
      '<workspace_context active="true" name="weird&quot;name" path="/p" />\n\nhello'
    expect(textFromMessage(userMessage(raw))).toBe('hello')
  })

  it('does not strip non-matching context-like tags', () => {
    const raw = '<workspace_context active="false" path="/x" /> stay'
    // active="false" should not match — preserves text
    expect(textFromMessage(userMessage(raw))).toContain('stay')
  })

  it('passes through user text without a directive untouched', () => {
    expect(textFromMessage(userMessage('how are you'))).toBe('how are you')
  })
})
