/**
 * Tests for ui-tui/src/hooks/useGitBranch.ts.
 *
 * The hook's `useGitBranch` body wraps a useEffect, which is awkward to
 * test without an Ink renderer. We test the pure helper `readBranchFromHead`
 * directly — that's where the parsing logic lives.
 */

import { describe, it, expect } from 'vitest'
import { promises as fs } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { readBranchFromHead } from '../hooks/useGitBranch.js'

describe('readBranchFromHead', () => {
  it('returns null when .git is missing', async () => {
    const dir = await fs.mkdtemp(join(tmpdir(), 'gb-'))
    expect(await readBranchFromHead(dir)).toBeNull()
  })

  it('parses ref pointer', async () => {
    const dir = await fs.mkdtemp(join(tmpdir(), 'gb-'))
    await fs.mkdir(join(dir, '.git'))
    await fs.writeFile(join(dir, '.git', 'HEAD'), 'ref: refs/heads/main\n')
    expect(await readBranchFromHead(dir)).toBe('main')
  })

  it('returns short sha for detached HEAD', async () => {
    const dir = await fs.mkdtemp(join(tmpdir(), 'gb-'))
    await fs.mkdir(join(dir, '.git'))
    await fs.writeFile(
      join(dir, '.git', 'HEAD'),
      'abc1234567890abcdef1234567890abcdef123456\n',
    )
    expect(await readBranchFromHead(dir)).toBe('abc1234')
  })

  it('follows worktree gitdir pointer', async () => {
    const repo = await fs.mkdtemp(join(tmpdir(), 'gb-repo-'))
    const wt = await fs.mkdtemp(join(tmpdir(), 'gb-wt-'))
    await fs.mkdir(join(repo, '.git'))
    await fs.writeFile(
      join(repo, '.git', 'HEAD'),
      'ref: refs/heads/feature\n',
    )
    // worktree's .git is a file pointing to a folder under main repo's .git
    await fs.writeFile(join(wt, '.git'), `gitdir: ${join(repo, '.git')}\n`)
    expect(await readBranchFromHead(wt)).toBe('feature')
  })
})
