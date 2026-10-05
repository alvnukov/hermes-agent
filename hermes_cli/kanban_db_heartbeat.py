"""Run-fenced worker heartbeat transactions."""

from __future__ import annotations

import sqlite3
from typing import Optional


def heartbeat_current_run(
    conn: sqlite3.Connection, task_id: str, *, expected_run_id: int,
    claimer: Optional[str] = None, note: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
) -> bool:
    """Extend both liveness fields only for the same run and claim owner."""
    from hermes_cli import kanban_db_dispatch as _dispatch
    _kb = _dispatch._kb
    time = _dispatch.time
    if type(expected_run_id) is not int or expected_run_id <= 0:
        return False
    now = int(time.time())
    expires = now + _kb._resolve_claim_ttl_seconds(ttl_seconds)
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock = ? AND current_run_id = ?",
            (expires, now, task_id, claimer or _kb._claimer_id(), expected_run_id),
        )
        if cur.rowcount != 1:
            return False
        _kb._extend_run_claim(conn, task_id, expires)
        conn.execute("UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (now, expected_run_id))
        _kb._append_event(conn, task_id, "heartbeat", {"note": note} if note else None, run_id=expected_run_id)
    return True


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    Liveness signal orthogonal to the PID check: a worker whose forked child
    (train loop, crawl) is stuck can still have a live Python process.
    Returns False if the task is not running or its claim expired.
    """
    from hermes_cli import kanban_db_dispatch as _dispatch
    _kb = _dispatch._kb
    time = _dispatch.time
    now = int(time.time())
    with _kb.write_txn(conn):
        sql = "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ? AND status = 'running'"
        params: tuple = (now, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params += (int(expected_run_id),)
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _kb._current_run_id(conn, task_id)
        )
        if run_id is not None:
            conn.execute("UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (now, run_id))
        _kb._append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=run_id,
        )
    return True
