import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const source = () =>
  readFileSync(
    resolve(process.cwd(), 'src/screens/chat/components/chat-composer.tsx'),
    'utf8',
  )

describe('ChatComposer context controls', () => {
  it('wires profile selection through the existing profile APIs', () => {
    const src = source()

    expect(src).toContain("fetch('/api/profiles/list')")
    expect(src).toContain("fetch('/api/profiles/activate'")
    expect(src).toContain('Activated profile')
  })

  it('surfaces workspace and reasoning controls next to the model picker', () => {
    const src = source()

    // Workspace group — files-toggle btn + workspace chip (Hermes parity)
    expect(src).toContain("fetch('/api/workspace')")
    expect(src).toContain('workspaceContextQuery')
    expect(src).toContain('workspaceSelectMutation')
    expect(src).toMatch(/workspaceContextQuery\.data\?\.workspaces[^)]*\)\.map/)
    expect(src).toContain('SEARCH_MODAL_EVENTS.TOGGLE_FILE_EXPLORER')
    expect(src).toContain('aria-label="Toggle workspace files panel"')
    // Reasoning chip — preserved, now conditional
    expect(src).toContain('Reasoning effort')
    expect(src).toContain("['medium', 'Medium']")
    expect(src).toContain("['high', 'High']")
    // Gating now reads provider-capability flag (supportsReasoning).
    // Heuristic isClaude46Model kept as fallback when /api/model/info has no
    // capability metadata (vanilla hermes-agent without enhanced-fork).
    expect(src).toMatch(/thinkingLevel !== 'off' \|\| supportsReasoning/)
    expect(src).toContain('supports_reasoning')
    // Chips are NOT nested under a controls hamburger anymore
    expect(src).not.toContain('isControlsMenuOpen')
  })

  it('surfaces the toolsets chip with apply/clear actions (#493 parity)', () => {
    const src = source()

    expect(src).toContain('useSessionToolsetsStore')
    expect(src).toContain('Session toolsets')
    expect(src).toContain('handleApplyToolsets')
    expect(src).toContain('handleClearToolsets')
    expect(src).toContain('toolsetsMenuRef')
    expect(src).toContain('Clear (global)')
    expect(src).toContain('global default')
    expect(src).toContain('persistedSessionToolsets')
  })

  it('adds a composer divider between icon controls and chip strip', () => {
    const src = source()
    expect(src).toContain('Composer divider')
    expect(src).toContain('aria-hidden="true"')
  })
})
