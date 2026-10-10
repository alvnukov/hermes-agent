"""Worker liveness must not extend a successor's reused dispatcher claim."""

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatcher


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    with kbc.connect_closing() as conn:
        yield conn


def _snapshot(conn, tid):
    return (
        tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()),
        [tuple(r) for r in conn.execute("SELECT * FROM task_runs WHERE task_id = ?", (tid,))],
        kb.list_events(conn, tid),
    )


@pytest.mark.parametrize("entry", ["tool", "automatic"])
@pytest.mark.parametrize("identity", ["stale", "missing", "malformed", "wrong-lock", "valid"])
def test_worker_liveness_requires_current_run_and_lock(board, monkeypatch, entry, identity):
    from tools import kanban_tools
    from tools.registry import registry

    tid = kb.create_task(board, title="Reused dispatcher claim", assignee="author")
    old = kb.claim_task(board, tid, claimer="dispatcher:123", ttl_seconds=60)
    assert old is not None
    assert kb.block_task(board, tid, kind="needs_input", reason="Wait", expected_run_id=old.current_run_id)
    assert kb.unblock_task(board, tid)
    current = kb.claim_task(board, tid, claimer="dispatcher:123", ttl_seconds=60)
    assert current is not None
    board.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (tid,))
    board.execute("UPDATE task_runs SET claim_expires = 1 WHERE id = ?", (current.current_run_id,))
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "author")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "other-lock" if identity == "wrong-lock" else "dispatcher:123")
    raw = {"stale": str(old.current_run_id), "missing": "", "malformed": "invalid"}.get(identity, str(current.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", raw)
    monkeypatch.setattr(kanban_tools, "_auto_heartbeat_last_attempt", 0.0)
    before = _snapshot(board, tid)
    if entry == "tool":
        result = registry.dispatch("kanban_heartbeat", {"task_id": tid})
        output = json.loads(result) if isinstance(result, str) else result
        accepted = "error" not in output
    else:
        accepted = kanban_tools.heartbeat_current_worker_from_env()
    assert accepted is (identity == "valid")
    if identity != "valid":
        assert _snapshot(board, tid) == before
    else:
        task = kb.get_task(board, tid)
        assert task is not None and task.claim_expires is not None and task.claim_expires > 1
        assert task.last_heartbeat_at is not None
        assert kb.list_events(board, tid)[-1].kind == "heartbeat"


def test_liveness_transaction_rolls_back_both_halves(board, monkeypatch):
    tid = kb.create_task(board, title="Atomic heartbeat")
    task = kb.claim_task(board, tid, claimer="owner", ttl_seconds=60)
    assert task is not None
    before = _snapshot(board, tid)

    def fail_event(*args, **kwargs):
        raise RuntimeError("event write failed")

    monkeypatch.setattr(kb, "_append_event", fail_event)
    assert task.current_run_id is not None
    with pytest.raises(RuntimeError, match="event write failed"):
        dispatcher.heartbeat_current_run(board, tid, expected_run_id=task.current_run_id, claimer="owner", ttl_seconds=900)
    assert _snapshot(board, tid) == before
