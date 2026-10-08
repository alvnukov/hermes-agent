"""Shared operator queue moves for model tools and dashboard drag/drop."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

from hermes_cli import kanban_db


def move_task_to_queue(conn, task_id, status, *, actor, reason=None, expected_hold_id=None):
    """Route resume/review transitions through domain verbs before a queue write."""
    if status not in {"ready", "todo", "triage"}:
        raise ValueError("queue status must be ready, todo or triage")
    task = kanban_db.get_task(conn, task_id)
    if task is None:
        return False
    if status == "ready" and task.dispatch_hold is not None:
        return kanban_db.unblock_task(conn, task_id, actor=actor, reason=reason, expected_hold_id=expected_hold_id)
    if expected_hold_id is not None:
        return False
    if status == "ready" and task.status in {"blocked", "scheduled"}:
        return kanban_db.unblock_task(conn, task_id)
    if task.status == "review" and status != "triage":
        return kanban_db.reopen_review_task(conn, task_id)
    return set_queue_status(conn, task_id, status, actor=actor)

def set_queue_status(conn: sqlite3.Connection, task_id: str, new_status: str, *, actor: str) -> bool:
    """Direct status write for drag-drop moves without a structured verb (todo<->ready,
    running<->ready) + a ``status`` event. Leaving ``running`` closes the run as 'reclaimed'
    so attempt history isn't orphaned; the worker is killed only AFTER the txn commits."""
    if new_status not in {"ready", "todo", "triage"}:
        raise ValueError("queue status must be ready, todo or triage")
    terminations: list[tuple[Optional[int], Optional[str], Optional[int]]] = []
    effective_status = new_status
    with kanban_db.write_txn(conn):
        prev = conn.execute(
            "SELECT status, current_run_id, worker_pid, claim_lock, worker_started_at FROM tasks WHERE id = ?",
            (task_id,)).fetchone()
        if prev is None:
            return False
        if prev["status"] == "running" and new_status == "ready":
            resume_status = kanban_db._retry_status_for_run(conn, task_id, prev["current_run_id"])
            if resume_status == "review":
                effective_status = "review" if kanban_db._parents_satisfied(conn, task_id) else "todo"
        # Never promote to 'ready' unless all parents are done/archived — otherwise the
        # dispatcher spawns a child whose upstream work hasn't completed.
        if effective_status == "ready" and not kanban_db._parents_satisfied(conn, task_id):
            return False
        was_running = prev["status"] == "running"
        reopening_satisfied_parent = prev["status"] in {"done", "archived"} and effective_status not in {"done", "archived"}
        cur = conn.execute(
            "UPDATE tasks SET status = ?, "
            "  claim_lock = CASE WHEN ? = 'running' THEN claim_lock ELSE NULL END, "
            "  claim_expires = CASE WHEN ? = 'running' THEN claim_expires ELSE NULL END, "
            "  worker_pid = CASE WHEN ? = 'running' THEN worker_pid ELSE NULL END "
            "WHERE id = ?",
            (effective_status,) * 4 + (task_id,))
        if cur.rowcount != 1:
            return False
        run_id = None
        if was_running and effective_status != "running" and prev["current_run_id"]:
            run_id = kanban_db._end_run(
                conn, task_id, outcome="reclaimed", status="reclaimed",
                summary=f"status changed to {effective_status} ({actor}/direct)")
            terminations.append((prev["worker_pid"], prev["claim_lock"], prev["worker_started_at"]))
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'status', ?, ?)",
            (task_id, run_id, json.dumps({"status": effective_status, "requested_status": new_status}), int(time.time())))
        if reopening_satisfied_parent:
            # Domain-layer invalidation composes via a savepoint inside our txn and hands
            # back worker terminations to perform post-commit.
            result = kanban_db.invalidate_descendants_for_parent_reopen(conn, task_id, author=actor)
            terminations.extend(result["terminations"])
    for pid, claim_lock, started_at in terminations:
        kanban_db._terminate_reclaimed_worker(pid, claim_lock, started_at=started_at)
    # Re-opening something may have made children stale.
    if effective_status in {"done", "ready", "review"}:
        kanban_db.recompute_ready(conn)
    return True
