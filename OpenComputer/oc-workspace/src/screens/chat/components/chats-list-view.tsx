import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { MoreHorizontal, Pencil, Trash2 } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useDeleteSession } from '../hooks/use-delete-session'
import { useRenameSession } from '../hooks/use-rename-session'
import { fetchSessions, chatQueryKeys } from '../chat-queries'
import { SessionRenameDialog } from './sidebar/session-rename-dialog'
import {
  MenuContent,
  MenuItem,
  MenuRoot,
  MenuTrigger,
} from '@/components/ui/menu'
import {
  AlertDialogRoot,
  AlertDialogContent,
  AlertDialogTitle,
  AlertDialogDescription,
  AlertDialogCancel,
  AlertDialogAction,
} from '@/components/ui/alert-dialog'

/**
 * Claude.ai-style "Chats" page at /chats — faithful visual port of
 * claude.ai/recents. Renders inside WorkspaceShell so the OC sidebar
 * stays visible, just like claude.ai keeps its own left rail.
 *
 * Layout cues taken from claude.ai/recents:
 *   - 860px content column, generous top padding
 *   - Heading "Chats" left, "Select chats" + "New chat" pills right
 *   - Full-width search input with leading magnifier
 *   - Time-bucketed list:
 *       • First bucket (today + yesterday): no header
 *       • "Recents": 2–30 days
 *       • "Older": > 30 days
 *   - Each row: title left, relative timestamp right, hover kebab menu
 *     with Rename / Delete actions
 */

export type ChatSessionRow = {
  key?: string
  friendlyId?: string
  title?: string
  derivedTitle?: string
  label?: string
  updatedAt?: number
}

type ChatsListViewProps = {
  /** Optional override for the sessions list. When omitted, the view fetches
      /api/sessions via TanStack Query — same source as the sidebar Recents. */
  sessions?: Array<ChatSessionRow>
  activeFriendlyId?: string
  activeSessionKey?: string
}

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const HEX8_PATTERN = /^[0-9a-f]{8}$/i

function formatRelative(updatedAt: number | undefined): string {
  if (!updatedAt) return ''
  const now = Date.now()
  const diff = Math.max(0, now - updatedAt)
  const minute = 60_000
  const hour = 60 * minute
  const day = 24 * hour
  if (diff < minute) return 'just now'
  if (diff < hour) {
    const m = Math.floor(diff / minute)
    return `${m} ${m === 1 ? 'minute' : 'minutes'} ago`
  }
  if (diff < day) {
    const h = Math.floor(diff / hour)
    return `${h} ${h === 1 ? 'hour' : 'hours'} ago`
  }
  if (diff < 2 * day) return 'yesterday'
  if (diff < 7 * day) {
    const d = Math.floor(diff / day)
    return `${d} days ago`
  }
  if (diff < 30 * day) {
    const w = Math.floor(diff / (7 * day))
    return w === 1 ? 'last week' : `${w} weeks ago`
  }
  if (diff < 365 * day) {
    const mo = Math.floor(diff / (30 * day))
    return mo === 1 ? 'last month' : `${mo} months ago`
  }
  const y = Math.floor(diff / (365 * day))
  return y === 1 ? 'last year' : `${y} years ago`
}

/**
 * Claude.ai shows ONE section header — "Recents" — between the most-recent
 * items and the older history. We follow the same shape:
 *   - first bucket has no header (today + yesterday)
 *   - "Recents" covers 2–30 days
 *   - "Older" covers > 30 days
 */
function timeBucket(
  updatedAt: number | undefined,
): 'today' | 'recents' | 'older' {
  if (!updatedAt) return 'older'
  const diff = Date.now() - updatedAt
  const day = 24 * 60 * 60 * 1000
  if (diff < 2 * day) return 'today'
  if (diff < 30 * day) return 'recents'
  return 'older'
}

