import { isAuthenticated } from './auth-middleware'
import {
  getClaudeTask,
  moveClaudeTask,
  updateClaudeTask,
} from './claude-tasks-backend'
import type { TaskColumn, TaskPriority } from './claude-tasks-backend'

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function isTaskColumn(value: unknown): value is TaskColumn {
  return (
    value === 'backlog' ||
    value === 'todo' ||
    value === 'in_progress' ||
    value === 'review' ||
    value === 'blocked' ||
    value === 'done'
  )
}

function isTaskPriority(value: unknown): value is TaskPriority {
  return value === 'high' || value === 'medium' || value === 'low'
}

export async function handleGet({
  request,
  params,
}: {
  request: Request
  params: { taskId: string }
}) {
  if (!isAuthenticated(request)) {
    return jsonResponse({ error: 'Unauthorized' }, 401)
  }

  const task = await getClaudeTask(params.taskId)
  if (!task) return jsonResponse({ error: 'Task not found' }, 404)
  return jsonResponse({ task })
}

export async function handlePatch({
  request,
  params,
}: {
  request: Request
  params: { taskId: string }
}) {
  if (!isAuthenticated(request)) {
    return jsonResponse({ error: 'Unauthorized' }, 401)
  }

  try {
    const body = (await request.json()) as Record<string, unknown>
    const task = await updateClaudeTask(params.taskId, {
      title: typeof body.title === 'string' ? body.title : undefined,
      description:
        typeof body.description === 'string' ? body.description : undefined,
      column: isTaskColumn(body.column) ? body.column : undefined,
      priority: isTaskPriority(body.priority) ? body.priority : undefined,
      assignee:
        body.assignee === null || typeof body.assignee === 'string'
          ? body.assignee
          : undefined,
      tags: Array.isArray(body.tags)
        ? body.tags.filter((tag): tag is string => typeof tag === 'string')
        : undefined,
      due_date:
        body.due_date === null || typeof body.due_date === 'string'
          ? body.due_date
          : undefined,
    })

    if (!task) return jsonResponse({ error: 'Task not found' }, 404)
    return jsonResponse({ task })
  } catch {
    return jsonResponse({ error: 'Invalid request body' }, 400)
  }
}

export async function handleDelete({ request }: { request: Request }) {
  if (!isAuthenticated(request)) {
    return jsonResponse({ error: 'Unauthorized' }, 401)
  }

  return jsonResponse(
    { error: 'Delete is not supported by the shared Agent Kanban backend' },
    405,
  )
}

export async function handlePost({
  request,
  params,
}: {
  request: Request
  params: { taskId: string }
}) {
  if (!isAuthenticated(request)) {
    return jsonResponse({ error: 'Unauthorized' }, 401)
  }

  const url = new URL(request.url)
  const action = url.searchParams.get('action') || 'move'
  if (action !== 'move') {
    return jsonResponse({ error: `Unsupported action: ${action}` }, 400)
  }

  try {
    const body = (await request.json()) as Record<string, unknown>
    if (!isTaskColumn(body.column)) {
      return jsonResponse({ error: 'column is required' }, 400)
    }
    const task = await moveClaudeTask(params.taskId, body.column)
    if (!task) return jsonResponse({ error: 'Task not found' }, 404)
    return jsonResponse({ task })
  } catch {
    return jsonResponse({ error: 'Invalid request body' }, 400)
  }
}
