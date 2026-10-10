"""A card chat sends operator notes to its worker without becoming a second writer."""
import os
import threading
from pathlib import Path

import pytest

from cli import HermesCLI
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.active_sessions import active_session_registry_snapshot
from hermes_cli.kanban_db_run_lifecycle import adopt_worker_pid
from hermes_state import SessionDB
from tools import kanban_tools as kt
from tui_gateway import server


@pytest.fixture
def worker(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    monkeypatch.setenv("HERMES_PROFILE", "worker")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    conn = kbc.connect()
    task = kb.create_task(conn, title="task", assignee="worker")
    claimed = kb.claim_task(conn, task)
    assert claimed is not None and claimed.current_run_id is not None
    assert adopt_worker_pid(conn, task, claimed.current_run_id, os.getpid())
    monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    kt._comment_watermark.clear()
    assert kt.register_current_worker_from_env(session_id="worker-chat")
    cli = object.__new__(HermesCLI)
    cli.session_id, cli.config, cli._active_session_lease = "worker-chat", {}, None
    assert cli._claim_active_session(stderr=True)
    session = dict(session_key="worker-chat", profile_home=str(home), source="desktop",
                   history=[], history_lock=threading.Lock(), attached_images=[], running=False)
    monkeypatch.setitem(server._sessions, "card-view", session)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_legacy_group_fence_error", lambda *a: None)
    monkeypatch.setattr(server, "_typed_stop_phrase_response", lambda *a: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: pytest.fail("second writer started"))
    yield cli, conn, task, session
    cli._release_active_session()
    conn.close()
    kt._comment_watermark.clear()


def submit(**params):
    return server._methods["prompt.submit"]("send", {"session_id": "card-view", "text": "Use v2", **params})


@pytest.mark.parametrize("compressed", [False, True])
def test_card_chat_note_reaches_existing_worker_and_preserves_lease(worker, monkeypatch, compressed):
    cli, conn, task, session = worker
    steers = []
    agent = type("Agent", (), {"steer": lambda self, text: steers.append(text) or True})()
    monkeypatch.setenv("HERMES_PROFILE", "operator")
    if compressed:
        with SessionDB(db_path=Path(session["profile_home"]) / "state.db") as db:
            db.create_session("worker-chat", source="kanban")
            db.end_session("worker-chat", end_reason="compression")
            db.create_session("compressed-chat", source="kanban", parent_session_id="worker-chat")
            assert db.get_compression_tip("worker-chat") == "compressed-chat"
        session["session_key"] = "compressed-chat"
    owner = active_session_registry_snapshot()[0]

    reply = submit()

    assert "error" not in reply, reply
    assert reply["result"] == {"status": "steered", "worker_note_accepted": True}
    kt._comment_poll_last_attempt = 0
    assert kt.inject_new_comments_from_env(agent)
    assert len(steers) == 1 and "Use v2" in steers[0]
    assert active_session_registry_snapshot()[0]["lease_id"] == owner["lease_id"]
    assert session["history"] == [] and session["running"] is False


def ordinary_cli(cli, conn, task, session):
    cli._release_active_session()
    os.environ.pop("HERMES_KANBAN_TASK")
    assert cli._claim_active_session(stderr=True)


@pytest.mark.parametrize("change,params", [
    (ordinary_cli, {}),
    (lambda cli, conn, task, session: conn.execute("UPDATE task_runs SET ended_at=1 WHERE task_id=?", (task,)), {}),
    (lambda cli, conn, task, session: conn.execute("UPDATE task_runs SET worker_pid=123 WHERE task_id=?", (task,)), {}),
    (lambda *a: None, {"truncate_before_row_id": 1, "confirm_truncate": True}),
    (lambda cli, conn, task, session: session.update(attached_images=["image.png"]), {}),
])
def test_unsafe_card_chat_submits_keep_ownership_refusal(worker, change, params):
    cli, conn, task, session = worker
    change(cli, conn, task, session)
    reply = submit(**params)
    assert reply["error"]["data"]["reason"] == "SESSION_NOT_OWNED"
    assert kb.list_comments(conn, task) == []