function isPlaceholderLabel(value: string): boolean {
  const trimmed = value.trim()
  if (!trimmed) return true
  if (UUID_PATTERN.test(trimmed)) return true
  if (HEX8_PATTERN.test(trimmed)) return true
  // Sessions whose title-generator hasn't fired yet sometimes carry a tag like
  // "(SILENT)" or "(EMPTY)" — those read as junk in a Claude-style list.
  if (/^\([A-Z]+\)$/.test(trimmed)) return true
  return false
}

function sessionTitle(s: ChatSessionRow): string {
  const candidates = [s.label, s.derivedTitle, s.title]
  for (const c of candidates) {
    if (typeof c !== 'string') continue
    const trimmed = c.trim()
    if (!trimmed) continue
    if (isPlaceholderLabel(trimmed)) continue
    return trimmed
  }
  // Fallback: short friendly-id chip, but framed as a session so the row
  // never looks like raw data.
  const short = (s.friendlyId || s.key || '').slice(0, 8)
  return short ? `Session ${short}` : 'New chat'
}

function sessionId(s: ChatSessionRow): string {
  return s.key || s.friendlyId || ''
}

export function ChatsListView({
  sessions: overrideSessions,
  activeFriendlyId,
  activeSessionKey,
}: ChatsListViewProps) {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [selectMode, setSelectMode] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [confirmOpen, setConfirmOpen] = useState(false)
  const searchInputRef = useRef<HTMLInputElement | null>(null)
  const { deleteSession, deleting } = useDeleteSession()
  const { renameSession } = useRenameSession()

  // Per-row rename + delete dialogs
  const [renameTarget, setRenameTarget] = useState<ChatSessionRow | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ChatSessionRow | null>(null)

  const query = useQuery({
    queryKey: chatQueryKeys.sessions,
    queryFn: fetchSessions,
    enabled: !overrideSessions,
    staleTime: 5_000,
    refetchOnWindowFocus: false,
  })
  const sessions: Array<ChatSessionRow> = overrideSessions || query.data || []

  useEffect(() => {
    const id = window.setTimeout(() => {
      searchInputRef.current?.focus()
    }, 100)
    return () => window.clearTimeout(id)
  }, [])

  // Esc exits select mode (it doesn't close the page — this is a route)
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      if (confirmOpen || renameTarget || deleteTarget) return
      if (selectMode) {
        setSelectMode(false)
        setSelected(new Set())
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [selectMode, confirmOpen, renameTarget, deleteTarget])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    const list = sessions.filter((s) => sessionId(s))
    const matched = q
      ? list.filter((s) =>
          [sessionTitle(s), s.friendlyId, s.key]
            .filter(Boolean)
            .some((v) => String(v).toLowerCase().includes(q)),
        )
      : list
    return [...matched].sort((a, b) => {
      const ta = Number(a.updatedAt || 0)
      const tb = Number(b.updatedAt || 0)
      return tb - ta
    })
  }, [sessions, search])

  /**
   * Group rows by time bucket, preserving sort order. We render the bucket
   * label as a section header above its first row (except for "today", which
   * Claude leaves un-labeled).
   */
  const grouped = useMemo(() => {
    type Group = {
      bucket: 'today' | 'recents' | 'older'
      header: string | null
      items: Array<ChatSessionRow>
    }
    const buckets: Record<Group['bucket'], Group> = {
      today: { bucket: 'today', header: null, items: [] },
      recents: { bucket: 'recents', header: 'Recents', items: [] },
      older: { bucket: 'older', header: 'Older', items: [] },
    }
    for (const s of filtered) {
      buckets[timeBucket(s.updatedAt)].items.push(s)
    }
    return [buckets.today, buckets.recents, buckets.older].filter(
      (g) => g.items.length > 0,
    )
  }, [filtered])

  const allIds = useMemo(() => filtered.map((s) => sessionId(s)), [filtered])
  const allSelected = allIds.length > 0 && allIds.every((id) => selected.has(id))

  const enterSelectMode = () => {
    setSelectMode(true)
    setSelected(new Set())
  }
  const exitSelectMode = () => {
    setSelectMode(false)
    setSelected(new Set())
  }
  const toggleId = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  const toggleAll = () => {
    if (allSelected) setSelected(new Set())
    else setSelected(new Set(allIds))
  }
  const handleRowClick = (s: ChatSessionRow) => {
    const id = sessionId(s)
    if (selectMode) {
      toggleId(id)
      return
    }
    const key = s.friendlyId || s.key || id
    if (!key) return
    void navigate({ to: '/chat/$sessionKey', params: { sessionKey: key } })
  }
  const performBulkDelete = async () => {
    const ids = [...selected]
    if (ids.length === 0) return
    const map = new Map<string, ChatSessionRow>()
    sessions.forEach((s) => map.set(sessionId(s), s))
    for (const id of ids) {
      const s = map.get(id)
      if (!s) continue
      const isActive =
        (activeFriendlyId && s.friendlyId === activeFriendlyId) ||
        (activeSessionKey && s.key === activeSessionKey) ||
        false
      try {
        await deleteSession(s.key || '', s.friendlyId || '', Boolean(isActive))
      } catch {
        // Errors surface via useDeleteSession; continue so partial failures
        // don't strand the user in a stale state.
      }
    }
    exitSelectMode()
  }

  const performRowDelete = async (s: ChatSessionRow) => {
    const isActive =
      (activeFriendlyId && s.friendlyId === activeFriendlyId) ||
      (activeSessionKey && s.key === activeSessionKey) ||
      false
    try {
      await deleteSession(s.key || '', s.friendlyId || '', Boolean(isActive))
    } catch {
      // surfaced via the hook
    }
  }

  return (
    <div className="h-full w-full overflow-y-auto bg-[#0f1011]">
      <div className="mx-auto flex w-full max-w-[860px] flex-col gap-7 px-8 pt-16 pb-12 md:pt-20">
        <header className="flex items-start justify-between gap-4">
          <h1 className="m-0 text-[28px] font-semibold leading-tight tracking-[-0.01em] text-white">
            Chats
          </h1>
          {selectMode ? (
            <div className="flex flex-wrap items-center justify-end gap-2.5">
              <span className="select-none pr-1 text-[13px] text-white/55">
                {selected.size} selected
              </span>
              <ChatsBtn onClick={toggleAll} filled>
                {allSelected ? 'Deselect all' : 'Select all'}
              </ChatsBtn>
              <ChatsBtn
                onClick={() => {}}
                disabled
                title="Projects are not available in this workspace"
              >
                Move to project
              </ChatsBtn>
              <ChatsBtn
                onClick={() => setConfirmOpen(true)}
                disabled={selected.size === 0 || deleting}
              >
                {deleting ? 'Deleting…' : 'Delete'}
              </ChatsBtn>
              <ChatsBtn onClick={exitSelectMode}>Cancel</ChatsBtn>
            </div>
          ) : (
            <div className="flex items-center gap-2.5">
              <ChatsBtn onClick={enterSelectMode}>Select chats</ChatsBtn>
              <ChatsBtn
                filled
                onClick={() => {
                  void navigate({
                    to: '/chat/$sessionKey',
                    params: { sessionKey: 'new' },
                  })
                }}
              >
                New chat
              </ChatsBtn>
            </div>
          )}
        </header>

        <div className="relative">
          <svg
            className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-white/45"
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <circle cx="11" cy="11" r="8" />
            <path d="M21 21l-4.35-4.35" />
          </svg>
          <input
            ref={searchInputRef}
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search chats..."
            autoComplete="off"
            className="h-12 w-full rounded-[14px] border border-transparent bg-white/[0.045] pl-12 pr-4 text-[14px] text-white outline-none transition-colors placeholder:text-white/45 hover:bg-white/[0.06] focus:border-white/15 focus:bg-white/[0.045]"
          />
        </div>

        <div className="-mx-2 px-2">
          {query.isLoading && !overrideSessions ? (
            <div className="py-16 text-center text-[13.5px] text-white/50">
              Loading chats…
            </div>
          ) : filtered.length === 0 ? (
            <div className="py-16 text-center text-[13.5px] text-white/50">
              {search.trim()
                ? `No chats match "${search}".`
                : 'No chats yet — start a new conversation to see it here.'}
            </div>
          ) : (
            <ChatGroupsList
              groups={grouped}
              selectMode={selectMode}
              selected={selected}
              activeFriendlyId={activeFriendlyId}
              activeSessionKey={activeSessionKey}
              onRowClick={handleRowClick}
              onAskRename={(s) => setRenameTarget(s)}
              onAskDelete={(s) => setDeleteTarget(s)}
            />
          )}
        </div>
      </div>

      <AlertDialogRoot open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <div className="p-5">
            <AlertDialogTitle>
              Delete {selected.size === 1 ? 'this chat' : `${selected.size} chats`}?
            </AlertDialogTitle>
            <AlertDialogDescription className="mt-2">
              This cannot be undone. The conversation history will be permanently
              removed from this workspace.
            </AlertDialogDescription>
            <div className="mt-5 flex justify-end gap-2">
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                onClick={() => {
                  setConfirmOpen(false)
                  void performBulkDelete()
                }}
              >
                Delete
              </AlertDialogAction>
            </div>
          </div>
        </AlertDialogContent>
      </AlertDialogRoot>

      <SessionRenameDialog
        open={Boolean(renameTarget)}
        onOpenChange={(open) => {
          if (!open) setRenameTarget(null)
        }}
        sessionTitle={renameTarget ? sessionTitle(renameTarget) : ''}
        onSave={async (newTitle) => {
          const t = renameTarget
          setRenameTarget(null)
          if (!t || !newTitle.trim()) return
          await renameSession(t.key || '', t.friendlyId || null, newTitle)
        }}
        onCancel={() => setRenameTarget(null)}
      />

      <AlertDialogRoot
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null)
        }}
      >
        <AlertDialogContent>
          <div className="p-5">
            <AlertDialogTitle>Delete this chat?</AlertDialogTitle>
            <AlertDialogDescription className="mt-2">
              This cannot be undone. The conversation history will be permanently
              removed from this workspace.
            </AlertDialogDescription>
            <div className="mt-5 flex justify-end gap-2">
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                onClick={() => {
                  const t = deleteTarget
                  setDeleteTarget(null)
                  if (t) void performRowDelete(t)
                }}
              >
                Delete
              </AlertDialogAction>
            </div>
          </div>
        </AlertDialogContent>
      </AlertDialogRoot>
    </div>
  )
}

