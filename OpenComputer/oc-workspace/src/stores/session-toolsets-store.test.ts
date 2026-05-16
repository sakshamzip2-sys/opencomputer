/**
 * @vitest-environment jsdom
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { useSessionToolsetsStore } from './session-toolsets-store'

const STORAGE_KEY = 'hermes-workspace.session-toolsets'

describe('useSessionToolsetsStore', () => {
  beforeEach(() => {
    // Reset both the in-memory store and the persisted shape so tests are
    // independent of each other and don't leak via localStorage.
    useSessionToolsetsStore.setState({ toolsets: {} })
    if (typeof localStorage !== 'undefined') localStorage.removeItem(STORAGE_KEY)
  })

  it('returns undefined when no override is set', () => {
    const { getToolsets } = useSessionToolsetsStore.getState()
    expect(getToolsets('session-a')).toBeUndefined()
  })

  it('persists trimmed, non-empty toolset names per session', () => {
    const { setToolsets, getToolsets } = useSessionToolsetsStore.getState()
    setToolsets('session-a', ['  coding  ', '', 'browse'])
    expect(getToolsets('session-a')).toEqual(['coding', 'browse'])
    // Other sessions are unaffected
    expect(getToolsets('session-b')).toBeUndefined()
  })

  it('treats an all-empty input as a clear', () => {
    const { setToolsets, getToolsets } = useSessionToolsetsStore.getState()
    setToolsets('session-a', ['coding'])
    expect(getToolsets('session-a')).toEqual(['coding'])
    setToolsets('session-a', ['  ', ''])
    expect(getToolsets('session-a')).toBeUndefined()
  })

  it('clearToolsets removes the override and leaves siblings intact', () => {
    const { setToolsets, clearToolsets, getToolsets } =
      useSessionToolsetsStore.getState()
    setToolsets('session-a', ['coding'])
    setToolsets('session-b', ['browse'])
    clearToolsets('session-a')
    expect(getToolsets('session-a')).toBeUndefined()
    expect(getToolsets('session-b')).toEqual(['browse'])
  })

  it('null / empty sessionKey is a no-op (no crashes, no writes)', () => {
    const { setToolsets, getToolsets, clearToolsets } =
      useSessionToolsetsStore.getState()
    setToolsets('', ['coding'])
    setToolsets(null as unknown as string, ['coding'])
    expect(getToolsets('')).toBeUndefined()
    expect(getToolsets(null)).toBeUndefined()
    expect(getToolsets(undefined)).toBeUndefined()
    clearToolsets('')
  })
})
