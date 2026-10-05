"""Durable scheduling holds, separate from a task's workflow status."""

import json
import uuid
from typing import Optional

HOLD_KINDS = frozenset({"block_loop", "manual_approval", "policy_gate"})


def decode_dispatch_hold(raw) -> Optional[dict]:
    if raw is None:
        return None
    try:
        hold = json.loads(raw)
        if (isinstance(hold, dict) and type(hold.get("schema")) is int and hold.get("schema") == 1
                and hold.get("kind") in HOLD_KINDS
                and isinstance(hold.get("hold_id"), str) and hold["hold_id"]
                and isinstance(hold.get("cause_key"), str) and hold["cause_key"]
                and hold.get("resume_status") in ("ready", "review")
                and isinstance(hold.get("context"), dict)):
            source = hold.get("source_event_id")
            context = hold["context"]
            if source is not None and (type(source) is not int or source <= 0):
                raise ValueError("invalid source event ID")
            for key in ("gate_id", "gate_revision", "operation", "task_fingerprint", "held_task_fingerprint"):
                if key in context and (not isinstance(context[key], str) or not context[key].strip()):
                    raise ValueError(f"invalid hold context {key}")
            if "block_event_id" in context and (type(context["block_event_id"]) is not int or context["block_event_id"] <= 0):
                raise ValueError("invalid block event ID")
            return hold
    except (TypeError, ValueError):
        pass
    return {"kind": "invalid", "invalid": True}


