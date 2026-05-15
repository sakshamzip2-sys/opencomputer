import {
  Brain,
  ChevronDown,
  CircleUser,
  Clock,
  FileText,
  KanbanSquare,
  LayoutDashboard,
  MessageSquare,
  MessagesSquare,
  Moon,
  Palette,
  PanelLeftClose,
  PanelLeftOpen,
  Puzzle,
  Rocket,
  Search,
  Server,
  Settings,
  SquarePen,
  SquareTerminal,
  Sun,
  Users,
  UsersRound,
  type LucideIcon,
} from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useRouterState } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { CHAT_OPEN_SETTINGS_EVENT } from '../chat-events'
import { useChatSettings as useSidebarSettings } from '../hooks/use-chat-settings'
import { useDeleteSession } from '../hooks/use-delete-session'
import { useRenameSession } from '../hooks/use-rename-session'
import { ProvidersDialog } from './providers-dialog'
import { SessionRenameDialog } from './sidebar/session-rename-dialog'
import { SessionDeleteDialog } from './sidebar/session-delete-dialog'
import { SidebarSessions } from './sidebar/sidebar-sessions'
import type { ChatOpenSettingsDetail } from '../chat-events'
import type { SessionMeta } from '../types'
import { t } from '@/lib/i18n'
import { SettingsDialog } from '@/components/settings-dialog'
import {
  TooltipContent,
  TooltipProvider,
  TooltipRoot,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { Button, buttonVariants } from '@/components/ui/button'
import { UserAvatar } from '@/components/avatars'
import { SEARCH_MODAL_EVENTS, useSearchModal } from '@/hooks/use-search-modal'
import {
  selectChatProfileAvatarDataUrl,
  selectChatProfileDisplayName,
  selectSidebarHoverExpand,
  useChatSettingsStore,
} from '@/hooks/use-chat-settings'
import { StatusDot } from '@/components/status-indicator'
import {
  MenuContent,
  MenuItem,
  MenuRoot,
  MenuTrigger,
} from '@/components/ui/menu'
import { applyTheme, useSettingsStore } from '@/hooks/use-settings'

type WorkspaceStats = Record<string, unknown>

// ── Footer subtitle data ───────────────────────────────────────────────
// Replaces a hardcoded "Pro plan" string. Pulls the active model + chat
// mode from /api/connection-status (the endpoint the app already polls
// for startup health). Falls back to the active theme when the backend
// is unreachable so the chip never shows a misleading value.

type ConnectionStatusPayload = {
  activeModel?: unknown
  chatMode?: unknown
  status?: unknown
}

async function fetchFooterSubtitle(): Promise<string | null> {
  const controller = new AbortController()
  const timeoutId = globalThis.setTimeout(() => controller.abort(), 4000)
  try {
    const response = await fetch('/api/connection-status', {
      signal: controller.signal,
      cache: 'no-store',
    })
    if (!response.ok) {
      // Don't log at warn for expected 503 (gateway down) — that's a
      // normal offline state, not an error worth user attention.
      if (response.status !== 503) {
        console.warn(
          `[sidebar-footer] connection-status returned ${response.status}`,
        )
      }
      return null
    }
    const body = (await response.json()) as ConnectionStatusPayload
    const model =
      typeof body.activeModel === 'string' && body.activeModel.trim()
        ? body.activeModel.trim()
        : null
    if (!model) return null
    // Strip any provider prefix like "anthropic/" for compact display.
    const display = model.includes('/') ? model.split('/').pop() || model : model
    return display
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      console.debug('[sidebar-footer] connection-status timed out')
      return null
    }
    console.warn('[sidebar-footer] connection-status failed:', error)
    return null
  } finally {
    globalThis.clearTimeout(timeoutId)
  }
}

function ThemeToggleMini() {
  const _theme = useSettingsStore((state) => state.settings.theme)
  const updateSettings = useSettingsStore((state) => state.updateSettings)
  void _theme
  // Detect dark/light from actual data-theme attribute
  const currentDataTheme =
    typeof document !== 'undefined'
      ? document.documentElement.getAttribute('data-theme') || 'claude-nous'
      : 'claude-nous'
  const isDark = !currentDataTheme.endsWith('-light')

  // Map between dark and light counterparts — must include all theme families
  const LIGHT_DARK_PAIRS: Record<string, string> = {
    'claude-nous': 'claude-nous-light',
    'claude-nous-light': 'claude-nous',
    'claude-official': 'claude-official-light',
    'claude-official-light': 'claude-official',
    'claude-classic': 'claude-classic-light',
    'claude-classic-light': 'claude-classic',
    'claude-slate': 'claude-slate-light',
    'claude-slate-light': 'claude-slate',
  }

  return (
    <button
      type="button"
      onClick={() => {
        // Fall back to current family rather than dropping the user into claude-official
        const nextDataTheme =
          LIGHT_DARK_PAIRS[currentDataTheme] ||
          (isDark
            ? `${currentDataTheme}-light`
            : currentDataTheme.replace(/-light$/, ''))
        // Import and call setTheme to persist and apply
        import('@/lib/theme').then(({ setTheme }) => {
          setTheme(nextDataTheme as any)
        })
        // Also update settings hook
        const nextMode = nextDataTheme.endsWith('-light') ? 'light' : 'dark'
        applyTheme(nextMode)
        updateSettings({ theme: nextMode })
      }}
      className="sidebar-row shrink-0 rounded-md p-1.5"
      style={{ color: 'var(--sidebar-muted)' }}
      aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
    >
      {isDark ? (
        <Sun size={16} strokeWidth={1.5} />
      ) : (
        <Moon size={16} strokeWidth={1.5} />
      )}
    </button>
  )
}

