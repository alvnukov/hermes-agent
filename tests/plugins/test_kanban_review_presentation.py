"""Review presentation follows the current claim, without changing worker status."""

from contextlib import closing
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.plugins.test_kanban_dashboard_plugin import _load_plugin_router


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _review(client):
    task = client.post("/api/plugins/kanban/tasks", json={"title": "Review presentation", "assignee": "builder"}).json()["task"]
    with closing(kbc.connect()) as conn:
        assert kb.request_review(conn, task["id"], force=True)
        review = kb.claim_review_task(conn, task["id"])
        assert review is not None
    return review


def _card(client, task_id, column):
    board = client.get("/api/plugins/kanban/board").json()
    return next(task for lane in board["columns"] if lane["name"] == column for task in lane["tasks"] if task["id"] == task_id)


def test_review_presentation_tracks_current_run_not_review_history(client):
    review = _review(client)
    card = _card(client, review.id, "review")
    detail = client.get(f"/api/plugins/kanban/tasks/{review.id}").json()["task"]
    for task in (card, detail):
        assert task["status"] == "running"
        assert task["display_status"] == "review"
    # A worker subsequently implementing changes must not inherit the old review phase.
    with closing(kbc.connect()) as conn:
        assert kb.request_changes(conn, review.id, reason="Fix the result")[0]
        assert kb.claim_task(conn, review.id, claimer="builder:retry") is not None
    card = _card(client, review.id, "running")
    assert card["display_status"] == "running"
    assert client.get(f"/api/plugins/kanban/tasks/{review.id}").json()["task"]["display_status"] == "running"


def test_review_dashboard_actions_keep_restart_and_changes_distinct(client):
    review = _review(client)
    url = f"/api/plugins/kanban/tasks/{review.id}"
    restarted = client.patch(url, json={"status": "ready"})
    assert restarted.status_code == 200
    assert restarted.json()["task"]["status"] == "review"
    with closing(kbc.connect()) as conn:
        again = kb.claim_review_task(conn, review.id, claimer="reviewer:retry")
        assert again is not None
    # Stale drawer actions cannot return a replacement reviewer run to the author.
    refused = client.post(url + "/request-changes", json={"reason": "Fix result", "expected_run_id": review.current_run_id})
    assert refused.status_code == 409
    stale_approval = client.patch(url, json={"status": "done", "expected_run_id": review.current_run_id})
    assert stale_approval.status_code == 409
    stale_bulk = client.post("/api/plugins/kanban/tasks/bulk", json={
        "ids": [review.id], "status": "done", "expected_run_ids": {review.id: review.current_run_id}})
    assert stale_bulk.json()["results"][0]["ok"] is False
    changed = client.post(url + "/request-changes", json={"reason": "Fix result", "expected_run_id": again.current_run_id})
    assert changed.status_code == 200
    assert changed.json()["task"]["status"] == "ready"
    assert changed.json()["task"]["assignee"] == "builder"
    with closing(kbc.connect()) as conn:
        assert kb.request_review(conn, review.id, force=True)
        assert kb.claim_review_task(conn, review.id, claimer="reviewer:approve")
    approved = client.patch(url, json={"status": "done"})
    assert approved.status_code == 200
    assert approved.json()["task"]["status"] == "done"


@pytest.mark.parametrize("action", ["request_changes", "complete_task"])
def test_human_review_handoff_stops_worker_registered_while_action_is_in_flight(client, monkeypatch, action):
    from hermes_cli import kanban_db_dispatch as dispatch

    review = _review(client)
    run_id = review.current_run_id
    assert run_id is not None
    terminated = []
    original = getattr(kb, action)
    monkeypatch.setattr(dispatch, "_process_fingerprint", lambda pid: 123)
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda pid, lock, *, started_at: terminated.append((pid, lock, started_at)))

    def registering_worker(conn, task_id, **kwargs):
        assert dispatch.adopt_worker_pid(conn, task_id, run_id, 12345)
        return original(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, action, registering_worker)
    url = f"/api/plugins/kanban/tasks/{review.id}"
    if action == "request_changes":
        response = client.post(url + "/request-changes", json={"reason": "Fix result", "expected_run_id": review.current_run_id})
    else:
        response = client.patch(url, json={"status": "done", "expected_run_id": review.current_run_id})
    assert response.status_code == 200, response.text
    assert terminated == [(12345, review.claim_lock, 123)]
