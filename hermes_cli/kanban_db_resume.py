"""Operator promotion and phase-preserving resume transitions."""

from __future__ import annotations

import sqlite3
from typing import Optional


def promote_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: Optional[str] = None,
    dry_run: bool = False, expected_hold_id: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Operator promotion ``todo``/``blocked`` -> ``ready`` with an audit event.
    Refused while a parent is unfinished; ``dry_run`` only validates.
    Returns ``(ok, reason)``."""
    from hermes_cli import kanban_db as _kb
    cur_status = _kb._task_status(conn, task_id)
    if cur_status is None:
        return False, f"task {task_id} not found"

    from hermes_cli.kanban_db_holds import get_dispatch_hold, operator_release_matches, release_dispatch_hold
    hold = get_dispatch_hold(conn, task_id)
    if hold is not None and not operator_release_matches(hold, actor=actor, reason=reason, expected_hold_id=expected_hold_id):
        return False, "dispatch hold requires an explicit operator reason and current hold ID"
    if cur_status not in ("todo", "blocked") and not (hold is not None and cur_status == "triage"):
        return False, (
            f"task {task_id} is {cur_status!r}; promote only applies to "
            f"'todo' or 'blocked'"
        )

    # No override: claim_task demotes ready -> todo on an undone parent whichever
    # writer set 'ready', so a forced promotion would only report a success the
    # first claim silently reverts (#106195). The dependency itself is the knob.
    parents = conn.execute(
        "SELECT t.id, t.status FROM tasks t "
        "JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ?", (task_id,),
    ).fetchall()
    unsatisfied = [p["id"] for p in parents if p["status"] not in ("done", "archived")]
    if unsatisfied:
        return False, (
            f"unsatisfied parent dependencies: {', '.join(unsatisfied)} "
            f"(the ready -> running claim re-checks parents, so promotion cannot "
            f"bypass them; complete the parents or drop the link with "
            f"`hermes kanban unlink <parent_id> {task_id}`)"
        )

    if dry_run:
        return True, None

    with _kb.write_txn(conn):
        if not _kb._parents_satisfied(conn, task_id):
            return False, "parent dependencies changed during promotion"
        if _kb._task_status(conn, task_id) != cur_status:
            return False, "task status changed during promotion"
        hold = get_dispatch_hold(conn, task_id)
        if hold is not None:
            if not operator_release_matches(hold, actor=actor, reason=reason, expected_hold_id=expected_hold_id):
                return False, "dispatch hold changed during promotion"
            resume_status = hold["resume_status"]
            release_dispatch_hold(conn, task_id, expected_hold_id=expected_hold_id, wake_kind="operator",
                                  actor=actor, evidence={"reason": reason})
        else:
            if expected_hold_id is not None:
                return False, "dispatch hold changed during promotion"
            resume_status = _kb._resume_status_from_events(conn, task_id)
        upd = conn.execute(
            "UPDATE tasks SET status = ? "
            "WHERE id = ? AND status IN ('todo', 'blocked', 'triage')", (resume_status, task_id),
        )
        if upd.rowcount != 1:
            return False, f"task {task_id} status changed during promotion"
        _kb._append_event(conn, task_id, "promoted_manual", {
            "actor": actor, "reason": reason, "resume_status": resume_status,
        })

    return True, None


def unblock_task(conn: sqlite3.Connection, task_id: str, *, actor: Optional[str] = None,
                 reason: Optional[str] = None, expected_hold_id: Optional[str] = None) -> bool:
    """``blocked``/``scheduled`` -> its resumable phase (parent re-gated; ``review``
    when that is where it left off), closing any leaked run first."""
    from hermes_cli import kanban_db as _kb
    now = int(_kb.time.time())
    with _kb.write_txn(conn):
        from hermes_cli.kanban_db_holds import get_dispatch_hold, operator_release_matches, release_dispatch_hold
        if _kb._task_status(conn, task_id) is None:
            return False
        hold = get_dispatch_hold(conn, task_id)
        statuses = ("blocked", "scheduled")
        if hold is not None:
            statuses += ("triage", "todo", "ready", "review")
            if _kb._task_status(conn, task_id) not in statuses or not operator_release_matches(
                    hold, actor=actor, reason=reason, expected_hold_id=expected_hold_id):
                return False
            resume_status = hold["resume_status"]
            release_dispatch_hold(conn, task_id, expected_hold_id=expected_hold_id, wake_kind="operator",
                                  actor=actor, evidence={"reason": reason})
        else:
            if expected_hold_id is not None:
                return False
            resume_status = _kb._resume_status_from_events(conn, task_id)
        _kb._reclaim_dangling_run(
            conn, task_id, statuses=statuses, now=now,
            note="invariant recovery on unblock",
        )
        # Re-gate on parent completion before restoring the source phase.
        landing_status = _kb._landing_status_after_parents(conn, task_id)
        new_status = (
            "review"
            if landing_status == "ready" and resume_status == "review"
            else landing_status
        )
        # ``block_kind``/``block_recurrences`` deliberately survive the unblock:
        # resetting them is the amnesia that let cron-unblock <-> re-block loop
        # unbounded; only complete_task clears them. ``consecutive_failures``
        # (the dispatcher's spawn/crash counter) resets only for legacy unblocks;
        # releasing a hold preserves the failure history.
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = CASE WHEN ? THEN consecutive_failures ELSE 0 END, "
            "last_failure_error = CASE WHEN ? THEN last_failure_error ELSE NULL END "
            f"WHERE id = ? AND status IN ({','.join('?' for _ in statuses)})",
            (new_status, hold is not None, hold is not None, task_id, *statuses),
        )
        if cur.rowcount != 1:
            return False
        _kb._append_event(
            conn, task_id, "unblocked",
            (
                {"status": new_status, "resume_status": resume_status}
                if new_status != "ready" or resume_status != "ready"
                else None
            ),
        )
        return True