def get_dispatch_hold(conn, task_id: str) -> Optional[dict]:
    row = conn.execute("SELECT dispatch_hold FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"task {task_id} not found")
    return decode_dispatch_hold(row["dispatch_hold"])


def _require_transaction(conn):
    if not conn.in_transaction:
        raise RuntimeError("dispatch hold mutation requires the caller's write transaction")


def _bounded_evidence(value: dict) -> dict:
    from hermes_cli import kanban_db as kb
    cleaned = kb.redact_review_value(value)
    if not isinstance(cleaned, dict) or len(json.dumps(cleaned, ensure_ascii=False)) > 4096:
        raise ValueError("dispatch hold evidence must be a bounded object (4096 characters)")
    return cleaned


def set_dispatch_hold(
    conn, task_id: str, *, kind: str, cause_key: str, resume_status: str,
    source_event_id: Optional[int] = None, context: Optional[dict] = None,
) -> dict:
    from hermes_cli import kanban_db as kb
    _require_transaction(conn)
    if kind not in HOLD_KINDS or resume_status not in ("ready", "review") or not isinstance(cause_key, str) or not cause_key:
        raise ValueError("invalid dispatch hold kind, cause or resume phase")
    current = get_dispatch_hold(conn, task_id)
    if current is not None:
        if (current.get("kind"), current.get("cause_key"), current.get("resume_status")) == (kind, cause_key, resume_status):
            return current
        raise ValueError("task already has a different active hold")
    hold = {
        "schema": 1, "hold_id": uuid.uuid4().hex, "kind": kind,
        "cause_key": cause_key, "resume_status": resume_status,
        "source_event_id": source_event_id, "context": _bounded_evidence(context or {}),
    }
    conn.execute("UPDATE tasks SET dispatch_hold = ? WHERE id = ? AND dispatch_hold IS NULL", (json.dumps(hold, ensure_ascii=False), task_id))
    kb._append_event(conn, task_id, "dispatch_held", hold)
    return hold


def release_dispatch_hold(conn, task_id: str, *, expected_hold_id: str, wake_kind: str, actor: str, evidence: dict) -> bool:
    from hermes_cli import kanban_db as kb
    _require_transaction(conn)
    if not isinstance(expected_hold_id, str) or not expected_hold_id.strip():
        return False
    row = conn.execute("SELECT dispatch_hold FROM tasks WHERE id = ?", (task_id,)).fetchone()
    hold = decode_dispatch_hold(row["dispatch_hold"]) if row else None
    if hold is None or hold.get("invalid") or hold.get("hold_id") != expected_hold_id:
        return False
    if not actor or not wake_kind:
        raise ValueError("dispatch hold release requires actor and wake kind")
    payload = {"hold_id": expected_hold_id, "wake_kind": wake_kind, "actor": actor, "evidence": _bounded_evidence(evidence)}
    changed = conn.execute("UPDATE tasks SET dispatch_hold = NULL WHERE id = ? AND dispatch_hold = ?", (task_id, row["dispatch_hold"])).rowcount
    if changed != 1:
        return False
    kb._append_event(conn, task_id, "dispatch_hold_released", payload)
    return True


def operator_release_matches(hold: Optional[dict], *, actor, reason, expected_hold_id) -> bool:
    """A release token is a concurrency fence; caller supplies operator authority."""
    import os
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return bool(hold and isinstance(actor, str) and actor.strip()
                and isinstance(reason, str) and reason.strip()
                and expected_hold_id and hold.get("hold_id") == expected_hold_id)


def policy_rejection_evidence(conn, task_id: str, *, event_id, expected_run_id) -> Optional[dict]:
    """Resolve a fresh mechanical rejection; caller owns the write transaction."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_goal_gate import gate_revisions, task_fingerprint
    if type(event_id) is not int or event_id <= 0:
        return None
    task = kb.get_task(conn, task_id)
    if task is None or task.status != "running" or task.current_run_id is None:
        return None
    if expected_run_id is not None and task.current_run_id != expected_run_id:
        return None
    event = conn.execute(
        "SELECT * FROM task_events WHERE id = ? AND task_id = ? AND kind = 'goal_gate_rejected' AND run_id = ?",
        (event_id, task_id, task.current_run_id),
    ).fetchone()
    if event is None:
        return None
    latest = conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'goal_gate_rejected'",
        (task_id, task.current_run_id),
    ).fetchone()[0]
    payload = kb._json_dict(event["payload"])
    if (latest != event_id or payload.get("blockable") is not True
            or payload.get("category") not in ("input_too_large", "unsupported_context_schema")
            or payload.get("operation") != "request_review"
            or payload.get("gate_id") not in gate_revisions()
            or payload.get("gate_revision") != gate_revisions().get(payload.get("gate_id"))
            or payload.get("task_fingerprint") != task_fingerprint(task)):
        return None
    return {key: payload.get(key) for key in (
        "gate_id", "gate_revision", "operation", "candidate_fingerprint", "task_fingerprint",
    )}


def reconcile_policy_holds(conn, *, gate_revisions: dict[str, str]) -> int:
    """Release once after a relevant native revision, retaining task/run history."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_goal_gate import task_fingerprint
    released = 0
    with kb.write_txn(conn):
        rows = conn.execute("SELECT id FROM tasks WHERE dispatch_hold IS NOT NULL AND status IN ('blocked', 'triage') AND current_run_id IS NULL").fetchall()
        for row in rows:
            tid = row["id"]
            hold = get_dispatch_hold(conn, tid)
            if not hold or hold.get("kind") != "policy_gate":
                continue
            context = hold["context"]
            revision = gate_revisions.get(context.get("gate_id"))
            prior_revision = context.get("gate_revision")
            if (not isinstance(revision, str) or not revision
                    or not isinstance(prior_revision, str) or not prior_revision
                    or revision == prior_revision):
                continue
            source = conn.execute(
                "SELECT payload FROM task_events WHERE id = ? AND task_id = ? AND kind = 'goal_gate_rejected'",
                (hold.get("source_event_id"), tid),
            ).fetchone()
            payload = kb._json_dict(source["payload"] if source else None)
            if (payload.get("blockable") is not True
                    or payload.get("category") not in ("input_too_large", "unsupported_context_schema")
                    or any(payload.get(key) != context.get(key) for key in (
                        "gate_id", "gate_revision", "operation", "candidate_fingerprint", "task_fingerprint"))
                    or context.get("held_task_fingerprint") != task_fingerprint(kb.get_task(conn, tid))
                    or type(context.get("block_event_id")) is not int):
                continue
            if conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND id > ? AND kind IN ('blocked', 'block_loop_detected', 'dependency_wait', 'scheduled') LIMIT 1",
                (tid, context["block_event_id"]),
            ).fetchone():
                continue
            if not release_dispatch_hold(conn, tid, expected_hold_id=hold["hold_id"], wake_kind="gate_revision",
                    actor="dispatcher", evidence={"gate_id": context["gate_id"], "from_revision": context["gate_revision"], "to_revision": revision}):
                continue
            phase = hold["resume_status"]
            status = phase if kb._parents_satisfied(conn, tid) else "todo"
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
            kb._append_event(conn, tid, "unblocked", {"status": status, "resume_status": phase, "wake_kind": "gate_revision"})
            released += 1
    return released


