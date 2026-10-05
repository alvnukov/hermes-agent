"""Technical gate holds require native evidence and one causal version change."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_goal_gate as gate
from tests.hermes_cli import test_kanban_dispatch_hold_lifecycle as hold_tests

board = hold_tests.board


def _rejection(conn, tid, *, not_ready=False):
    task = kb.get_task(conn, tid)
    assert task is not None
    decision = gate.evaluate_kanban_handoff(
        conn, task, operation="request_review", evidence="Candidate", reviewer="reviewer",
        judge=lambda **kw: ("continue", "Author work incomplete", None, None, False),
        judge_available=True,
    )
    event_id = gate.record_gate_rejection(conn, tid, expected_run_id=task.current_run_id, decision=decision)
    assert event_id is not None and not decision.allowed
    assert decision.blockable is (not not_ready)
    return event_id


@pytest.mark.parametrize("evidence", ["valid", "missing", "forged", "wrong_task", "stale_run", "edited", "older_decision", "not_ready"])
@pytest.mark.parametrize("entry", ["tool", "cli"])
def test_policy_block_accepts_only_fresh_mechanical_native_evidence(board, monkeypatch, evidence, entry):
    from tools import kanban_tools  # registers the real handler
    from tools.registry import registry

    tid = kb.create_task(board, title="Gate input", body="x" * (10 if evidence == "not_ready" else 32001), assignee="author", goal_mode=True)
    task = kb.claim_task(board, tid)
    assert task is not None
    event_id = _rejection(board, tid, not_ready=evidence == "not_ready")
    def wrong_task():
        other = kb.create_task(board, title="Other", body="x" * 32001, assignee="author", goal_mode=True)
        assert kb.claim_task(board, other)
        return _rejection(board, other), task

    def stale_run():
        assert kb.block_task(board, tid, kind="needs_input", expected_run_id=task.current_run_id)
        assert kb.unblock_task(board, tid)
        next_task = kb.claim_task(board, tid)
        assert next_task is not None
        return event_id, next_task

    def edited():
        assert kb.edit_task(board, tid, body="Different goal")
        return event_id, task

    def older_decision():
        assert _rejection(board, tid) != event_id
        return event_id, task

    alterations = {
        "missing": lambda: (None, task),
        "forged": lambda: (event_id + 9999, task),
        "wrong_task": wrong_task,
        "stale_run": stale_run,
        "edited": edited,
        "older_decision": older_decision,
    }
    event_id, task = alterations.get(evidence, lambda: (event_id, task))()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    before = board.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    event_count = len(kb.list_events(board, tid))
    if entry == "tool":
        result = registry.dispatch("kanban_block", {"kind": "policy_gate", "reason": "Native gate input cannot be processed", "gate_rejection_event_id": event_id})
        result = json.loads(result) if isinstance(result, str) else result
    else:
        from hermes_cli import kanban
        rc = kanban._cmd_block(argparse.Namespace(task_id=tid, kind="policy_gate", reason=["Native rejection"], gate_rejection_event_id=event_id))
        result = {"ok": True} if rc == 0 else {"error": "rejected"}
    current = kb.get_task(board, tid)
    assert current is not None
    if evidence == "valid":
        assert result.get("ok") is True and current.status == "blocked"
        assert current.block_kind == "policy_gate" and current.dispatch_hold
        assert current.dispatch_hold["source_event_id"] == event_id
        assert current.dispatch_hold["context"]["gate_id"] == gate.REVIEW_GATE_ID
        assert current.dispatch_hold["context"]["gate_revision"] == gate.REVIEW_GATE_REVISION
        assert current.current_run_id is None and kb.claim_task(board, tid) is None
    else:
        assert "error" in result
        assert tuple(board.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()) == tuple(before)
        assert len(kb.list_events(board, tid)) == event_count


@pytest.mark.parametrize("phase", ["ready", "review"])
@pytest.mark.parametrize("change", ["same", "unrelated", "new_revision", "dispatch", "human_block", "edited", "parent"])
def test_policy_revision_wakes_once_and_preserves_cause_and_phase(board, monkeypatch, phase, change):
    from hermes_cli.kanban_db_holds import reconcile_policy_holds

    tid = kb.create_task(board, title="Revision candidate", body="x" * 32001, assignee="author", goal_mode=True)
    task = kb.claim_task(board, tid)
    assert task is not None
    if phase == "review":
        assert kb.request_review(board, tid, reviewer="reviewer", expected_run_id=task.current_run_id)
        task = kb.claim_review_task(board, tid)
        assert task is not None
    event_id = _rejection(board, tid)
    assert kb.block_task(board, tid, kind="policy_gate", expected_run_id=task.current_run_id, gate_rejection_event_id=event_id)
    held = kb.get_task(board, tid)
    assert held is not None and held.dispatch_hold
    parent = None
    if change == "human_block":
        with kb.write_txn(board):
            kb._append_event(board, tid, "blocked", {"kind": "needs_input", "reason": "Human decision now required"})
    elif change == "edited":
        assert kb.edit_task(board, tid, body="Changed causal task")
    elif change == "parent":
        parent = kb.create_task(board, title="Unfinished parent", assignee="author")
        kb.link_tasks(board, parent, tid)
    kb.add_comment(board, tid, author="author", body="Rephrasing alone is not a wake")
    assert reconcile_policy_holds(board, gate_revisions={gate.REVIEW_GATE_ID: "1"}) == 0
    revisions = {gate.REVIEW_GATE_ID: "1"} if change == "same" else ({"another.gate": "2"} if change == "unrelated" else {gate.REVIEW_GATE_ID: "2"})
    if change == "dispatch":
        from hermes_cli import kanban_db_dispatch as dispatcher
        monkeypatch.setattr(gate, "REVIEW_GATE_REVISION", "2")
        dispatcher.dispatch_once(board, max_spawn=0, reconcile_orphans=False)
        resumed = kb.get_task(board, tid)
        assert resumed is not None and resumed.dispatch_hold is None
    path = Path(board.execute("PRAGMA database_list").fetchone()[2])

    def attempt(_):
        with kbc.connect_closing(path) as conn:
            released = reconcile_policy_holds(conn, gate_revisions=revisions)
            claim = kb.claim_review_task if phase == "review" else kb.claim_task
            return released, claim(conn, tid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(attempt, range(2)))
    expected = change in ("new_revision", "parent")
    assert sum(n for n, _ in attempts) == int(expected)
    assert sum(task is not None for _, task in attempts) == int(change in ("new_revision", "dispatch"))
    current = kb.get_task(board, tid)
    assert current is not None and current.block_recurrences == held.block_recurrences
    if change == "parent":
        assert current.status == "todo" and current.dispatch_hold is None
        assert parent is not None
        assert kb.complete_task(board, parent, result="Parent done")
        kb.recompute_ready(board)
        current = kb.get_task(board, tid)
        assert current is not None and current.status == phase
    elif change in ("new_revision", "dispatch"):
        monkeypatch.setattr(gate, "REVIEW_GATE_REVISION", "2")
        new_event = _rejection(board, tid)
        assert kb.block_task(board, tid, kind="policy_gate", expected_run_id=current.current_run_id, gate_rejection_event_id=new_event)
        newly_blocked = kb.get_task(board, tid)
        assert newly_blocked is not None
        new_hold = newly_blocked.dispatch_hold
        assert new_hold and new_hold["hold_id"] != held.dispatch_hold["hold_id"]
        assert reconcile_policy_holds(board, gate_revisions=revisions) == 0
    else:
        assert current.dispatch_hold == held.dispatch_hold
