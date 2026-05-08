/**
 * `useGitBranch` — read the current git branch from .git/HEAD with mtime cache.
 *
 * Hermes-CLI parity (doc lines 350-352). Displays in the TUI status row
 * as `~/projects/foo (main)`. Polls every 5s; only re-reads when HEAD's
 * mtime changes so checked-out branch updates are picked up but the
 * filesystem is touched lightly.
 */

import { promises as fs } from 'fs'
import { join } from 'path'
import { useEffect, useState } from 'react'

/**
 * Pure helper — read the branch name (or short SHA for detached HEAD)
 * from a `.git/HEAD` file under *cwd*. Handles `gitdir:` worktree
 * pointers. Returns null on any error.
 */
export const readBranchFromHead = async (
  cwd: string,
): Promise<string | null> => {
  try {
    let gitPath = join(cwd, '.git')
    const stat = await fs.stat(gitPath).catch(() => null)
    if (!stat) return null
    if (stat.isFile()) {
      const txt = await fs.readFile(gitPath, 'utf8')
      const m = /gitdir:\s*(.+)/.exec(txt)
      if (!m) return null
      gitPath = m[1].trim()
    }
    const head = await fs.readFile(join(gitPath, 'HEAD'), 'utf8')
    const refMatch = /^ref:\s*refs\/heads\/(.+)$/m.exec(head.trim())
    if (refMatch) return refMatch[1]
    return head.trim().slice(0, 7)
  } catch {
    return null
  }
}

export const useGitBranch = (cwd: string): string | null => {
  const [branch, setBranch] = useState<string | null>(null)
  useEffect(() => {
    let mtime = 0
    let cancelled = false
    const tick = async (): Promise<void> => {
      try {
        const stat = await fs.stat(join(cwd, '.git', 'HEAD'))
        if (cancelled) return
        if (stat.mtimeMs !== mtime) {
          mtime = stat.mtimeMs
          setBranch(await readBranchFromHead(cwd))
        }
      } catch {
        if (!cancelled) setBranch(null)
      }
    }
    void tick()
    const id = setInterval(tick, 5_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [cwd])
  return branch
}
