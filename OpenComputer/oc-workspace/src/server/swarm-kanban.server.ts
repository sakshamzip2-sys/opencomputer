import { json } from '@tanstack/react-start'
import { z } from 'zod'
import {
  createKanbanCard,
  getKanbanBackendMeta,
  listKanbanCards,
  updateKanbanCard,
} from './kanban-backend'

const CreateCardSchema = z.object({
  title: z.string().trim().min(1).max(200),
  spec: z.string().trim().max(5000).optional().default(''),
  acceptanceCriteria: z.string().trim().max(5000).optional().default(''),
  assignedWorker: z.string().trim().max(120).optional().nullable(),
  reviewer: z.string().trim().max(120).optional().nullable(),
  status: z
    .enum(['backlog', 'ready', 'running', 'review', 'blocked', 'done'])
    .optional()
    .default('backlog'),
  missionId: z.string().trim().max(200).optional().nullable(),
  reportPath: z.string().trim().max(500).optional().nullable(),
  createdBy: z.string().trim().max(120).optional().default('aurora'),
})

const UpdateCardSchema = CreateCardSchema.partial().extend({
  id: z.string().trim().min(1),
})

export async function handleGet() {
  return json({
    ok: true,
    cards: await listKanbanCards(),
    backend: getKanbanBackendMeta(),
  })
}

export async function handlePost({ request }: { request: Request }) {
  let body: unknown
  try {
    body = await request.json()
  } catch {
    return json({ ok: false, error: 'Invalid JSON' }, { status: 400 })
  }
  const parsed = CreateCardSchema.safeParse(body)
  if (!parsed.success) {
    return json(
      {
        ok: false,
        error: parsed.error.issues.map((issue) => issue.message).join('; '),
      },
      { status: 400 },
    )
  }
  const card = await createKanbanCard(parsed.data)
  return json({ ok: true, card, backend: getKanbanBackendMeta() })
}

export async function handlePatch({ request }: { request: Request }) {
  let body: unknown
  try {
    body = await request.json()
  } catch {
    return json({ ok: false, error: 'Invalid JSON' }, { status: 400 })
  }
  const parsed = UpdateCardSchema.safeParse(body)
  if (!parsed.success) {
    return json(
      {
        ok: false,
        error: parsed.error.issues.map((issue) => issue.message).join('; '),
      },
      { status: 400 },
    )
  }
  const { id, ...updates } = parsed.data
  const card = await updateKanbanCard(id, updates)
  if (!card)
    return json({ ok: false, error: 'Card not found' }, { status: 404 })
  return json({ ok: true, card, backend: getKanbanBackendMeta() })
}
