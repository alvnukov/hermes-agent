"""Worker registration and run closure, including the durable chat lineage anchor."""
from __future__ import annotations

import sqlite3
import time
from typing import Optional


def adopt_worker_pid(
    conn: sqlite3.Connection, task_id: str, run_id: int, pid: int, *, session_id: Optional[str] = None,
) -> bool:
    """Register a worker before its first model call; reject a reclaimed run.

    A dispatcher can die between spawning a worker and recording its pid. The
    worker fills the missing host-local pid and links its persisted session to
    the same live attempt. Keep the first session as the compression lineage anchor.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as dispatch

    started_at = dispatch._process_fingerprint(int(pid)) or dispatch.UNVERIFIED_WORKER_FINGERPRINT
    with kb.write_txn(conn):
        row = conn.execute("SELECT status, current_run_id, worker_pid, claim_lock FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
        if row is None or row["status"] != "running" or row["current_run_id"] != int(run_id):
            return False
        # A pid from another host or pid namespace proves nothing here.
        if row["worker_pid"] is None and (row["claim_lock"] or "").startswith(kb._host_prefix()):
            conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, task_id))
            conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, int(run_id)))
            kb._append_event(conn, task_id, "worker_registered", {"pid": int(pid), "started_at": started_at},
                             run_id=int(run_id))
        if session_id:
            run = kb.get_run(conn, int(run_id))
            metadata = dict(run.metadata or {})
            if not metadata.get("worker_session_id"):
                metadata["worker_session_id"] = session_id
                conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?",
                             (kb._json_or_null(metadata), int(run_id)))
    return True


def _end_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str] = None,
    error: Optional[str] = None, metadata: Optional[dict] = None, status: Optional[str] = None,
) -> Optional[int]:
    """Close the active run and clear current_run_id; None for an unclaimed task.

    Retain the worker process evidence for terminal reaping and the original chat
    anchor for navigation after block, failure, review or completion.
    """
    from hermes_cli import kanban_db as kb

    now = int(time.time())
    run_id = kb._current_run_id(conn, task_id)
    if run_id is None:
        return None
    run = kb.get_run(conn, run_id)
    worker_session_id = (run.metadata or {}).get("worker_session_id") if run else None
    if worker_session_id:
        metadata = {**(metadata or {}), "worker_session_id": worker_session_id}
    conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               claim_expires = NULL
         WHERE id = ?
           AND ended_at IS NULL
        """,
        (status or outcome, outcome, summary, error, kb._json_or_null(metadata), now, run_id),
    )
    conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,))
    return run_id
