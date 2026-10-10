import { expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'

import * as submit from './submit'

it('settles a card-chat send after an external worker accepts the note without stream events', () => {
  let state = { ...createClientSessionState('worker-chat'), busy: true, awaitingResponse: true }
  const busyRef = { current: true }
  const scope = { setBusy: vi.fn(), setAwaitingResponse: vi.fn() }

  const updateSessionState = vi.fn((_id: string, update: (current: typeof state) => typeof state) => {
    state = update(state)

    return state
  })

  submit.applySubmitAcknowledgement(
    { sessionId: 'card-view', result: { status: 'steered', worker_note_accepted: true } },
    'note',
    { busyRef, scope, targetIsCurrentView: () => true, updateSessionState }
  )

  expect(state.busy).toBe(false)
  expect(state.awaitingResponse).toBe(false)
  expect(busyRef.current).toBe(false)
  expect(scope.setBusy).toHaveBeenCalledWith(false)
  expect(scope.setAwaitingResponse).toHaveBeenCalledWith(false)
})

it('keeps the normal streaming turn active after submit acceptance', () => {
  const updateSessionState = vi.fn()
  const scope = { setBusy: vi.fn(), setAwaitingResponse: vi.fn() }
  const busyRef = { current: true }

  submit.applySubmitAcknowledgement({ sessionId: 'card-view', result: { status: 'streaming' } }, 'note', {
    busyRef,
    scope,
    targetIsCurrentView: () => true,
    updateSessionState
  })

  expect(updateSessionState).not.toHaveBeenCalled()
  expect(scope.setBusy).not.toHaveBeenCalled()
  expect(busyRef.current).toBe(true)
})
