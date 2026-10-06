import { host } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Test harness supplies plugin translations, as plugin loading does.
// eslint-disable-next-line no-restricted-imports
import { registerPluginLocales } from '@/i18n/plugin-i18n'

import type * as KanbanApi from './api'
import { fetchTask } from './api'
import { KanbanBoardPage } from './board'
import { TaskDrawer } from './drawer'
import { KANBAN_LOCALES } from './i18n'
import type { KanbanTaskDetail } from './types'

let scope = 'remote-owner'
let detail: KanbanTaskDetail
let client: QueryClient
let disposeLocales: () => void

vi.mock('./api', async importOriginal => ({
  ...(await importOriginal<typeof KanbanApi>()),
  useKanbanScope: () => scope,
  fetchBoard: vi.fn(async () => ({
    assignees: ['worker'],
    tenants: [],
    latest_event_id: 0,
    now: 0,
    columns: [{ name: 'blocked', tasks: [detail.task] }]
  })),
  fetchBoards: vi.fn(async () => ({ boards: [], current: '' })),
  fetchProfiles: vi.fn(async () => ({ profiles: [] })),
  fetchOrchestration: vi.fn(async () => ({ default_assignee: '' })),
  fetchLog: vi.fn(async () => ({ exists: false, content: '' })),
  fetchTask: vi.fn(async () => detail)
}))

beforeEach(() => {
  scope = 'remote-owner'
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  disposeLocales = registerPluginLocales('kanban', KANBAN_LOCALES)
  vi.spyOn(host, 'activeConnectionId').mockImplementation(() => scope)
  vi.spyOn(host, 'openSession').mockResolvedValue()
  vi.spyOn(host, 'notify').mockReturnValue('test-notice')
  detail = {
    task: { id: 'task', title: 'Blocked task', status: 'blocked', assignee: 'new-assignee' },
    comments: [],
    events: [],
    links: { parents: [], children: [] },
    attachments: [],
    runs: [
      {
        id: 1,
        status: 'done',
        profile: 'previous-worker',
        metadata: JSON.stringify({ worker_session_id: 'old-chat' })
      },
      { id: 2, status: 'blocked', profile: 'worker', metadata: { worker_session_id: 'current-chat' } }
    ]
  }
})

afterEach(() => {
  cleanup()
  client.clear()
  disposeLocales()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

function showBoard() {
  render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  )
}

function showDrawer(onClose = vi.fn()) {
  render(
    <QueryClientProvider client={client}>
      <TaskDrawer columns={['blocked', 'done']} id="task" onClose={onClose} onOpen={vi.fn()} />
    </QueryClientProvider>
  )

  return onClose
}

describe('Kanban run chat navigation', () => {
  it('opens the latest attempt from the board card without opening its drawer', async () => {
    showBoard()
    await screen.findByText('Blocked task')
    expect(fetchTask).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Open chat' }))
    await waitFor(() =>
      expect(host.openSession).toHaveBeenCalledWith(
        'current-chat',
        expect.objectContaining({
          profile: 'worker',
          route: { connectionId: 'remote-owner', mode: 'remote', profile: 'worker', targetProfile: 'worker' }
        })
      )
    )
    expect(fetchTask).toHaveBeenCalledWith('task')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('opens the latest attempt directly from the drawer header and closes the drawer', async () => {
    const onClose = showDrawer()
    fireEvent.click(await screen.findByRole('button', { name: 'Open chat' }))
    await waitFor(() =>
      expect(host.openSession).toHaveBeenCalledWith('current-chat', expect.objectContaining({ profile: 'worker' }))
    )
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('opens the selected historical run with its own profile instead of the task assignee', async () => {
    showDrawer()
    fireEvent.click(await screen.findByRole('button', { name: 'Runs · 2' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Open chat' })[1])
    await waitFor(() =>
      expect(host.openSession).toHaveBeenCalledWith('old-chat', expect.objectContaining({ profile: 'previous-worker' }))
    )
  })

  it('does not fall back to an old chat when the latest run has no binding', async () => {
    detail.runs[1].metadata = null
    showBoard()
    fireEvent.click(await screen.findByRole('button', { name: 'Open chat' }))
    await waitFor(() =>
      expect(host.notify).toHaveBeenCalledWith(expect.objectContaining({ message: 'This run has no linked chat yet.' }))
    )
    expect(host.openSession).not.toHaveBeenCalled()
  })

  it('ignores a task lookup completed after the source connection changed', async () => {
    let resolve!: (value: Awaited<ReturnType<typeof fetchTask>>) => void
    vi.mocked(fetchTask).mockReturnValueOnce(
      new Promise(done => {
        resolve = done
      })
    )
    showBoard()
    fireEvent.click(await screen.findByRole('button', { name: 'Open chat' }))
    await waitFor(() => expect(fetchTask).toHaveBeenCalled())
    scope = 'another-host'
    resolve({ ...detail, downloadAttachment: vi.fn() })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open chat' }).hasAttribute('disabled')).toBe(false))
    expect(host.openSession).not.toHaveBeenCalled()
  })
})
