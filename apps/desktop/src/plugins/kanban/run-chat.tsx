import { Button, Codicon, host, Tip, useValue } from '@hermes/plugin-sdk'
import { useState } from 'react'

import { $boardSlug, fetchTask, routedToScope, taskKey, useKanbanScope } from './api'
import type { KanbanRun, KanbanTaskDetail } from './types'
import { errText, useKanban } from './ui'

function workerSessionId(run: KanbanRun | null): null | string {
  try {
    const metadata = typeof run?.metadata === 'string' ? JSON.parse(run.metadata) : run?.metadata
    const id = metadata?.worker_session_id

    return typeof id === 'string' && id.trim() ? id.trim() : null
  } catch {
    return null
  }
}

/** Board cards resolve only on click; loaded run rows open their exact attempt. */
export function RunChatButton({
  onOpened,
  run,
  taskId
}: {
  onOpened?: () => void
  run?: KanbanRun | null
  taskId: string
}) {
  const k = useKanban()
  const scope = useKanbanScope()
  const slug = useValue($boardSlug)
  const [pending, setPending] = useState(false)

  const unavailable =
    run === undefined
      ? null
      : !workerSessionId(run)
        ? k.chatUnavailable
        : !run?.profile?.trim()
          ? k.chatProfileUnknown
          : null

  async function open() {
    if (!routedToScope({ queryKey: taskKey(scope, slug, taskId) })) {
      return
    }
    setPending(true)

    try {
      // list_runs is ordered by started_at ASC, id ASC. Never use the last
      // *bound* run: a new unbound attempt must not navigate to an old worker.
      const target = run === undefined ? ((await fetchTask(taskId)).runs.at(-1) ?? null) : run

      if ((host.activeConnectionId() ?? 'local') !== scope || $boardSlug.get() !== slug) {
        return
      }
      const sessionId = workerSessionId(target)
      const profile = target?.profile?.trim()

      if (!sessionId || !profile) {
        host.notify({ kind: 'info', message: sessionId ? k.chatProfileUnknown : k.chatUnavailable })

        return
      }

      await host.openSession(sessionId, {
        profile,
        route: { connectionId: scope, mode: scope === 'local' ? 'local' : 'remote', profile, targetProfile: profile },
        intent: 'tab',
        expectHistory: true,
        forceResume: true,
        awaitHydration: true
      })
      onOpened?.()
    } catch (error) {
      if ((host.activeConnectionId() ?? 'local') === scope) {
        host.notify({ kind: 'error', message: errText(error) || k.chatOpenFailed })
      }
    } finally {
      setPending(false)
    }
  }

  return (
    <Tip label={unavailable ?? k.openChat}>
      <span
        className="inline-flex"
        onClick={event => event.stopPropagation()}
        onPointerDown={event => event.stopPropagation()}
      >
        <Button
          aria-label={k.openChat}
          disabled={pending || !!unavailable}
          onClick={event => {
            event.stopPropagation()
            void open()
          }}
          onKeyDown={event => event.stopPropagation()}
          onPointerDown={event => event.stopPropagation()}
          size="xs"
          variant="ghost"
        >
          <Codicon name={pending ? 'loading' : 'comment-discussion'} />
          {k.openChat}
        </Button>
      </span>
    </Tip>
  )
}

export function LatestRunChatButton({ detail, onOpened }: { detail?: KanbanTaskDetail; onOpened: () => void }) {
  return detail ? <RunChatButton onOpened={onOpened} run={detail.runs.at(-1) ?? null} taskId={detail.task.id} /> : null
}
