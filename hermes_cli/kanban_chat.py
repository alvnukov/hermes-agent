"""Route card-chat corrections through the worker's existing operator-note channel."""
from __future__ import annotations

import os
from pathlib import Path


def cli_session_metadata(session_id: str) -> dict:
    """Advertise a dispatcher worker's board and attempt alongside its writer identity."""
    from agent.delegation_context import is_dispatcher_owned_worker_context
    from hermes_cli.kanban_db import kanban_db_path

    metadata: dict = {"live_session_id": str(session_id)}
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    if task_id and run_id.isdecimal() and is_dispatcher_owned_worker_context():
        metadata["kanban_worker"] = {"task_id": task_id, "run_id": int(run_id),
                                     "db_path": str(kanban_db_path().resolve())}
    return metadata


def worker_chat_owner(session: dict, resolve_tip) -> dict | None:
    """Find this profile's worker, including its proven compression continuation."""
    from hermes_cli.active_sessions import active_session_registry_snapshot

    key = str(session.get("session_key") or "")
    owners = []
    for entry in active_session_registry_snapshot(session.get("profile_home"), strict=True):
        if entry.get("surface") != "cli" or not isinstance((entry.get("metadata") or {}).get("kanban_worker"), dict):
            continue
        anchor = entry["session_id"]
        if anchor == key or resolve_tip(anchor) == key:
            owners.append(entry)
    if len(owners) > 1:
        raise ValueError("Kanban worker session ownership is ambiguous")
    return owners[0] if owners else None


def submit_worker_note(session: dict, text: object, params: dict, owner: dict) -> bool:
    """Accept plain operator text only for a proven live worker and its current attempt.

    Discovery is scoped to the session's profile. The registry advertises the board;
    the board transaction fences the attempt, PID and durable session binding. Nothing
    in this path writes the transcript or acquires/releases the worker's session lease.
    """
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_db_dispatch import _process_fingerprint

    if (not isinstance(text, str) or not text.strip() or session.get("attached_images")
            or any(params.get(k) is not None for k in (
                "truncate_before_row_id", "truncate_before_message_id", "truncate_before_user_ordinal",
                "rebind_survivor_row_ids", "_hosted_task", "_hosted_terminal_callback", "_turn_author"))
            or params.get("queued") or params.get("display_kind") or params.get("confirm_truncate")):
        return False
    key = owner["session_id"]
    binding = (owner.get("metadata") or {}).get("kanban_worker")
    if not isinstance(binding, dict):
        return False
    path = Path(binding["db_path"])
    if not path.is_absolute() or not path.is_file():
        return False
    with kbc.connect_closing(db_path=path) as conn, kb.write_txn(conn):
        task = kb.get_task(conn, binding["task_id"])
        run = kb.get_run(conn, binding["run_id"])
        fingerprint = conn.execute("SELECT worker_started_at FROM task_runs WHERE id=?", (binding["run_id"],)).fetchone()
        if (task is None or run is None or task.status != "running"
                or task.current_run_id != run.id or run.task_id != task.id or run.ended_at is not None
                or run.worker_pid != owner["pid"]
                or fingerprint[0] != _process_fingerprint(owner["pid"])
                or (run.metadata or {}).get("worker_session_id") != key):
            return False
        # Distinct from the worker profile identity so its echo filter retains the note.
        kb.add_comment(conn, task.id, author="[operator]", body=text)
    return True
