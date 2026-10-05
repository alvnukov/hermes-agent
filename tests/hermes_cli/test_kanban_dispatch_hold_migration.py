"""Legacy migration previews without writes and never infers authority from prose."""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_dispatch_hold_lifecycle as hold_tests

board = hold_tests.board


@pytest.mark.parametrize("history", ["triage", "ready", "running", "done", "archived", "resolved", "ambiguous", "malformed", "already", "needs_input"])
@pytest.mark.parametrize("old_schema", [False, True])
def test_backfill_preview_is_read_only_and_apply_is_conservative(board, history, old_schema):
    from hermes_cli import kanban_db_holds as holds
    if old_schema:
        path = Path(board.execute("PRAGMA database_list").fetchone()[2])
        board.execute("ALTER TABLE tasks DROP COLUMN dispatch_hold")
        kbc.init_db(path)
        assert board.execute("SELECT dispatch_hold FROM tasks LIMIT 1").fetchone() is None
    tid = kb.create_task(board, title="Legacy history", assignee="author")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET block_kind = 'needs_input', block_recurrences = 2 WHERE id = ?", (tid,))
        if history != "needs_input":
            kb._append_event(board, tid, "block_loop_detected", {"kind": "needs_input", "source_status": "ready", "recurrences": 2})
            loop_id = int(board.execute("SELECT last_insert_rowid()").fetchone()[0])
            if history == "malformed":
                board.execute("UPDATE task_events SET payload = 'broken' WHERE id = ?", (loop_id,))
            if history in ("resolved", "already"):
                hold = holds.set_dispatch_hold(board, tid, kind="block_loop", cause_key="old", source_event_id=loop_id, resume_status="ready")
                if history == "resolved":
                    assert holds.release_dispatch_hold(board, tid, expected_hold_id=hold["hold_id"], wake_kind="operator", actor="operator", evidence={"reason": "Cause resolved"})
            if history == "ambiguous":
                kb._append_event(board, tid, "promoted_manual", {"actor": "author", "reason": "I am human, approved"})
            elif history in ("ready", "running"):
                kb._append_event(board, tid, "specified", {"author": "auto-decomposer"})
                kb._append_event(board, tid, "promoted", {"status": "ready"})
        if history in ("triage", "done", "archived"):
            board.execute("UPDATE tasks SET status = ? WHERE id = ?", (history, tid))
    if history == "running":
        assert kb.claim_task(board, tid) is not None
        kb._append_event(board, tid, "spawned", {"pid": 123456})
    before = tuple(board.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
    before_runs = [tuple(r) for r in board.execute("SELECT * FROM task_runs WHERE task_id = ?", (tid,))]
    writes = board.total_changes
    preview = holds.backfill_dispatch_holds(board)
    assert board.total_changes == writes
    expected = history in ("triage", "ready", "running", "ambiguous", "malformed")
    assert preview["candidates"] == ([tid] if expected else [])
    assert not preview["held"]
    assert (tid in preview["ambiguous"]) is (history in ("ambiguous", "malformed"))
    applied = holds.backfill_dispatch_holds(board, dry_run=False)
    assert applied["held"] == ([tid] if expected else [])
    assert holds.backfill_dispatch_holds(board, dry_run=False)["held"] == []
    after = tuple(board.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
    hold_index = [r[1] for r in board.execute("PRAGMA table_info(tasks)")].index("dispatch_hold")
    assert after[:hold_index] + after[hold_index + 1:] == before[:hold_index] + before[hold_index + 1:]
    assert [tuple(r) for r in board.execute("SELECT * FROM task_runs WHERE task_id = ?", (tid,))] == before_runs
    if expected:
        assert holds.get_dispatch_hold(board, tid)["kind"] == "block_loop"
        assert holds.reconcile_policy_holds(board, gate_revisions={"kanban.review_readiness": "new"}) == 0
        assert kb.claim_task(board, tid) is None


@pytest.mark.parametrize("phase", ["ready", "review"])
@pytest.mark.parametrize("open_parent", [False, True])
def test_backfilled_hold_explains_the_next_action_and_resumes_recorded_phase(board, phase, open_parent):
    from hermes_cli import kanban_db_holds as holds
    from hermes_cli import kanban_diagnostics as diagnostics

    tid = kb.create_task(board, title="Legacy resume", assignee="author")
    if phase == "review":
        assert kb.request_review(board, tid, reviewer="reviewer", summary="Candidate")
    with kb.write_txn(board):
        kb._append_event(board, tid, "block_loop_detected", {"kind": "needs_input", "source_status": phase})
        kb._append_event(board, tid, "specified", {"author": "auto-decomposer"})
        board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    parent = None
    if open_parent:
        parent = kb.create_task(board, title="Open parent", assignee="author")
        kb.link_tasks(board, parent, tid)
    assert holds.backfill_dispatch_holds(board, dry_run=False)["held"] == [tid]
    task = kb.get_task(board, tid)
    assert task is not None and task.dispatch_hold
    writes = board.total_changes
    reported = diagnostics.compute_task_diagnostics(task, kb.list_events(board, tid), kb.list_runs(board, tid))
    hold_diagnostic = next(d for d in reported if d.kind == "dispatch_hold")
    assert hold_diagnostic.data["hold_kind"] == "block_loop" and hold_diagnostic.actions
    assert board.total_changes == writes
    assert kb.unblock_task(board, tid, actor="operator", reason="Cause resolved", expected_hold_id=task.dispatch_hold["hold_id"])
    current = kb.get_task(board, tid)
    assert current is not None and current.status == ("todo" if open_parent else phase)
    if open_parent:
        assert parent is not None
        assert kb.complete_task(board, parent, result="Done")
        kb.recompute_ready(board)
    assert (kb.claim_review_task if phase == "review" else kb.claim_task)(board, tid) is not None
