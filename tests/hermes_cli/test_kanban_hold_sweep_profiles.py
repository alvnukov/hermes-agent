"""Held rows cannot starve new work or route review to a removed profile."""

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_dispatch_hold_lifecycle as hold_tests

board = hold_tests.board


@pytest.mark.parametrize("sweep", ["decompose", "specify"])
def test_unheld_triage_is_selected_before_bounded_sweep(board, sweep):
    from hermes_cli import kanban_decompose, kanban_specify
    from hermes_cli.kanban_db_holds import set_dispatch_hold
    with kb.write_txn(board):
        for i in range(1000):
            tid = f"held_{i}"
            board.execute("INSERT INTO tasks (id, title, assignee, status, priority, created_at) VALUES (?, 'Held', 'author', 'triage', 10, 1)", (tid,))
            set_dispatch_hold(board, tid, kind="block_loop", cause_key="loop", resume_status="ready")
    fresh = kb.create_task(board, title="New idea", assignee="author", triage=True, tenant="team")
    method = (kanban_decompose if sweep == "decompose" else kanban_specify).list_triage_ids
    assert method() == [fresh]
    assert method(tenant="team") == [fresh]
    assert method(tenant="other") == []


@pytest.mark.parametrize("selection", ["historical", "replacement", "optional"])
def test_resolved_reviewer_is_validated_before_judge_or_transition(board, monkeypatch, selection):
    from tools import kanban_tools
    from tools.registry import registry

    tid = kb.create_task(board, title="Profile validation", assignee="author")
    task = kb.claim_task(board, tid)
    assert task is not None
    assert kb.request_review(board, tid, summary="Candidate", reviewer="reviewer" if selection != "optional" else None, expected_run_id=task.current_run_id)
    assert kb.reopen_review_task(board, tid)
    task = kb.claim_task(board, tid)
    assert task is not None
    if selection != "optional":
        profile = Path(kb.kanban_db_path()).parent / "profiles" / "reviewer"
        (profile / "config.yaml").unlink()
        profile.rmdir()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    judged = []
    real_gate = kanban_tools._goal_gate

    def tracked(*args, **kwargs):
        judged.append(True)
        return real_gate(*args, **kwargs)

    monkeypatch.setattr(kanban_tools, "_goal_gate", tracked)
    before = kb.list_events(board, tid)
    args = {"summary": "Ready for another review"}
    if selection == "replacement":
        args["reviewer"] = "author"
    result = registry.dispatch("kanban_request_review", args)
    result = json.loads(result) if isinstance(result, str) else result
    current = kb.get_task(board, tid)
    assert current is not None
    if selection == "historical":
        assert "not installed" in result.get("error", "") and not judged
        assert current.status == "running" and current.current_run_id == task.current_run_id
        assert kb.list_events(board, tid) == before
    else:
        assert result.get("ok") is True and judged == [True]
        assert current.status == "review" and current.assignee == "author"