type ChatSidebarProps = {
  sessions: Array<SessionMeta>
  activeFriendlyId: string
  creatingSession: boolean
  onCreateSession: () => void
  isCollapsed: boolean
  onToggleCollapse: () => void
  onSelectSession?: () => void
  onActiveSessionDelete?: () => void
  sessionsLoading: boolean
  sessionsFetching: boolean
  sessionsError: string | null
  onRetrySessions: () => void
}

// ── Reusable nav item ───────────────────────────────────────────────────

type NavItemDef = {
  kind: 'link' | 'button'
  to?: string
  search?: Record<string, unknown>
  hash?: string
  icon: LucideIcon
  label: string
  active: boolean
  onClick?: () => void
  disabled?: boolean
  badge?: 'error-dot' | string | number
  dataTour?: string
}

export async function fetchWorkspaceStats(): Promise<WorkspaceStats | null> {
  try {
    const response = await fetch('/api/workspace/stats')
    if (!response.ok) return null
    return (await response.json()) as WorkspaceStats
  } catch {
    return null
  }
}

export async function fetchWorkspaceProjectShortcuts(): Promise<Array<never>> {
  return []
}

function NavItem({
  item,
  isCollapsed,
  transition,
  onSelectSession,
}: {
  item: NavItemDef
  isCollapsed: boolean
  transition: Record<string, unknown>
  onSelectSession?: () => void
}) {
  const cls = cn(
    buttonVariants({ variant: 'ghost', size: 'sm' }),
    'sidebar-row w-full h-auto min-h-9 gap-3 py-1.5 md:min-h-0 rounded-md',
    isCollapsed ? 'justify-center px-0' : 'justify-start px-2.5',
    item.active && 'is-active',
  )

  const Icon = item.icon
  const iconEl =
    item.badge === 'error-dot' ? (
      <span className="relative inline-flex size-[17px] shrink-0 items-center justify-center">
        <Icon size={17} strokeWidth={1.5} className="size-[17px] shrink-0" />
        <span className="absolute -top-0.5 -right-0.5 size-2 rounded-full bg-red-500" />
      </span>
    ) : (
      <Icon size={17} strokeWidth={1.5} className="size-[17px] shrink-0" />
    )

  const labelEl = (
    <AnimatePresence initial={false} mode="wait">
      {!isCollapsed ? (
        <motion.span
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={transition}
          className="flex min-w-0 items-center gap-2"
        >
          <span className="overflow-hidden whitespace-nowrap">
            {item.label}
          </span>
          {item.badge && item.badge !== 'error-dot' ? (
            <span
              className={cn(
                'ml-auto inline-flex min-w-5 items-center justify-center rounded-full px-2 py-0.5 text-[9px] font-semibold leading-none uppercase tracking-wider',
                item.badge === 'NEW' && 'sidebar-new-badge',
              )}
            >
              {item.badge}
            </span>
          ) : null}
        </motion.span>
      ) : null}
    </AnimatePresence>
  )

  const handleSelect = () => {
    onSelectSession?.()
  }

  if (item.kind === 'link') {
    if (isCollapsed) {
      return (
        <TooltipProvider>
          <TooltipRoot>
            <TooltipTrigger
              render={
                <Link
                  to={item.to}
                  hash={item.hash}
                  onClick={handleSelect}
                  className={cls}
                  data-tour={item.dataTour}
                >
                  {iconEl}
                </Link>
              }
            />
            <TooltipContent side="right">{item.label}</TooltipContent>
          </TooltipRoot>
        </TooltipProvider>
      )
    }
    return (
      <Link
        to={item.to}
        hash={item.hash}
        onClick={handleSelect}
        className={cls}
        data-tour={item.dataTour}
      >
        {iconEl}
        {labelEl}
      </Link>
    )
  }

  if (isCollapsed) {
    return (
      <TooltipProvider>
        <TooltipRoot>
          <TooltipTrigger
            render={
              <Button
                disabled={item.disabled}
                variant="ghost"
                size="sm"
                onClick={() => {
                  item.onClick?.()
                  handleSelect()
                }}
                className={cls}
                data-tour={item.dataTour}
              >
                {iconEl}
              </Button>
            }
          />
          <TooltipContent side="right">{item.label}</TooltipContent>
        </TooltipRoot>
      </TooltipProvider>
    )
  }

  return (
    <Button
      disabled={item.disabled}
      variant="ghost"
      size="sm"
      onClick={() => {
        item.onClick?.()
        handleSelect()
      }}
      className={cls}
      data-tour={item.dataTour}
    >
      {iconEl}
      {labelEl}
    </Button>
  )
}

