/**
 * Light / dark terminal detection for the TUI.
 *
 * Mirrors opencomputer.cli_ui.theme_detect (Python). Layered detection:
 * env override → COLORFGBG → OSC 11 background probe → dark default.
 *
 * Hermes-CLI parity (doc lines 318-325).
 */

export type ThemeKind = 'light' | 'dark'

export interface Theme {
  kind: ThemeKind
  bgHex: string
}

const HEX_RE = /^[0-9a-fA-F]{6}$/
const OSC11_RE = /rgb:([0-9a-fA-F]+)\/([0-9a-fA-F]+)\/([0-9a-fA-F]+)/

const luminance = (hex: string): number => {
  const r = parseInt(hex.slice(0, 2), 16) / 255
  const g = parseInt(hex.slice(2, 4), 16) / 255
  const b = parseInt(hex.slice(4, 6), 16) / 255
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

const fromEnv = (): Theme | null => {
  const v = (process.env.OPENCOMPUTER_TUI_THEME ?? '').trim().toLowerCase()
  if (v === 'light') return { kind: 'light', bgHex: 'ffffff' }
  if (v === 'dark') return { kind: 'dark', bgHex: '000000' }
  if (HEX_RE.test(v)) {
    return { kind: luminance(v) > 0.5 ? 'light' : 'dark', bgHex: v }
  }
  return null
}

const fromColorFgBg = (): Theme | null => {
  const v = (process.env.COLORFGBG ?? '').trim()
  if (!v) return null
  const parts = v.split(';')
  if (parts.length < 2) return null
  const bg = parseInt(parts[parts.length - 1], 10)
  if (isNaN(bg)) return null
  return { kind: bg >= 8 ? 'light' : 'dark', bgHex: '' }
}

const parseOsc11 = (reply: string): Theme | null => {
  const m = OSC11_RE.exec(reply)
  if (!m) return null
  const hi = (s: string): string => (s + '00').slice(0, 2)
  const hex = hi(m[1]) + hi(m[2]) + hi(m[3])
  return { kind: luminance(hex) > 0.5 ? 'light' : 'dark', bgHex: hex }
}

const realProbe = async (timeoutMs = 200): Promise<string | null> => {
  if (!process.stdout.isTTY || !process.stdin.isTTY) return null
  return new Promise<string | null>((resolve) => {
    let buf = ''
    const onData = (chunk: Buffer): void => {
      buf += chunk.toString('utf8')
      if (buf.endsWith('\x1b\\') || buf.endsWith('\x07')) {
        cleanup()
        resolve(buf)
      }
    }
    const t = setTimeout(() => {
      cleanup()
      resolve(null)
    }, timeoutMs)
    const cleanup = (): void => {
      clearTimeout(t)
      process.stdin.off('data', onData)
      try {
        process.stdin.setRawMode(false)
      } catch {
        /* noop */
      }
      process.stdin.pause()
    }
    try {
      process.stdin.setRawMode(true)
      process.stdin.resume()
      process.stdin.on('data', onData)
      process.stdout.write('\x1b]11;?\x1b\\')
    } catch {
      cleanup()
      resolve(null)
    }
  })
}

export const detectTheme = async (
  opts: { probe?: () => Promise<string | null> } = {},
): Promise<Theme> => {
  const fromEnvT = fromEnv()
  if (fromEnvT) return fromEnvT
  const fromCfb = fromColorFgBg()
  if (fromCfb) return fromCfb
  const probe = opts.probe ?? realProbe
  const reply = await probe()
  if (reply) {
    const t = parseOsc11(reply)
    if (t) return t
  }
  return { kind: 'dark', bgHex: '000000' }
}

// Pure helpers exported for unit tests.
export const _internal = {
  fromEnv,
  fromColorFgBg,
  parseOsc11,
  luminance,
}
