import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'

/**
 * Per-session toolset override (Hermes-parity, #493).
 *
 * Stored locally keyed by sessionKey. Empty array / null means "use the
 * global toolset list from the agent's config". A non-empty array narrows
 * the active toolsets for this chat only — transmitted to the gateway on
 * every send via the `enabled_toolsets` field, mirroring the cron-schedule
 * runtime override shape (see opencomputer/dashboard/routes/cron.py).
 *
 * Cleared automatically when the session is deleted (parallel to model store).
 */
type State = {
  toolsets: Record<string, ReadonlyArray<string>>
}

type Actions = {
  getToolsets: (sessionKey: string | null | undefined) => ReadonlyArray<string> | undefined
  setToolsets: (sessionKey: string, toolsets: ReadonlyArray<string>) => void
  clearToolsets: (sessionKey: string) => void
}

export const useSessionToolsetsStore = create<State & Actions>()(
  persist(
    (set, get) => ({
      toolsets: {},
      getToolsets: (sessionKey) => {
        if (!sessionKey) return undefined
        return get().toolsets[sessionKey]
      },
      setToolsets: (sessionKey, toolsets) => {
        if (!sessionKey) return
        const cleaned = toolsets
          .map((t) => t.trim())
          .filter((t) => t.length > 0)
        set((state) => {
          if (cleaned.length === 0) {
            const { [sessionKey]: _removed, ...rest } = state.toolsets
            void _removed
            return { toolsets: rest }
          }
          return {
            toolsets: { ...state.toolsets, [sessionKey]: cleaned },
          }
        })
      },
      clearToolsets: (sessionKey) => {
        if (!sessionKey) return
        set((state) => {
          const { [sessionKey]: _removed, ...rest } = state.toolsets
          void _removed
          return { toolsets: rest }
        })
      },
    }),
    {
      name: 'hermes-workspace.session-toolsets',
      storage: createJSONStorage(() => localStorage),
    },
  ),
)
