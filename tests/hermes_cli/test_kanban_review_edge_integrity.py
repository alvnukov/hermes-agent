"""Manual handoffs and corrupt persisted holds cannot lose authority or stall peers."""

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatcher
from hermes_cli import kanban_db_holds as holds
from tests.hermes_cli import test_kanban_dispatch_hold_lifecycle as hold_tests

board = hold_tests.board


@pytest.mark.parametrize("summary", [None, "Candidate"])
@pytest.mark.parametrize("latest", ["reviewer", "replacement", "optional", "malformed", "missing_event"])
def test_manual_handoff_preserves_latest_event_reviewer(board, summary, latest):
    tid = kb.create_task(board, title="Manual review", assignee="author")
    assert kb.request_review(board, tid, reviewer="reviewer", summary=summary)
    assert kb.reopen_review_task(board, tid)
    if latest in ("replacement", "optional"):
        assert kb.request_review(board, tid, reviewer="replacement" if latest == "replacement" else None, summary="Next")
        if latest == "optional":
            # Explicit optional provenance is distinct from omission's latest-reviewer fallback.
            event = kb.list_events(board, tid)[-1]
            assert event.payload is not None
            payload = dict(event.payload)
            payload["reviewer"] = None
            board.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), event.id))
        assert kb.reopen_review_task(board, tid)
    if latest == "malformed":
        board.execute("UPDATE task_events SET payload = '{\"implementer\":\"author\",\"reviewer\":\"\"}' WHERE task_id = ? AND kind = 'review_requested'", (tid,))
    elif latest == "missing_event":
        if summary is None:
            assert kb.request_review(board, tid, summary="Declared handoff")
            assert kb.reopen_review_task(board, tid)
        board.execute("DELETE FROM task_events WHERE task_id = ? AND kind = 'review_requested'", (tid,))
    before = kb.list_events(board, tid)
    accepted = kb.request_review(board, tid, summary="Final candidate")
    assert accepted is (latest not in ("malformed", "missing_event"))
    current = kb.get_task(board, tid)
    assert current is not None
    if accepted:
        expected = "replacement" if latest == "replacement" else ("author" if latest == "optional" else "reviewer")
        assert current.assignee == expected and current.status == "review"
    else:
        assert current.status == "ready" and kb.list_events(board, tid) == before


@pytest.mark.parametrize("corrupt", ["json", "gate_id", "source_event_id", "block_event_id", "release_token", "resolution_event", "resolution_schema", "resolution_actor"])
def test_corrupt_hold_fails_closed_without_stalling_unrelated_dispatch(board, corrupt):
    tid = kb.create_task(board, title="Corrupt held card", assignee="author")
    payload = {"schema": 1, "hold_id": "hold", "kind": "policy_gate", "cause_key": "cause", "resume_status": "ready",
               "source_event_id": 1, "context": {"gate_id": "kanban.review_readiness", "gate_revision": "old"}}
    if corrupt in ("gate_id", "block_event_id"):
        payload["context"][corrupt] = []
    elif corrupt == "source_event_id":
        payload[corrupt] = []
    raw = "not-json" if corrupt in ("json", "release_token") else json.dumps(payload)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status = 'blocked', dispatch_hold = ? WHERE id = ?", (raw, tid))
        if corrupt == "release_token":
            assert not holds.release_dispatch_hold(board, tid, expected_hold_id=None, wake_kind="operator", actor="operator", evidence={"reason": "Ready"})
        if corrupt.startswith("resolution_"):
            kb._append_event(board, tid, "block_loop_detected", {"kind": "needs_input", "source_status": "ready"})
            loop_id = int(board.execute("SELECT last_insert_rowid()").fetchone()[0])
            kb._append_event(board, tid, "dispatch_held", {"schema": 9 if corrupt == "resolution_schema" else 1,
                "hold_id": "loop", "kind": "block_loop", "cause_key": "loop", "resume_status": "ready",
                "source_event_id": loop_id, "context": {}})
            kb._append_event(board, tid, "dispatch_hold_released", {
                "hold_id": [] if corrupt == "resolution_event" else "loop",
                "actor": [] if corrupt == "resolution_actor" else "operator", "wake_kind": "operator",
                "evidence": {"reason": "Claimed resolution"}})
            board.execute("UPDATE tasks SET dispatch_hold = NULL WHERE id = ?", (tid,))
    if corrupt.startswith("resolution_"):
        preview = holds.backfill_dispatch_holds(board)
        assert preview["ambiguous"] == [tid]
        assert holds.backfill_dispatch_holds(board, dry_run=False)["held"] == [tid]
    neighbor = kb.create_task(board, title="Healthy neighbor", assignee="author")
    assert holds.reconcile_policy_holds(board, gate_revisions={"kanban.review_readiness": "new"}) == 0
    attempts = []
    dispatcher.dispatch_once(board, spawn_fn=lambda task, *a, **k: attempts.append(task.id), reconcile_orphans=False)
    assert attempts == [neighbor]
    current = kb.get_task(board, tid)
    assert current is not None and current.dispatch_hold is not None
    assert kb.claim_task(board, tid) is None
