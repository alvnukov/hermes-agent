"""Pausing and resuming work preserves reviewer authority and parent gating."""

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path):
    with kbc.connect_closing(tmp_path / "kanban.db") as db:
        yield db


@pytest.mark.parametrize("path", ["unblock", "promote", "schedule"])
@pytest.mark.parametrize("phase", ["ready", "review"])
@pytest.mark.parametrize("open_parent", [False, True])
def test_resume_preserves_phase_and_parent_gate(conn, path, phase, open_parent):
    tid = kb.create_task(conn, title="Resume work", assignee="author")
    active = kb.claim_task(conn, tid)
    assert active is not None
    if phase == "review":
        assert kb.request_review(conn, tid, reviewer="reviewer", summary="Candidate", expected_run_id=active.current_run_id)
        active = kb.claim_review_task(conn, tid)
        assert active is not None
    if path == "schedule":
        assert kb.schedule_task(conn, tid, reason="Wait", expected_run_id=active.current_run_id)
    else:
        assert kb.block_task(conn, tid, kind="needs_input", reason="Wait", expected_run_id=active.current_run_id)
    parent = None
    if open_parent:
        parent = kb.create_task(conn, title="Upstream")
        kb.link_tasks(conn, parent, tid)
    if path == "promote":
        ok, _ = kb.promote_task(conn, tid, actor="operator", reason="Ready")
        assert ok is (not open_parent)
        if open_parent:
            assert parent is not None and kb.complete_task(conn, parent, summary="Upstream complete")
            assert kb.promote_task(conn, tid, actor="operator", reason="Parent finished")[0]
    else:
        assert kb.unblock_task(conn, tid)
        if open_parent:
            task = kb.get_task(conn, tid)
            assert task is not None and task.status == "todo"
            assert kb.recompute_ready(conn) == 0
            assert parent is not None and kb.complete_task(conn, parent, summary="Upstream complete")
            kb.recompute_ready(conn)
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == phase
    resumed = (kb.claim_review_task if phase == "review" else kb.claim_task)(conn, tid)
    assert resumed is not None
    if phase == "review":
        assert kb.request_changes(conn, tid, reason="Rework", expected_run_id=resumed.current_run_id) == (True, "author")
    else:
        assert kb.complete_task(conn, tid, summary="Work complete", expected_run_id=resumed.current_run_id)


@pytest.mark.parametrize("initial_cycle", ["reopen", "changes"])
@pytest.mark.parametrize("latest", ["reviewer", "replacement", "malformed"])
def test_re_review_uses_latest_durable_reviewer(conn, initial_cycle, latest):
    tid = kb.create_task(conn, title="Reviewer identity", assignee="author")
    author = kb.claim_task(conn, tid)
    assert author is not None
    assert kb.request_review(conn, tid, reviewer="reviewer", summary="v1", expected_run_id=author.current_run_id)
    if initial_cycle == "changes":
        reviewer = kb.claim_review_task(conn, tid)
        assert reviewer is not None
        assert kb.request_changes(conn, tid, reason="Rework", expected_run_id=reviewer.current_run_id)[0]
    else:
        assert kb.reopen_review_task(conn, tid)
    author = kb.claim_task(conn, tid)
    assert author is not None
    chosen = "replacement" if latest == "replacement" else "reviewer"
    assert kb.request_review(conn, tid, reviewer=chosen, summary="v2", expected_run_id=author.current_run_id)
    if latest == "malformed":
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'review_requested'",
            ('{"implementer":"author","reviewer":""}', tid),
        )
    assert kb.reopen_review_task(conn, tid)
    author = kb.claim_task(conn, tid)
    assert author is not None
    before = kb.list_events(conn, tid)
    ok = kb.request_review(conn, tid, summary="v3", expected_run_id=author.current_run_id)
    assert ok is (latest != "malformed")
    task = kb.get_task(conn, tid)
    assert task is not None
    if latest == "malformed":
        assert task.status == "running" and task.assignee == "author"
        assert kb.list_events(conn, tid) == before
    else:
        assert (task.status, task.assignee) == ("review", chosen)
