import { createFileRoute } from '@tanstack/react-router'
import { usePageTitle } from '@/hooks/use-page-title'
import { DesignScreen } from '@/screens/design/design-screen'

export const Route = createFileRoute('/design')({
  ssr: false,
  component: DesignRoute,
})

function DesignRoute() {
  usePageTitle('Design')
  return <DesignScreen />
}