// ── Last-visited route tracking ─────────────────────────────────────────

const LAST_ROUTE_KEY = 'claude-sidebar-last-route'

function getLastRoute(section: string): string | null {
  try {
    const stored = localStorage.getItem(LAST_ROUTE_KEY)
    if (!stored) return null
    const map = JSON.parse(stored) as Record<string, string>
    return map[section] || null
  } catch {
    return null
  }
}

function setLastRoute(section: string, route: string) {
  try {
    const stored = localStorage.getItem(LAST_ROUTE_KEY)
    const map = stored ? (JSON.parse(stored) as Record<string, string>) : {}
    map[section] = route
    localStorage.setItem(LAST_ROUTE_KEY, JSON.stringify(map))
  } catch {
    // ignore
  }
}

// ── Section header ──────────────────────────────────────────────────────

function SectionLabel({
  label,
  isCollapsed,
  transition,
  collapsible,
  expanded,
  onToggle,
  navigateTo,
}: {
  label: string
  isCollapsed: boolean
  transition: Record<string, unknown>
  collapsible?: boolean
  expanded?: boolean
  onToggle?: () => void
  navigateTo?: string
}) {
  if (isCollapsed) return null

  const labelContent = (
    <span className="sidebar-section-label text-[12px] font-normal select-none">
      {label}
    </span>
  )

  if (collapsible) {
    return (
      <motion.div
        layout
        transition={{ layout: transition }}
        className="flex items-center gap-1.5 px-3 pt-4 pb-1 w-full"
      >
        {navigateTo ? (
          <Link
            to={navigateTo}
            className="sidebar-section-label text-[12px] font-normal select-none transition-colors hover:opacity-80"
          >
            {label}
          </Link>
        ) : (
          labelContent
        )}
        <button
          type="button"
          onClick={onToggle}
          className="sidebar-row ml-auto p-0.5 rounded transition-colors"
          aria-label={expanded ? `Collapse ${label}` : `Expand ${label}`}
        >
          <ChevronDown
            size={12}
            strokeWidth={1.5}
            className={cn(
              'transition-transform duration-150 opacity-60',
              expanded ? 'rotate-0' : '-rotate-90',
            )}
            style={{ color: 'var(--sidebar-dim)' }}
          />
        </button>
      </motion.div>
    )
  }

  return (
    <motion.div
      layout
      transition={{ layout: transition }}
      className="px-3 pt-4 pb-1"
    >
      {navigateTo ? (
        <Link
          to={navigateTo}
          className="sidebar-section-label text-[12px] font-normal select-none transition-colors hover:opacity-80"
        >
          {label}
        </Link>
      ) : (
        labelContent
      )}
    </motion.div>
  )
}

// ── Collapsible section wrapper ─────────────────────────────────────────

function CollapsibleSection({
  expanded,
  items,
  isCollapsed,
  transition,
  onSelectSession,
}: {
  expanded: boolean
  items: Array<NavItemDef>
  isCollapsed: boolean
  transition: Record<string, unknown>
  onSelectSession?: () => void
}) {
  return (
    <AnimatePresence initial={false}>
      {expanded && (
        <motion.div
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: 'auto', opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={{ duration: 0.15 }}
          className="overflow-hidden space-y-0.5"
        >
          {items.map((item) => (
            <motion.div
              key={item.label}
              layout
              transition={{ layout: transition }}
              className="w-full"
            >
              <NavItem
                item={item}
                isCollapsed={isCollapsed}
                transition={transition}
                onSelectSession={onSelectSession}
              />
            </motion.div>
          ))}
        </motion.div>
      )}
    </AnimatePresence>
  )
}

// ── Persist helper ──────────────────────────────────────────────────────

function usePersistedBool(key: string, defaultValue: boolean) {
  const [value, setValue] = useState(() => {
    try {
      const stored = localStorage.getItem(key)
      if (stored === 'true') return true
      if (stored === 'false') return false
      return defaultValue
    } catch {
      return defaultValue
    }
  })

  function toggle() {
    setValue((prev) => {
      const next = !prev
      try {
        localStorage.setItem(key, String(next))
      } catch {
        // ignore
      }
      return next
    })
  }

  return [value, toggle] as const
}

