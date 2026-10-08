"""Full board management is an explicit, profile/session-scoped grant."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def admin_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    from hermes_cli.config import save_config
    save_config({"platform_toolsets": {"cli": ["kanban_admin"], "telegram": ["file"]}})
    return tmp_path


def _names(selection, disabled=None):
    from model_tools import get_tool_definitions
    return {r["function"]["name"] for r in get_tool_definitions(
        selection, disabled_toolsets=disabled, quiet_mode=True, skip_tool_search_assembly=True)}


def _call(name, args, selection=None, disabled=None):
    from model_tools import handle_function_call
    result = handle_function_call(name, args,
        enabled_toolsets=["kanban_admin"] if selection is None else selection,
        disabled_toolsets=disabled)
    return json.loads(result) if isinstance(result, str) else result


def _task(**fields):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        return kb.create_task(conn, title="manage me", assignee="default", **fields)


@pytest.mark.parametrize("surface", ["cli", "http", "rpc"])
def test_saved_admin_opt_in_and_disable_are_profile_scoped(admin_home, surface):
    from hermes_cli.config import load_config, save_config
    from hermes_cli.tools_config import _apply_toolset_change, _get_platform_tools
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    save_config({"platform_toolsets": {"cli": ["file"], "telegram": ["file"]}})
    client = None
    if surface == "http":
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from hermes_cli.web_routers.tools import router
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

    def selected(platform="cli"):
        return sorted(_get_platform_tools(load_config(), platform, include_default_mcp_servers=False))

    def toggle(enabled):
        action = "enable" if enabled else "disable"
        if client:
            response = client.put("/api/tools/toolsets/kanban_admin", json={"enabled": enabled})
            assert response.status_code == 200, response.text
        elif surface == "rpc":
            from tui_gateway.server import _methods
            response = _methods["tools.configure"]("admin-test", {"action": action, "names": ["kanban_admin"]})
            assert "error" not in response, response
        else:
            _apply_toolset_change(load_config(), "cli", ["kanban_admin"], action)

    try:
        assert "kanban_tasks" not in _names(selected())
        toggle(True)
        offered = _names(selected())
        assert {"kanban_tasks", "kanban_boards", "kanban_workers", "kanban_dispatch", "kanban_create"} <= offered
        assert "kanban_tasks" not in _names(selected("telegram"))
        token = set_hermes_home_override(admin_home / "profiles" / "observer")
        try:
            save_config({"platform_toolsets": {"cli": ["file"]}})
            assert "kanban_tasks" not in _names(selected())
            assert "error" in _call("kanban_boards", {"action": "create", "board": "denied"})
        finally:
            reset_hermes_home_override(token)
        assert _names(selected()) == offered
        toggle(False)
        assert "kanban_tasks" not in _names(selected())
    finally:
        if client:
            client.close()


def test_default_worker_child_and_session_denials_apply_at_execution(admin_home, monkeypatch):
    from agent.delegation_context import delegated_child_context
    from hermes_cli.config import save_config
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    tid = _task()
    for selection, disabled in ((["all"], None), (["kanban"], None), (["file"], None), (["kanban_admin"], ["kanban_admin"])):
        assert "kanban_tasks" not in _names(selection, disabled)
        assert "error" in _call("kanban_tasks", {"action": "delete", "task_id": tid}, selection, disabled)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    assert "kanban_complete" in _names(["kanban_admin"])
    assert "kanban_tasks" not in _names(["kanban_admin"])
    assert "error" in _call("kanban_tasks", {"action": "delete", "task_id": tid})
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    with delegated_child_context():
        assert "kanban_tasks" not in _names(["kanban_admin"])
        assert "error" in _call("kanban_tasks", {"action": "delete", "task_id": tid})
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(admin_home))
    assert "kanban_orchestration" not in _names(["kanban_admin"])
    assert "error" in _call("kanban_orchestration", {"action": "update", "settings": {"max_spawn": 9}})
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT")
    save_config({"platform_toolsets": {"cli": ["file"]}})
    assert "error" in _call("kanban_tasks", {"action": "delete", "task_id": tid})
    with connect_closing() as conn:
        assert kb.get_task(conn, tid) is not None


def test_task_management_edits_routes_and_removes_real_cards(admin_home):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    tid = _task()
    result = _call("kanban_tasks", {"action": "edit", "task_id": tid, "title": "edited", "body": "new body", "priority": 7})
    assert result.get("ok"), result
    assert "error" in _call("kanban_tasks", {"action": "edit", "task_id": tid, "title": " ", "priority": 99})
    assert "error" in _call("kanban_tasks", {"action": "edit", "task_id": tid, "priority": "wrong"})
    assert "error" in _call("kanban_tasks", {"action": "move", "task_id": tid, "status": "running"})
    with connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert (task.title, task.body, task.priority) == ("edited", "new body", 7)
        assert not kb.list_runs(conn, tid)
    assert _call("kanban_tasks", {"action": "move", "task_id": tid, "status": "triage"}).get("ok")
    assert _call("kanban_tasks", {"action": "move", "task_id": tid, "status": "todo"}).get("ok")
    assert _call("kanban_tasks", {"action": "promote", "task_id": tid}).get("ok")
    assert _call("kanban_tasks", {"action": "archive", "task_id": tid}).get("ok")
    assert _call("kanban_tasks", {"action": "delete", "task_id": tid}).get("ok")
    with connect_closing() as conn:
        assert kb.get_task(conn, tid) is None


def test_queue_moves_preserve_dependencies_and_reopen_invalidation(admin_home):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    parent, child = _task(), _task()
    with connect_closing() as conn:
        kb.link_tasks(conn, parent_id=parent, child_id=child)
    assert "error" in _call("kanban_tasks", {"action": "move", "task_id": child, "status": "ready"})
    assert _call("kanban_complete", {"task_id": parent, "summary": "parent finished"}).get("ok")
    assert _call("kanban_complete", {"task_id": child, "summary": "child finished"}).get("ok")
    assert _call("kanban_tasks", {"action": "move", "task_id": parent, "status": "todo"}).get("ok")
    with connect_closing() as conn:
        assert kb.get_task(conn, child).status == "todo"
    assert _call("kanban_tasks", {"action": "unlink", "task_id": child, "parent_id": parent}).get("ok")


def test_boards_export_import_and_archive_roundtrip(admin_home, tmp_path):
    created = _call("kanban_boards", {"action": "create", "board": "tools", "name": "Tool board"})
    assert created.get("ok"), created
    tid = _call("kanban_create", {"board": "tools", "title": "roundtrip", "assignee": "default"})["task_id"]
    assert _call("kanban_boards", {"action": "update", "board": "tools", "description": "updated"}).get("ok")
    exported = _call("kanban_boards", {"action": "export", "board": "tools", "path": str(tmp_path / "board.tgz")})
    assert exported.get("ok"), exported
    imported = _call("kanban_boards", {"action": "import", "board": "copy", "path": exported["path"]})
    assert imported.get("ok"), imported
    task = _call("kanban_show", {"board": "copy", "task_id": tid})
    assert task.get("task", {}).get("title") == "roundtrip", task
    assert _call("kanban_boards", {"action": "switch", "board": "copy"}).get("ok")
    assert _call("kanban_boards", {"action": "archive", "board": "tools"}).get("ok")
    assert _call("kanban_boards", {"action": "delete", "board": "copy"}).get("ok")


def test_board_project_resolution_and_active_worker_removal_guard(admin_home, tmp_path):
    from hermes_cli import projects_db as projects, kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    repo = tmp_path / "project"
    repo.mkdir()
    with projects.connect_closing() as conn:
        project_id = projects.create_project(conn, slug="app", name="App", primary_path=str(repo))
    result = _call("kanban_boards", {"action": "create", "board": "app-board", "project": "app"})
    assert result.get("ok"), result
    assert result["board"]["project_id"] == project_id
    assert result["board"]["default_workdir"] == str(repo)
    with connect_closing(board="app-board") as conn:
        tid = kb.create_task(conn, title="active", assignee="default", board="app-board")
        assert kb.claim_task(conn, tid)
    assert "error" in _call("kanban_boards", {"action": "delete", "board": "app-board"})
    assert kb.board_exists("app-board")
    assert _call("kanban_workers", {"action": "reclaim", "task_id": tid, "board": "app-board"}).get("ok")
    assert _call("kanban_boards", {"action": "delete", "board": "app-board"}).get("ok")


def test_hold_release_needs_matching_id_and_reason(admin_home):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_holds import set_dispatch_hold
    from hermes_cli.kanban_db_connect import connect_closing
    tid = _task()
    with connect_closing() as conn:
        with kb.write_txn(conn):
            set_dispatch_hold(conn, tid, kind="manual_approval", cause_key="test", resume_status="ready", context={})
        hold = kb.get_task(conn, tid).dispatch_hold
    assert "error" in _call("kanban_tasks", {"action": "move", "task_id": tid, "status": "ready"})
    assert "error" in _call("kanban_tasks", {"action": "promote", "task_id": tid, "reason": "approved", "expected_hold_id": "stale"})
    result = _call("kanban_tasks", {"action": "move", "task_id": tid, "status": "ready", "reason": "approved", "expected_hold_id": hold["hold_id"]})
    assert result.get("ok"), result
    with connect_closing() as conn:
        assert kb.get_task(conn, tid).dispatch_hold is None


def test_structured_decomposition_routes_children_without_aux_model(admin_home):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    root = _call("kanban_create", {"title": "triage me", "triage": True, "assignee": "default"})["task_id"]
    result = _call("kanban_tasks", {"action": "decompose", "task_id": root,
        "children": [{"title": "first", "assignee": "default"}, {"title": "second", "assignee": "default", "parents": [0]}]})
    assert result.get("ok"), result
    with connect_closing() as conn:
        children = [kb.get_task(conn, tid) for tid in result["child_ids"]]
        assert [t.title for t in children] == ["first", "second"]
        assert children[0].status == "ready"
        assert children[1].status == "todo"
        assert kb.get_task(conn, root).status == "todo"


def test_workers_and_dispatch_dry_run_use_real_runs(admin_home):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    tid = _task()
    assert _call("kanban_dispatch", {"dry_run": True, "max_spawn": 1}).get("ok")
    with connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
    listed = _call("kanban_workers", {"action": "runs", "task_id": tid})
    assert listed.get("ok"), listed
    assert listed["runs"][0]["id"] == run_id
    assert _call("kanban_workers", {"action": "show", "run_id": run_id}).get("ok")
    assert _call("kanban_workers", {"action": "terminate", "run_id": run_id, "reason": "test reclaim"}).get("ok")
    assert "error" in _call("kanban_workers", {"action": "terminate", "run_id": run_id})
    with connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.get_run(conn, run_id).ended_at is not None


def test_terminate_stale_run_does_not_reclaim_successor(admin_home, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    tid = _task()
    with connect_closing() as conn:
        assert kb.claim_task(conn, tid)
        old_run = kb.get_task(conn, tid).current_run_id
    real_get = kb.get_task
    successor = []
    handed_off = False

    def get_snapshot_then_handoff(conn, task_id):
        nonlocal handed_off
        snapshot = real_get(conn, task_id)
        if task_id == tid and not handed_off:
            handed_off = True
            assert kb.reclaim_task(conn, tid, reason="worker finished while operator inspected it")
            assert kb.claim_task(conn, tid)
            successor.append(real_get(conn, tid).current_run_id)
        return snapshot

    monkeypatch.setattr(kb, "get_task", get_snapshot_then_handoff)
    result = _call("kanban_workers", {"action": "terminate", "run_id": old_run})
    assert "error" in result, result
    with connect_closing() as conn:
        assert real_get(conn, tid).status == "running"
        assert real_get(conn, tid).current_run_id == successor[0]


def test_board_removal_serializes_manual_claims(admin_home, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    assert _call("kanban_boards", {"action": "create", "board": "race"}).get("ok")
    tid = _call("kanban_create", {"board": "race", "title": "ready", "assignee": "default"})["task_id"]
    real_remove = kb.remove_board
    claims = []
    with connect_closing(board="race") as stale:
        def claim_then_remove(slug, *, archive):
            with connect_closing(board=slug) as conn:
                claims.append(kb.claim_task(conn, tid))
            return real_remove(slug, archive=archive)
        monkeypatch.setattr(kb, "remove_board", claim_then_remove)
        assert _call("kanban_boards", {"action": "delete", "board": "race"}).get("ok")
        assert claims == [None], "manual CLI claim must be excluded during removal"
        assert kb.claim_task(stale, tid) is None, "a stale connection must not claim a removed board"


def test_archived_connection_cannot_claim_after_board_slug_reuse(admin_home):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    assert _call("kanban_boards", {"action": "create", "board": "recycle"}).get("ok")
    tid = _call("kanban_create", {"board": "recycle", "title": "old card", "assignee": "default"})["task_id"]
    with connect_closing(board="recycle") as stale:
        assert _call("kanban_boards", {"action": "archive", "board": "recycle"}).get("ok")
        assert _call("kanban_boards", {"action": "create", "board": "recycle"}).get("ok")
        with connect_closing(board="recycle") as fresh:
            assert kb.get_task(fresh, tid) is None
        assert kb.claim_task(stale, tid) is None
        assert kb.get_task(stale, tid).status == "ready"


def test_implicit_model_selection_does_not_authorize_admin(admin_home):
    from model_tools import handle_function_call
    tid = _task()
    assert "kanban_tasks" not in _names(None)
    result = handle_function_call("kanban_tasks", {"action": "delete", "task_id": tid}, enabled_toolsets=None)
    assert "error" in json.loads(result)


def test_notification_write_checks_owner_atomically(admin_home, monkeypatch):
    from hermes_cli import kanban_db_notify as kbn
    from hermes_cli.kanban_db_connect import connect_closing
    tid = _task()
    real_add = kbn.add_notify_sub
    target = {"task_id": tid, "platform": "telegram", "chat_id": "race"}
    inserted = False

    def insert_other_then_subscribe(conn, **kw):
        nonlocal inserted
        if not inserted:
            inserted = True
            real_add(conn, **target, notifier_profile="other-profile", delivery_mode="notify")
        return real_add(conn, **kw)

    monkeypatch.setattr(kbn, "add_notify_sub", insert_other_then_subscribe)
    result = _call("kanban_notifications", {"action": "subscribe", **target, "delivery_mode": "wake"})
    assert "error" in result, result
    with connect_closing() as conn:
        row = kbn.list_notify_subs(conn, tid)[0]
        assert row["notifier_profile"] == "other-profile"
        assert row["delivery_mode"] == "notify"


def test_attachments_notifications_and_settings_preserve_profile_state(admin_home):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.config import load_config
    tid = _task()
    with connect_closing() as conn:
        attachment = kb.store_attachment_bytes(conn, tid, "readme.txt", b"evidence")
    assert _call("kanban_attachment_manage", {"action": "show", "attachment_id": attachment}).get("ok")
    assert _call("kanban_attachment_manage", {"action": "delete", "attachment_id": attachment}).get("ok")
    assert _call("kanban_notifications", {"action": "subscribe", "task_id": tid, "platform": "telegram", "chat_id": "123"}).get("ok")
    subs = _call("kanban_notifications", {"action": "list", "task_id": tid})
    assert subs.get("ok"), subs
    assert subs["subscriptions"][0]["chat_id"] == "123"
    assert _call("kanban_notifications", {"action": "unsubscribe", "task_id": tid, "platform": "telegram", "chat_id": "123"}).get("ok")
    result = _call("kanban_orchestration", {"action": "update", "settings": {"max_spawn": 2, "auto_decompose": False}})
    assert result.get("ok"), result
    assert load_config()["kanban"]["max_spawn"] == 2
    assert load_config()["platform_toolsets"]["cli"] == ["kanban_admin"]
    assert "error" in _call("kanban_orchestration", {"action": "update", "settings": {"made_up": True}})
