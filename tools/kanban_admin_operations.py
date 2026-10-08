"""Execution, attachments, notifications and profile orchestration controls."""
from hermes_cli.config import load_config, save_config
from tools.kanban_tools import _board, _check, _existing_task, _persisted_identity, _require_text

_WORKER_FIELDS = {
    "list": set(), "runs": {"task_id"}, "show": {"run_id"},
    "log": {"task_id", "tail_bytes"}, "reclaim": {"task_id", "reason"},
    "terminate": {"run_id", "reason"},
}


def manage_workers(args):
    from tools.kanban_admin_tools import _action_fields, _reply
    action = args["action"]
    _action_fields(args, _WORKER_FIELDS[action])
    with _board(args.get("board")) as (kb, conn):
        if action == "list":
            tasks = kb.list_tasks(conn, status="running")
            return _reply(workers=[{"task": task, "run": kb.get_run(conn, task.current_run_id) if task.current_run_id else None} for task in tasks])
        if action in {"show", "terminate"}:
            _check("run_id" in args, "run_id is required")
            run = kb.get_run(conn, args["run_id"])
            _check(run is not None, "run not found")
            if action == "show":
                return _reply(run=run)
            task = _existing_task(kb, conn, run.task_id)
            _check(run.ended_at is None and task.current_run_id == run.id, "run already ended or no longer owns the card")
            _check(kb.reclaim_task(conn, run.task_id, reason=args.get("reason"), expected_run_id=run.id), "run cannot be reclaimed")
            return _reply(task_id=run.task_id, run_id=run.id)
        tid = _require_text(args, "task_id")
        _existing_task(kb, conn, tid)
        if action == "runs":
            return _reply(task_id=tid, runs=kb.list_runs(conn, tid))
        if action == "log":
            content = kb.read_worker_log(tid, tail_bytes=args.get("tail_bytes", 16384), board=args.get("board"))
            return _reply(task_id=tid, exists=content is not None, content=content or "")
        _check(kb.reclaim_task(conn, tid, reason=args.get("reason")), "card has no reclaimable worker claim")
        return _reply(task_id=tid, task=kb.get_task(conn, tid))


def dispatch(args):
    from tools.kanban_admin_tools import _reply
    from hermes_cli import kanban_db_dispatch as kbd
    from dataclasses import asdict
    config = load_config().get("kanban") or {}
    with _board(args.get("board")) as (_, conn):
        result = kbd.dispatch_once(conn, board=args.get("board"), dry_run=args.get("dry_run", False),
            max_spawn=args.get("max_spawn", kbd._positive_int(config.get("max_spawn"), 0) or None),
            max_in_progress=kbd.resolve_max_in_progress(kbd._positive_int(config.get("max_in_progress"), 0) or None),
            max_in_progress_per_profile=kbd._positive_int(config.get("max_in_progress_per_profile"), 0) or None,
            failure_limit=kbd._positive_int(config.get("failure_limit"), kbd.DEFAULT_FAILURE_LIMIT),
            default_assignee=(config.get("default_assignee") or "").strip() or None)
        return _reply(**asdict(result))


def manage_attachment(args):
    from tools.kanban_admin_tools import _reply
    with _board(args.get("board")) as (kb, conn):
        attachment = kb.get_attachment(conn, args["attachment_id"])
        _check(attachment is not None, "attachment not found")
        if args["action"] == "delete":
            _check(kb.delete_attachment(conn, attachment.id) is not None, "attachment removal refused")
        return _reply(attachment=attachment, deleted=args["action"] == "delete")


def manage_notifications(args):
    from tools.kanban_admin_tools import _action_fields, _reply
    from hermes_cli import kanban_db_notify as kbn
    action = args["action"]
    _action_fields(args, {"task_id"} if action == "list" else
        {"task_id", "platform", "chat_id", "thread_id"} | ({"delivery_mode"} if action == "subscribe" else set()))
    profile = _persisted_identity()
    with _board(args.get("board")) as (kb, conn):
        if action == "list":
            return _reply(subscriptions=kbn.list_notify_subs(conn, args.get("task_id"), notifier_profiles=[profile]))
        tid = _require_text(args, "task_id")
        _existing_task(kb, conn, tid)
        target = {"task_id": tid, "platform": _require_text(args, "platform"),
            "chat_id": _require_text(args, "chat_id"), "thread_id": args.get("thread_id")}
        if action == "subscribe":
            kbn.add_notify_sub(conn, **target, notifier_profile=profile, delivery_mode=args.get("delivery_mode"),
                expected_notifier_profile=profile)
        else:
            _check(kbn.remove_notify_sub(conn, **target, expected_notifier_profile=profile), "subscription not found or belongs to another profile")
        return _reply(task_id=tid, subscriptions=kbn.list_notify_subs(conn, tid, notifier_profiles=[profile]))


def manage_orchestration(args):
    from tools.kanban_admin_tools import _action_fields, _reply
    from tools.kanban_admin_schemas import ORCHESTRATION_FIELDS
    from tools.kanban_admin_tasks import _require_profile
    _action_fields(args, {"settings"} if args["action"] == "update" else set())
    config = load_config()
    if args["action"] == "update":
        settings = args.get("settings")
        _check(settings, "update requires nonempty settings")
        for key in ("orchestrator_profile", "default_assignee"):
            if key in settings:
                _require_profile(settings[key])
        section = config.setdefault("kanban", {})
        _check(isinstance(section, dict), "kanban configuration must be an object")
        section.update(settings)
        save_config(config)
    section = config.get("kanban") or {}
    return _reply(settings={key: section.get(key) for key in ORCHESTRATION_FIELDS})
