"""Operator claim control and coordination of board removal with manual claims."""
from __future__ import annotations

import functools
import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


def board_file_identity(path: Path):
    """Stat only: opening/closing the DB file would cancel SQLite's POSIX locks."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


@contextmanager
def board_lifecycle_lock(db_path: Path):
    """Nonblocking claim/removal guard stored outside the removable board tree."""
    from hermes_cli.kanban_db import kanban_home
    from hermes_cli.kanban_db_connect import _try_lock_nb, _unlock

    key = hashlib.sha256(str(db_path.resolve()).encode()).hexdigest()
    lock_dir = kanban_home() / "kanban" / ".lifecycle-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"{key}.lock").open("a+b") as handle:
        held = _try_lock_nb(handle)
        try:
            yield held
        finally:
            if held:
                _unlock(handle)


def guard_board_claim(fn):
    """Every claim writer, including CLI claims, participates in removal fencing."""
    @functools.wraps(fn)
    def guarded(conn, *args, **kwargs):
        from hermes_cli.kanban_db_connect import _main_db_file
        db_file = _main_db_file(conn)
        if not db_file:  # In-memory stores have no removable directory.
            return fn(conn, *args, **kwargs)
        path = Path(db_file)
        with board_lifecycle_lock(path) as held:
            identity = getattr(conn, "_hermes_kanban_file_identity", None)
            if not held or identity is None or identity != board_file_identity(path):
                return None
            return fn(conn, *args, **kwargs)
    return guarded


def reclaim_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None, signal_fn=None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Operator reclaim regardless of TTL: release the claim, restore the source
    phase, reset the failure counter. False when not running."""
    from hermes_cli import kanban_db as kb

    row = conn.execute(
        "SELECT status, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ? AND (? IS NULL OR current_run_id = ?)",
        (task_id, expected_run_id, expected_run_id),
    ).fetchone()
    if not row:
        return False
    if row["status"] != "running" and row["claim_lock"] is None:
        # Nothing to reclaim — already ready / blocked / done.
        return False
    prev_lock = row["claim_lock"]
    termination = kb._terminate_reclaimed_worker(
        row["worker_pid"], prev_lock, signal_fn=signal_fn, started_at=row["worker_started_at"])
    with kb.write_txn(conn):
        retry_status = kb._retry_status_for_run(conn, task_id)
        cur = conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND status IN ('running', 'ready', 'blocked') "
            "AND claim_lock IS ? AND (? IS NULL OR current_run_id = ?)",
            (retry_status, task_id, prev_lock, expected_run_id, expected_run_id),
        )
        if cur.rowcount != 1:
            return False
        kb._record_reclaim(
            conn, task_id, termination,
            error=f"manual_reclaim: {reason}" if reason else f"manual_reclaim lock={prev_lock}",
            payload={"manual": True, "reason": reason, "prev_lock": prev_lock, "retry_status": retry_status},
        )
    # Operator intervention = fresh retry budget (own txn, runs after commit).
    kb._clear_failure_counter(conn, task_id)
    return True