def _loop_resolution(conn, task_id: str, loop_id: int) -> tuple[bool, bool]:
    """Only a paired native release establishes authority; old resumes are ambiguous."""
    from hermes_cli import kanban_db as kb
    held = {}
    ambiguous = False
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND id > ? "
        "AND kind IN ('dispatch_held', 'dispatch_hold_released', 'promoted_manual', 'unblocked') ORDER BY id",
        (task_id, loop_id),
    )
    for row in rows:
        payload = kb._json_dict(row["payload"])
        if row["kind"] == "dispatch_held":
            native_hold = decode_dispatch_hold(row["payload"])
            if native_hold is None or native_hold.get("invalid"):
                ambiguous = True
                continue
            context = payload.get("context")
            if (payload.get("source_event_id") == loop_id
                    or isinstance(context, dict) and context.get("block_event_id") == loop_id):
                if isinstance(payload.get("hold_id"), str):
                    held[payload["hold_id"]] = payload
        elif row["kind"] == "dispatch_hold_released":
            token = payload.get("hold_id")
            matching = held.get(token) if isinstance(token, str) else None
            evidence = payload.get("evidence")
            actor = payload.get("actor")
            if not matching or not isinstance(evidence, dict) or not isinstance(actor, str) or not actor.strip():
                ambiguous = True
                continue
            if payload.get("wake_kind") == "operator" and isinstance(evidence.get("reason"), str) and evidence["reason"].strip():
                return True, ambiguous
            context = matching.get("context", {})
            if (payload.get("wake_kind") == "gate_revision" and matching.get("kind") == "policy_gate"
                    and evidence.get("gate_id") == context.get("gate_id")
                    and evidence.get("from_revision") == context.get("gate_revision")
                    and isinstance(evidence.get("to_revision"), str) and evidence["to_revision"]
                    and evidence["to_revision"] != evidence.get("from_revision")):
                return True, ambiguous
            ambiguous = True
        else:
            ambiguous = True
    return False, ambiguous


def backfill_dispatch_holds(conn, *, dry_run: bool = True) -> dict:
    """Explicit legacy preview/apply; no workflow transitions or process actions."""
    from contextlib import nullcontext
    from hermes_cli import kanban_db as kb
    report = {"candidates": [], "held": [], "skipped": [], "ambiguous": []}
    with (nullcontext() if dry_run else kb.write_txn(conn)):
        rows = conn.execute(
            "SELECT t.id, t.status, t.dispatch_hold, e.id AS loop_id, e.payload "
            "FROM tasks t JOIN task_events e ON e.id = "
            "(SELECT MAX(id) FROM task_events WHERE task_id = t.id AND kind = 'block_loop_detected') ORDER BY t.id"
        ).fetchall()
        for row in rows:
            tid = row["id"]
            skip = None
            if row["status"] in ("done", "archived"):
                skip = "terminal"
            elif row["dispatch_hold"] is not None:
                skip = "already_held"
            resolved, ambiguous = _loop_resolution(conn, tid, row["loop_id"])
            if resolved:
                skip = "native_resolution"
            if skip:
                report["skipped"].append({"task_id": tid, "reason": skip})
                continue
            payload = kb._json_dict(row["payload"])
            phase = payload.get("source_status")
            if phase not in ("ready", "review"):
                ambiguous = True
                phase = "review" if row["status"] == "review" else kb._retry_status_for_run(conn, tid)
            if payload.get("kind") not in kb.VALID_BLOCK_KINDS and payload.get("kind") is not None:
                ambiguous = True
            report["candidates"].append(tid)
            if ambiguous:
                report["ambiguous"].append(tid)
            if not dry_run:
                set_dispatch_hold(conn, tid, kind="block_loop", cause_key=f"legacy_loop:{row['loop_id']}",
                                  resume_status=phase, source_event_id=row["loop_id"],
                                  context={"legacy": True, "ambiguous": ambiguous, "legacy_block_kind": payload.get("kind")})
                report["held"].append(tid)
    return report