// ── Main component ──────────────────────────────────────────────────────

function ChatSidebarComponent({
  sessions,
  activeFriendlyId,
  isCollapsed,
  onToggleCollapse,
  onSelectSession,
  onActiveSessionDelete,
  sessionsLoading,
  sessionsFetching,
  sessionsError,
  onRetrySessions,
}: ChatSidebarProps) {
  const { settingsOpen, settingsSection, setSettingsOpen, handleOpenSettings } =
    useSidebarSettings()
  const profileDisplayName = useChatSettingsStore(selectChatProfileDisplayName)
  const profileAvatarDataUrl = useChatSettingsStore(
    selectChatProfileAvatarDataUrl,
  )
  const footerSubtitleQuery = useQuery({
    queryKey: ['sidebar-footer-subtitle'],
    queryFn: fetchFooterSubtitle,
    refetchInterval: 60_000,
    retry: false,
    staleTime: 30_000,
  })
  const footerSubtitle = footerSubtitleQuery.data || 'Offline'
  const { deleteSession } = useDeleteSession()
  const { renameSession } = useRenameSession()
  const openSearchModal = useSearchModal((state) => state.openModal)
  const isSearchModalOpen = useSearchModal((state) => state.isOpen)
  const pathname = useRouterState({
    select: function selectPathname(state) {
      return state.location.pathname
    },
  })

  useEffect(() => {
    function handleOpenSettingsEvent(event: Event) {
      const detail = (event as CustomEvent<ChatOpenSettingsDetail>).detail
      handleOpenSettings(detail.section === 'appearance' ? 'appearance' : 'claude')
    }

    window.addEventListener(CHAT_OPEN_SETTINGS_EVENT, handleOpenSettingsEvent)
    return () => {
      window.removeEventListener(
        CHAT_OPEN_SETTINGS_EVENT,
        handleOpenSettingsEvent,
      )
    }
  }, [handleOpenSettings])

  // Platform-aware modifier key
  const _mod = useMemo(
    () =>
      typeof navigator !== 'undefined' &&
      /Mac|iPod|iPhone|iPad/.test(navigator.userAgent)
        ? '⌘'
        : 'Ctrl+',
    [],
  )

  // Route active states
  const isNewSessionActive =
    pathname === '/new' || pathname.startsWith('/chat/new')
  // Chat is active for / and /chat/<existing-id>, but NOT for the New Session
  // routes — those have their own row that should light up exclusively.
  const isChatActive =
    !isNewSessionActive &&
    (pathname === '/' || pathname.startsWith('/chat'))
  const _isSettingsActive = pathname === '/settings'
  const isSkillsActive = pathname === '/skills'
  const isMcpActive = pathname === '/mcp'
  const isFilesActive = pathname === '/files'
  const isAgoraActive = pathname === '/agora'
  const isTerminalActive = pathname === '/terminal'
  const isJobsActive = pathname === '/jobs'
  const isMemoryActive = pathname === '/memory'
  const isChatsActive = pathname === '/chats'
  const isTasksActive = pathname === '/tasks'
  const isConductorActive = pathname === '/conductor'
  const isOperationsActive = pathname === '/operations'
  const isSwarmActive = pathname === '/swarm' || pathname === '/swarm2'
  const isDesignActive = pathname === '/design'
  const mainRoutes = ['/chat', '/new', '/files', '/terminal']
  const knowledgeRoutes = ['/memory', '/skills']
  const systemRoutes = ['/settings', '/logs']

  useEffect(() => {
    if (mainRoutes.includes(pathname)) setLastRoute('main', pathname)
    if (knowledgeRoutes.includes(pathname)) setLastRoute('knowledge', pathname)
    if (systemRoutes.includes(pathname)) setLastRoute('system', pathname)
  }, [pathname])

  const mainNav = getLastRoute('main') || '/chat'
  const knowledgeNav = getLastRoute('knowledge') || '/memory'
  const _systemNav = getLastRoute('system') || '/settings'

  const transition = {
    duration: 0.15,
    ease: isCollapsed ? 'easeIn' : 'easeOut',
  } as const

  // Collapsible section states
  const [mainExpanded, toggleMain] = usePersistedBool(
    'claude-sidebar-main-expanded',
    true,
  )
  const [knowledgeExpanded, toggleKnowledge] = usePersistedBool(
    'claude-sidebar-knowledge-expanded',
    true,
  )
  const [_systemExpanded, _toggleSystem] = usePersistedBool(
    'claude-sidebar-system-expanded',
    false,
  )

  const [renameDialogOpen, setRenameDialogOpen] = useState(false)
  const [renameSessionKey, setRenameSessionKey] = useState<string | null>(null)
  const [renameFriendlyId, setRenameFriendlyId] = useState<string | null>(null)
  const [renameSessionTitle, setRenameSessionTitle] = useState('')

  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [deleteSessionKey, setDeleteSessionKey] = useState<string | null>(null)
  const [deleteFriendlyId, setDeleteFriendlyId] = useState<string | null>(null)
  const [deleteSessionTitle, setDeleteSessionTitle] = useState('')
  const [providersOpen, setProvidersOpen] = useState(false)
  const [isMobile, setIsMobile] = useState(false)
  const [isHoverExpanded, setIsHoverExpanded] = useState(false)
  const sidebarHoverExpand = useChatSettingsStore(selectSidebarHoverExpand)
  const sidebarRef = useRef<HTMLElement | null>(null)
  const swipeStartRef = useRef<{ x: number; y: number } | null>(null)

  function handleOpenRename(session: SessionMeta) {
    setRenameSessionKey(session.key)
    setRenameFriendlyId(session.friendlyId)
    setRenameSessionTitle(
      session.label || session.title || session.derivedTitle || '',
    )
    setRenameDialogOpen(true)
  }

  function handleSaveRename(newTitle: string) {
    if (renameSessionKey) {
      void renameSession(renameSessionKey, renameFriendlyId, newTitle)
    }
    setRenameDialogOpen(false)
    setRenameSessionKey(null)
    setRenameFriendlyId(null)
  }

  function handleOpenDelete(session: SessionMeta) {
    setDeleteSessionKey(session.key)
    setDeleteFriendlyId(session.friendlyId)
    setDeleteSessionTitle(
      session.label ||
        session.title ||
        session.derivedTitle ||
        session.friendlyId,
    )
    setDeleteDialogOpen(true)
  }

  function handleConfirmDelete() {
    if (deleteSessionKey && deleteFriendlyId) {
      const isActive = deleteFriendlyId === activeFriendlyId
      if (isActive && onActiveSessionDelete) {
        onActiveSessionDelete()
      }
      void deleteSession(deleteSessionKey, deleteFriendlyId, isActive)
    }
    setDeleteDialogOpen(false)
    setDeleteSessionKey(null)
    setDeleteFriendlyId(null)
  }

  useEffect(() => {
    const media = window.matchMedia('(max-width: 767px)')
    const update = () => setIsMobile(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])

  useEffect(() => {
    if (isMobile || !isCollapsed || !sidebarHoverExpand) {
      setIsHoverExpanded(false)
    }
  }, [isCollapsed, isMobile, sidebarHoverExpand])

  const isHoverPreviewExpanded =
    sidebarHoverExpand && !isMobile && isCollapsed && isHoverExpanded
  const isVisuallyCollapsed = isCollapsed && !isHoverPreviewExpanded

  function handleSidebarToggle() {
    // In hover-preview mode, a click should dismiss the preview first;
    // otherwise toggle the persistent collapsed state.
    if (isHoverPreviewExpanded) {
      setIsHoverExpanded(false)
      return
    }
    onToggleCollapse()
  }

  const asideProps = {
    className: cn(
      'border-r h-full overflow-hidden flex flex-col theme-sidebar theme-border',
      isMobile && 'fixed inset-y-0 left-0 z-50 shadow-2xl',
      isMobile && isCollapsed && 'pointer-events-none',
    ),
  }

  useEffect(() => {
    if (!isMobile || isCollapsed) return
    const node = sidebarRef.current
    if (!node) return

    const SWIPE_CLOSE_PX = 64
    const MAX_VERTICAL_DRIFT_PX = 72

    function handleTouchStart(event: TouchEvent) {
      if (event.touches.length !== 1) return
      const touch = event.touches[0]
      swipeStartRef.current = { x: touch.clientX, y: touch.clientY }
    }

    function handleTouchEnd(event: TouchEvent) {
      const start = swipeStartRef.current
      swipeStartRef.current = null
      if (!start || event.changedTouches.length !== 1) return
      const touch = event.changedTouches[0]
      const dx = touch.clientX - start.x
      const dy = touch.clientY - start.y
      if (Math.abs(dy) > MAX_VERTICAL_DRIFT_PX) return
      if (dx <= -SWIPE_CLOSE_PX) {
        onToggleCollapse()
      }
    }

    node.addEventListener('touchstart', handleTouchStart, { passive: true })
    node.addEventListener('touchend', handleTouchEnd, { passive: true })
    return () => {
      node.removeEventListener('touchstart', handleTouchStart)
      node.removeEventListener('touchend', handleTouchEnd)
    }
  }, [isCollapsed, isMobile, onToggleCollapse])

  useEffect(() => {
    function handleOpenSettingsFromSearch() {
      handleOpenSettings()
    }

    window.addEventListener(
      SEARCH_MODAL_EVENTS.OPEN_SETTINGS,
      handleOpenSettingsFromSearch,
    )
    return () => {
      window.removeEventListener(
        SEARCH_MODAL_EVENTS.OPEN_SETTINGS,
        handleOpenSettingsFromSearch,
      )
    }
  }, [handleOpenSettings])

  // ── Nav definitions ─────────────────────────────────────────────────

  // Search button definition (placed above Studio section)
  const searchItem: NavItemDef = {
    kind: 'button',
    icon: Search,
    label: 'Search',
    active: isSearchModalOpen,
    onClick: openSearchModal,
  }

  const isDashboardActive = pathname === '/dashboard'

  const mainItems: Array<NavItemDef> = [
    {
      kind: 'link',
      to: '/dashboard',
      icon: LayoutDashboard,
      label: t('nav.dashboard'),
      active: isDashboardActive,
    },
    {
      kind: 'link',
      to: '/chats',
      icon: MessagesSquare,
      label: 'Chats',
      active: isChatsActive,
    },
    {
      kind: 'link',
      to: '/files',
      icon: FileText,
      label: t('nav.files'),
      active: isFilesActive,
    },
    {
      kind: 'link',
      to: '/terminal',
      icon: SquareTerminal,
      label: t('nav.terminal'),
      active: isTerminalActive,
    },
    {
      kind: 'link',
      to: '/jobs',
      icon: Clock,
      label: t('nav.jobs'),
      active: isJobsActive,
    },
    {
      kind: 'link',
      to: '/tasks',
      icon: KanbanSquare,
      label: 'Kanban',
      active: isTasksActive,
    },
    {
      kind: 'link',
      to: '/conductor',
      icon: Rocket,
      label: 'Conductor',
      active: isConductorActive,
    },
    {
      kind: 'link',
      to: '/operations',
      icon: Users,
      label: 'Operations',
      active: isOperationsActive,
    },
    {
      kind: 'link',
      to: '/swarm',
      icon: UsersRound,
      label: 'Swarm',
      active: isSwarmActive,
    },
    {
      kind: 'link',
      to: '/design',
      icon: Palette,
      label: 'Design',
      active: isDesignActive,
    },
  ]

  const knowledgeItems: Array<NavItemDef> = [
    {
      kind: 'link',
      to: '/memory',
      icon: Brain,
      label: t('nav.memory'),
      active: isMemoryActive,
    },
    {
      kind: 'link',
      to: '/skills',
      icon: Puzzle,
      label: t('nav.skills'),
      active: isSkillsActive,
      dataTour: 'skills',
    },
    {
      kind: 'link',
      to: '/mcp',
      icon: Server,
      label: 'MCP',
      active: isMcpActive,
    },
    {
      kind: 'link',
      to: '/profiles',
      icon: CircleUser,
      label: t('nav.profiles'),
      active: pathname === '/profiles',
    },
  ]

  const systemItems: Array<NavItemDef> = []

  return (
    <motion.aside
      ref={(node) => {
        sidebarRef.current = node
      }}
      initial={false}
      animate={{
        width: isVisuallyCollapsed
          ? isMobile
            ? 0
            : 48
          : isMobile
            ? '85vw'
            : 300,
      }}
      transition={{ type: 'spring', stiffness: 400, damping: 30 }}
      className={cn(
        asideProps.className,
        isMobile && isCollapsed && 'pointer-events-none overflow-hidden',
      )}
      data-tour="sidebar-container"
      style={isMobile ? { maxWidth: 360 } : undefined}
      onMouseEnter={() => {
        if (sidebarHoverExpand && !isMobile && isCollapsed) {
          setIsHoverExpanded(true)
        }
      }}
      onMouseLeave={() => {
        if (sidebarHoverExpand && !isMobile) setIsHoverExpanded(false)
      }}
      aria-hidden={isMobile && isCollapsed ? true : undefined}
      {...(isMobile && isCollapsed ? { inert: true } : {})}
    >
      {/* ── Header ──────────────────────────────────────────────────── */}
      <motion.div
        layout
        transition={{ layout: transition }}
        className="relative flex h-12 items-center px-2"
      >
        <AnimatePresence initial={false}>
          {!isVisuallyCollapsed ? (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={transition}
            >
              <Link
                to="/chat"
                className={cn(
                  buttonVariants({ variant: 'ghost', size: 'sm' }),
                  'sidebar-row w-full pl-1.5 justify-start gap-2.5',
                )}
              >
                <img
                  src="/claude-avatar.webp"
                  alt="OpenComputer"
                  className="size-6 rounded-full"
                />
                <span
                  className="sidebar-brand text-[20px] leading-none"
                  style={{ color: 'var(--sidebar-text)' }}
                >
                  OpenComputer
                </span>
              </Link>
            </motion.div>
          ) : null}
        </AnimatePresence>
        <TooltipProvider>
          <TooltipRoot>
            <TooltipTrigger
              onClick={handleSidebarToggle}
              render={
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label={
                    isVisuallyCollapsed ? 'Open Sidebar' : 'Close Sidebar'
                  }
                  className="absolute right-2 top-1/2 shrink-0 -translate-y-1/2 opacity-80 hover:opacity-100"
                  data-tour="sidebar-collapse-toggle"
                >
                  {isVisuallyCollapsed ? (
                    <PanelLeftOpen size={18} strokeWidth={1.5} />
                  ) : (
                    <PanelLeftClose size={18} strokeWidth={1.5} />
                  )}
                </Button>
              }
            />
            <TooltipContent side="right">
              {isVisuallyCollapsed ? 'Open Sidebar' : 'Close Sidebar'}
            </TooltipContent>
          </TooltipRoot>
        </TooltipProvider>
      </motion.div>

      {/* ── Search (ChatGPT-style, above sections) ─────────────────── */}
      <div className="px-2 pb-1">
        <motion.div
          layout
          transition={{ layout: transition }}
          className="w-full"
        >
          <NavItem
            item={searchItem}
            isCollapsed={isVisuallyCollapsed}
            transition={transition}
            onSelectSession={onSelectSession}
          />
        </motion.div>
      </div>

      {/* ── New Session button ──────────────────────────────────────── */}
      {!isVisuallyCollapsed && (
        <div className="px-2 pb-1">
          <Link
            to="/chat/$sessionKey"
            params={{ sessionKey: 'new' }}
            onClick={() => {
              onSelectSession?.()
            }}
            className={cn(
              buttonVariants({ variant: 'ghost', size: 'sm' }),
              'sidebar-row w-full justify-start gap-3 px-2.5 py-1.5 min-h-9 rounded-md',
              isNewSessionActive && 'is-active',
            )}
            data-tour="new-session"
          >
            <SquarePen
              size={17}
              strokeWidth={1.5}
              className="size-[17px] shrink-0"
            />
            <span>New Session</span>
          </Link>
        </div>
      )}

      {/* ── Scrollable body: nav + sessions ─────────────────────────── */}
      <div className="flex-1 min-h-0 overflow-y-auto scrollbar-thin flex flex-col">
        {/* Navigation sections */}
        <div className={cn('shrink-0 space-y-0.5 px-2', isMobile && 'order-2')}>
          <SectionLabel
            label="Main"
            isCollapsed={isVisuallyCollapsed}
            transition={transition}
            collapsible
            expanded={mainExpanded}
            onToggle={toggleMain}
            navigateTo={mainNav}
          />
          <CollapsibleSection
            expanded={mainExpanded || isCollapsed}
            items={mainItems}
            isCollapsed={isVisuallyCollapsed}
            transition={transition}
            onSelectSession={onSelectSession}
          />

          <SectionLabel
            label="Knowledge"
            isCollapsed={isVisuallyCollapsed}
            transition={transition}
            collapsible
            expanded={knowledgeExpanded}
            onToggle={toggleKnowledge}
            navigateTo={knowledgeNav}
          />
          <CollapsibleSection
            expanded={knowledgeExpanded || isCollapsed}
            items={knowledgeItems}
            isCollapsed={isVisuallyCollapsed}
            transition={transition}
            onSelectSession={onSelectSession}
          />

          {/* System */}
          <CollapsibleSection
            expanded={true}
            items={systemItems}
            isCollapsed={isVisuallyCollapsed}
            transition={transition}
            onSelectSession={onSelectSession}
          />
        </div>

        {/* Sessions list */}
        <div className={cn('shrink-0 mt-1', isMobile && 'order-1')}>
          <AnimatePresence initial={false}>
            {!isVisuallyCollapsed && (
              <motion.div
                key="content"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={transition}
                className="flex flex-col w-full min-h-0 h-full"
              >
                <div className="flex-1 min-h-0">
                  <SidebarSessions
                    sessions={sessions}
                    activeFriendlyId={activeFriendlyId}
                    onSelect={onSelectSession}
                    onRename={handleOpenRename}
                    onDelete={handleOpenDelete}
                    loading={sessionsLoading}
                    fetching={sessionsFetching}
                    error={sessionsError}
                    onRetry={onRetrySessions}
                  />
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
      {/* end scrollable body */}

      {/* ── Footer with User Menu ─────────────────────────────────── */}
      <div
        className="px-2 py-2 border-t shrink-0"
        style={{ borderColor: 'var(--sidebar-border)' }}
      >
        {/* User card + actions */}
        <div
          className={cn(
            'flex items-center rounded-md',
            isVisuallyCollapsed ? 'flex-col gap-2 py-2' : 'gap-2 px-1 py-1',
          )}
        >
          {/* User menu trigger */}
          <MenuRoot>
            <MenuTrigger
              data-tour="settings"
              className={cn(
                'sidebar-row flex items-center gap-2.5 rounded-md py-1 flex-1 min-w-0',
                isVisuallyCollapsed ? 'justify-center px-0' : 'px-1.5',
              )}
            >
              <UserAvatar
                size={28}
                src={profileAvatarDataUrl}
                alt={profileDisplayName}
              />
              <AnimatePresence initial={false} mode="wait">
                {!isVisuallyCollapsed && (
                  <motion.div
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    transition={transition}
                    className="flex-1 min-w-0 flex flex-col items-start leading-tight"
                  >
                    <span
                      className="flex items-center gap-1.5 text-[13px] font-medium truncate w-full"
                      style={{ color: 'var(--sidebar-text)' }}
                    >
                      <span className="truncate">{profileDisplayName}</span>
                      <StatusDot />
                    </span>
                    <span
                      className="text-[11px] truncate w-full"
                      style={{ color: 'var(--sidebar-muted)' }}
                      title={footerSubtitle}
                    >
                      {footerSubtitle}
                    </span>
                  </motion.div>
                )}
              </AnimatePresence>
            </MenuTrigger>
            <MenuContent side="top" align="start" className="min-w-[200px]">
              <MenuItem
                onClick={function onOpenSettings() {
                  handleOpenSettings('claude')
                }}
                className="justify-between"
              >
                <span className="flex items-center gap-2">
                  <Settings size={17} strokeWidth={1.5} />
                  Settings
                </span>
              </MenuItem>
            </MenuContent>
          </MenuRoot>

          {/* Settings + Theme toggle */}
          {!isVisuallyCollapsed && (
            <div className="flex items-center gap-0.5">
              <button
                type="button"
                onClick={() => handleOpenSettings('claude')}
                className="sidebar-row shrink-0 rounded-md p-1.5"
                style={{ color: 'var(--sidebar-muted)' }}
                aria-label="Settings"
              >
                <Settings size={16} strokeWidth={1.5} />
              </button>
              <ThemeToggleMini />
            </div>
          )}
        </div>
      </div>

      {/* ── Dialogs ─────────────────────────────────────────────────── */}
      <SettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        initialSection={settingsSection}
      />

      <ProvidersDialog open={providersOpen} onOpenChange={setProvidersOpen} />

      <SessionRenameDialog
        open={renameDialogOpen}
        onOpenChange={(open) => {
          setRenameDialogOpen(open)
          if (!open) {
            setRenameSessionKey(null)
            setRenameFriendlyId(null)
            setRenameSessionTitle('')
          }
        }}
        sessionTitle={renameSessionTitle}
        onSave={handleSaveRename}
        onCancel={() => {
          setRenameDialogOpen(false)
          setRenameSessionKey(null)
          setRenameFriendlyId(null)
          setRenameSessionTitle('')
        }}
      />

      <SessionDeleteDialog
        open={deleteDialogOpen}
        onOpenChange={setDeleteDialogOpen}
        sessionTitle={deleteSessionTitle}
        onConfirm={handleConfirmDelete}
        onCancel={() => setDeleteDialogOpen(false)}
      />
    </motion.aside>
  )
}

function areSessionsEqual(
  prevSessions: Array<SessionMeta>,
  nextSessions: Array<SessionMeta>,
): boolean {
  if (prevSessions === nextSessions) return true
  if (prevSessions.length !== nextSessions.length) return false
  for (let i = 0; i < prevSessions.length; i += 1) {
    const prev = prevSessions[i]
    const next = nextSessions[i]
    if (prev.key !== next.key) return false
    if (prev.friendlyId !== next.friendlyId) return false
    if (prev.label !== next.label) return false
    if (prev.title !== next.title) return false
    if (prev.derivedTitle !== next.derivedTitle) return false
    if (prev.updatedAt !== next.updatedAt) return false
    if (prev.titleStatus !== next.titleStatus) return false
    if (prev.titleSource !== next.titleSource) return false
    if (prev.titleError !== next.titleError) return false
  }
  return true
}

function areSidebarPropsEqual(
  prevProps: ChatSidebarProps,
  nextProps: ChatSidebarProps,
): boolean {
  if (prevProps.activeFriendlyId !== nextProps.activeFriendlyId) return false
  if (prevProps.creatingSession !== nextProps.creatingSession) return false
  if (prevProps.isCollapsed !== nextProps.isCollapsed) return false
  if (prevProps.sessionsLoading !== nextProps.sessionsLoading) return false
  if (prevProps.sessionsFetching !== nextProps.sessionsFetching) return false
  if (prevProps.sessionsError !== nextProps.sessionsError) return false
  if (prevProps.onRetrySessions !== nextProps.onRetrySessions) return false
  if (!areSessionsEqual(prevProps.sessions, nextProps.sessions)) return false
  return true
}

const MemoizedChatSidebar = memo(ChatSidebarComponent, areSidebarPropsEqual)

export { MemoizedChatSidebar as ChatSidebar }
