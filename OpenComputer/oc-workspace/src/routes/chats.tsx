import { createFileRoute } from '@tanstack/react-router'
import { ChatsListView } from '../screens/chat/components/chats-list-view'
import { usePageTitle } from '@/hooks/use-page-title'

/**
 * Claude.ai-style /chats route — a real page (not a modal) that lists every
 * chat session with title + relative time + bulk-select tooling, rendered
 * inside the WorkspaceShell so the OC sidebar stays visible.
 *
 * Mirrors claude.ai/recents.
 */

export const Route = createFileRoute('/chats')({
  ssr: false,
  component: ChatsRoute,
})

function ChatsRoute() {
  usePageTitle('Chats')
  return <ChatsListView />
}
