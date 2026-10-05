"""Explicit operator surfaces can release a hold; worker context cannot."""

import argparse
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban
from tests.plugins import test_kanban_dashboard_plugin as dashboard_tests

client = dashboard_tests.client
kanban_home = dashboard_tests.kanban_home


@pytest.mark.parametrize("entry", ["dashboard", "cli-promote", "cli-unblock"])
@pytest.mark.parametrize("worker", [False, True])
def test_operator_action_releases_only_operator_owned_hold(client, monkeypatch, entry, worker):
    from hermes_cli.kanban_db_holds import set_dispatch_hold
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Approved child", assignee="author", triage=True)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))
            hold = set_dispatch_hold(conn, tid, kind="manual_approval", cause_key="approval", resume_status="ready")
    if worker:
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    if entry == "dashboard":
        response = client.patch(f"/api/plugins/kanban/tasks/{tid}", json={"status": "ready"})
        accepted = response.status_code == 200
    elif entry == "cli-promote":
        accepted = kanban._cmd_promote(argparse.Namespace(task_id=tid, task_ids=None, reason=["Approved"], dry_run=False, json=False)) == 0
    else:
        accepted = kanban._cmd_unblock(argparse.Namespace(task_ids=[tid], reason="Approved")) == 0
    assert accepted is (not worker)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        if worker:
            assert task.dispatch_hold == hold
            assert kb.claim_task(conn, tid) is None
        else:
            assert task.dispatch_hold is None and task.status == "ready"
            released = [e for e in kb.list_events(conn, tid) if e.kind == "dispatch_hold_released"]
            assert len(released) == 1 and released[0].payload is not None and released[0].payload["evidence"]["reason"]