type ChatGroupsListProps = {
  groups: Array<{
    bucket: 'today' | 'recents' | 'older'
    header: string | null
    items: Array<ChatSessionRow>
  }>
  selectMode: boolean
  selected: Set<string>
  activeFriendlyId?: string
  activeSessionKey?: string
  onRowClick: (s: ChatSessionRow) => void
  onAskRename: (s: ChatSessionRow) => void
  onAskDelete: (s: ChatSessionRow) => void
}

function ChatGroupsList({
  groups,
  selectMode,
  selected,
  activeFriendlyId,
  activeSessionKey,
  onRowClick,
  onAskRename,
  onAskDelete,
}: ChatGroupsListProps) {
  return (
    <div className="flex flex-col">
      {groups.map((group, groupIdx) => (
        <section key={group.bucket} className={cn(groupIdx > 0 && 'mt-4')}>
          {group.header ? (
            <h2 className="m-0 mb-2 px-3 pt-1 text-[12.5px] font-medium leading-none tracking-[-0.005em] text-white/45">
              {group.header}
            </h2>
          ) : null}
          <ul className="m-0 flex list-none flex-col p-0">
            {group.items.map((s, idx) => {
              const id = sessionId(s)
              const isActive =
                (activeFriendlyId && s.friendlyId === activeFriendlyId) ||
                (activeSessionKey && s.key === activeSessionKey)
              const isSelected = selected.has(id)
              const title = sessionTitle(s)
              const time = formatRelative(s.updatedAt)
              return (
                <li
                  key={id || idx}
                  className={cn(
                    'group relative flex cursor-pointer select-none items-center gap-3.5 rounded-[10px] px-3 py-[14px] transition-colors',
                    'hover:bg-white/[0.05]',
                    isSelected && 'bg-white/[0.06]',
                    idx !== 0 && 'border-t border-white/[0.045]',
                  )}
                  onClick={() => onRowClick(s)}
                >
                  {selectMode && (
                    <span
                      className={cn(
                        'flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-[5px] border transition-colors',
                        isSelected
                          ? 'border-white bg-white'
                          : 'border-white/30 bg-transparent',
                      )}
                    >
                      {isSelected && (
                        <svg
                          width="11"
                          height="11"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="3.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          className="text-[#0f1011]"
                          aria-hidden="true"
                        >
                          <polyline points="20 6 9 17 4 12" />
                        </svg>
                      )}
                    </span>
                  )}
                  <span
                    className={cn(
                      'min-w-0 flex-1 truncate text-[14.5px] leading-tight text-white/90',
                      isActive && 'font-medium text-white',
                    )}
                  >
                    {title}
                  </span>
                  <span className="ml-3 shrink-0 text-[13px] leading-tight text-white/50">
                    {time}
                  </span>
                  {!selectMode && (
                    <MenuRoot>
                      <MenuTrigger
                        type="button"
                        onClick={(event) => {
                          event.preventDefault()
                          event.stopPropagation()
                        }}
                        className={cn(
                          'ml-1 inline-flex size-7 shrink-0 items-center justify-center rounded-md text-white/55 transition-colors',
                          'opacity-0 hover:bg-white/[0.08] hover:text-white group-hover:opacity-100',
                          'aria-expanded:opacity-100',
                        )}
                        aria-label="Chat options"
                      >
                        <MoreHorizontal size={16} strokeWidth={1.75} />
                      </MenuTrigger>
                      <MenuContent side="bottom" align="end">
                        <MenuItem
                          onClick={(event) => {
                            event.preventDefault()
                            event.stopPropagation()
                            onAskRename(s)
                          }}
                          className="gap-2"
                        >
                          <Pencil size={16} strokeWidth={1.5} /> Rename
                        </MenuItem>
                        <MenuItem
                          onClick={(event) => {
                            event.preventDefault()
                            event.stopPropagation()
                            onAskDelete(s)
                          }}
                          className="gap-2 text-red-700 hover:bg-red-50 data-highlighted:bg-red-50/80 dark:hover:bg-red-900/30"
                        >
                          <Trash2 size={16} strokeWidth={1.5} /> Delete
                        </MenuItem>
                      </MenuContent>
                    </MenuRoot>
                  )}
                </li>
              )
            })}
          </ul>
        </section>
      ))}
    </div>
  )
}

type ChatsBtnProps = {
  onClick: () => void
  children: React.ReactNode
  filled?: boolean
  disabled?: boolean
  title?: string
}

function ChatsBtn({
  onClick,
  children,
  filled = false,
  disabled = false,
  title,
}: ChatsBtnProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={cn(
        'inline-flex items-center justify-center whitespace-nowrap rounded-[10px] border px-3.5 py-[7px] text-[13.5px] font-medium leading-tight transition-all',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/30',
        disabled && 'cursor-not-allowed opacity-45',
        filled
          ? 'border-white bg-white text-[#0f1011] hover:bg-white/95'
          : 'border-white/[0.18] bg-transparent text-white hover:bg-white/[0.06]',
      )}
    >
      {children}
    </button>
  )
}
