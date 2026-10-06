"""Budget isolation must not disable wall-clock caps or rewrite retry outcomes."""
from __future__ import annotations

import logging

import pytest


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("agent:\n  max_turns: null\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    yield conn
    conn.close()


def test_explicit_wall_clock_cap_fires_at_its_own_boundary(board, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as kbd
    clock = [1000.0]
    monkeypatch.setattr(kbd.time, "time", lambda: clock[0])
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    capped = kb.create_task(board, title="synthetic wall cap", assignee="b", max_runtime_seconds=10)
    uncapped = kb.create_task(board, title="synthetic no wall cap", assignee="b", max_runtime_seconds=None)
    for index, task_id in enumerate((capped, uncapped)):
        assert kb.claim_task(board, task_id) is not None
        kbd._set_worker_pid(board, task_id, 4242000 + index)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET started_at = 1000")
        board.execute("UPDATE task_runs SET started_at = 1000")
    signals = []
    clock[0] = 1009.0
    assert kbd.enforce_max_runtime(board, signal_fn=lambda *args: signals.append(args)) == []
    assert signals == []
    clock[0] = 1010.0
    assert kbd.enforce_max_runtime(board, signal_fn=lambda *args: signals.append(args)) == [capped]
    assert signals and signals[0][0] == 4242000
    assert kb.get_task(board, uncapped).status == "running"
    event = next(event for event in kb.list_events(board, capped) if event.kind == "timed_out")
    assert event.payload["limit_seconds"] == event.payload["elapsed_seconds"] == 10


def test_iteration_error_keeps_existing_timeout_then_gave_up_outcomes(board):
    from agent.turn_finalizer import _record_kanban_budget_exhausted
    from hermes_cli import kanban_db as kb
    task_id = kb.create_task(board, title="synthetic iteration cap", assignee="b", max_retries=2)
    for attempt in range(2):
        assert kb.claim_task(board, task_id) is not None
        _record_kanban_budget_exhausted(task_id, 7, 7, logging.getLogger(__name__))
        task = kb.get_task(board, task_id)
        assert task.consecutive_failures == attempt + 1
        assert task.last_failure_error.startswith("Iteration budget exhausted (7/7)")
    outcomes = [row[0] for row in board.execute(
        "SELECT outcome FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,))]
    assert outcomes == ["timed_out", "gave_up"]
    assert kb.get_task(board, task_id).status == "blocked"
    event = next(event for event in kb.list_events(board, task_id) if event.kind == "gave_up")
    assert event.payload["trigger_outcome"] == "timed_out"
    assert event.payload["budget_used"] == event.payload["budget_max"] == 7
