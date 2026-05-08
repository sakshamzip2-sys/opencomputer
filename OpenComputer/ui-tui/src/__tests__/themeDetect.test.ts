/**
 * Tests for ui-tui/src/lib/themeDetect.ts.
 *
 * Mirrors the Python tests in tests/cli_ui/test_theme_detect.py to keep
 * the two implementations in sync.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { detectTheme, _internal } from '../lib/themeDetect.js'

const setEnv = (k: string, v: string | undefined): void => {
  if (v === undefined) {
    delete process.env[k]
  } else {
    process.env[k] = v
  }
}

describe('themeDetect — env override', () => {
  beforeEach(() => {
    setEnv('OPENCOMPUTER_TUI_THEME', undefined)
    setEnv('COLORFGBG', undefined)
  })
  afterEach(() => {
    setEnv('OPENCOMPUTER_TUI_THEME', undefined)
    setEnv('COLORFGBG', undefined)
  })

  it('honours env override light', async () => {
    process.env.OPENCOMPUTER_TUI_THEME = 'light'
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('light')
  })

  it('honours env override dark', async () => {
    process.env.OPENCOMPUTER_TUI_THEME = 'dark'
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('dark')
  })

  it('parses 6-char hex bg', async () => {
    process.env.OPENCOMPUTER_TUI_THEME = 'ffffff'
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('light')
    expect(t.bgHex).toBe('ffffff')
  })

  it('falls through on invalid hex', async () => {
    process.env.OPENCOMPUTER_TUI_THEME = 'zzzzzz'
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('dark')
  })
})

describe('themeDetect — COLORFGBG', () => {
  beforeEach(() => {
    setEnv('OPENCOMPUTER_TUI_THEME', undefined)
    setEnv('COLORFGBG', undefined)
  })

  it('parses xterm light', async () => {
    process.env.COLORFGBG = '0;15'
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('light')
  })

  it('parses xterm dark', async () => {
    process.env.COLORFGBG = '15;0'
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('dark')
  })

  it('falls through on garbage', async () => {
    process.env.COLORFGBG = 'garbage'
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('dark')
  })
})

describe('themeDetect — OSC 11 probe', () => {
  beforeEach(() => {
    setEnv('OPENCOMPUTER_TUI_THEME', undefined)
    setEnv('COLORFGBG', undefined)
  })

  it('parses light reply', async () => {
    const t = await detectTheme({
      probe: async () => '\x1b]11;rgb:ffff/ffff/ffff\x1b\\',
    })
    expect(t.kind).toBe('light')
  })

  it('parses dark reply', async () => {
    const t = await detectTheme({
      probe: async () => '\x1b]11;rgb:1010/1010/1010\x1b\\',
    })
    expect(t.kind).toBe('dark')
  })

  it('default dark when probe times out', async () => {
    const t = await detectTheme({ probe: async () => null })
    expect(t.kind).toBe('dark')
  })
})

describe('themeDetect — luminance helper', () => {
  it('white is bright', () => {
    expect(_internal.luminance('ffffff')).toBeGreaterThan(0.99)
  })

  it('black is dark', () => {
    expect(_internal.luminance('000000')).toBe(0)
  })
})
