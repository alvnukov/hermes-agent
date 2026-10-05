"""Successful review handoffs supersede earlier worker failures (#119070)."""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb.time, "time", lambda: 2_000_000_000)
    with kbc.connect_closing() as conn:
        yield conn


def _rate_limited_attempt(board, tid, pid):
    claimed = kb.claim_task(board, tid)
    assert claimed is not None
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, tid))
    kbd._record_worker_exit(pid, kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8)
    assert kbd.detect_crashed_workers(board) == []
    task = kb.get_task(board, tid)
    assert task is not None and task.last_failure_error is not None
    assert task.status == "ready" and task.current_run_id is None
    assert "rate-limited" in task.last_failure_error
    return task.last_failure_error


@pytest.mark.parametrize("attempts", [1, 3])
def test_successful_review_handoff_clears_error_and_spawns_reviewer(
    board, all_assignees_spawnable, attempts,
):
    tid = kb.create_task(board, title="Recovered implementation", assignee="author")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET consecutive_failures = 1 WHERE id = ?", (tid,))
    for i in range(attempts):
        _rate_limited_attempt(board, tid, 70000 + i)
    task = kb.get_task(board, tid)
    assert task is not None and task.consecutive_failures == 1
    claimed = kb.claim_task(board, tid)
    assert claimed is not None
    assert kb.request_review(board, tid, summary="Ready for review", reviewer="reviewer",
                             expected_run_id=claimed.current_run_id)
    task = kb.get_task(board, tid)
    assert task is not None
    assert task.status == "review" and task.current_run_id is None
    assert task.last_failure_error is None
    assert task.consecutive_failures == 1
    outcomes = [row[0] for row in board.execute(
        "SELECT outcome FROM task_runs WHERE task_id = ? ORDER BY id", (tid,),
    )]
    assert outcomes == ["rate_limited"] * attempts + ["review_requested"]

    spawned = []
    result = kbd.dispatch_once(board, spawn_fn=lambda task, workspace: spawned.append(task),
                               reconcile_orphans=False)
    assert result.respawn_guarded == []
    assert [(task.id, task.assignee) for task in spawned] == [(tid, "reviewer")]
    assert "sdlc-review" in spawned[0].skills


@pytest.mark.parametrize("handoff_delay", [0, 1])
def test_legacy_review_error_is_superseded_but_new_reviewer_failure_is_guarded(
    board, monkeypatch, all_assignees_spawnable, handoff_delay,
):
    tid = kb.create_task(board, title="Legacy review handoff", assignee="author")
    stale_error = _rate_limited_attempt(board, tid, 71000)
    now = 2_000_000_000 + handoff_delay
    monkeypatch.setattr(kb.time, "time", lambda: now)
    claimed = kb.claim_task(board, tid)
    assert claimed is not None
    assert kb.request_review(board, tid, summary="Ready for review", reviewer="reviewer",
                             expected_run_id=claimed.current_run_id)
    # Persisted rows from before the fix still carry the implementer's error.
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET last_failure_error = ? WHERE id = ?", (stale_error, tid))
    assert kbd.check_respawn_guard(board, tid, lane="review") is None

    spawned = []
    result = kbd.dispatch_once(board, spawn_fn=lambda task, workspace: spawned.append(task),
                               reconcile_orphans=False)
    assert result.respawn_guarded == []
    assert [(task.id, task.assignee) for task in spawned] == [(tid, "reviewer")]
    # A new auth failure in the review lane must still block another spawn,
    # including when all runs end within the same second.
    assert not kbd._record_task_failure(
        board, tid, "provider authentication failed", outcome="spawn_failed",
        release_claim=True, end_run=True, failure_limit=5,
    )
    task = kb.get_task(board, tid)
    assert task is not None and task.status == "review"
    assert kbd.check_respawn_guard(board, tid, lane="review") == "blocker_auth"
