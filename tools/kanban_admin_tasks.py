"""Card management through the existing domain transitions."""
from dataclasses import asdict

from tools.kanban_tools import _board, _check, _existing_task, _persisted_identity, _require_text


_FIELDS = {
    "edit": {"title", "body", "priority", "result", "summary", "metadata"},
    "assign": {"assignee"}, "reassign": {"assignee", "reclaim_first", "reason"},
    "move": {"status", "reason", "summary", "result", "metadata", "expected_hold_id"},
    "promote": {"reason", "expected_hold_id"}, "reopen_review": set(), "archive": set(), "delete": set(),
    "unlink": {"parent_id"}, "model": {"model", "provider"}, "reasoning": {"reasoning_effort"},
    "specify": set(), "decompose": {"children", "assignee", "auto_promote"},
}


def _edit(kb, conn, tid, args):
    fields = {key: args[key] for key in _FIELDS["edit"] if key in args}
    _check(fields, "edit requires at least one field")
    _check("result" in fields or not (fields.keys() & {"summary", "metadata"}),
           "summary/metadata require result when editing a completed card")
    if "title" in fields:
        fields["title"] = _require_text(args, "title").strip()
    return kb.edit_task(conn, tid, board=args.get("board"), **fields)


def _move(kb, conn, tid, args):
    from tools.kanban_admin_tools import _action_fields
    status = _require_text(args, "status")
    allowed = {
        "ready": {"reason", "expected_hold_id"}, "todo": set(), "triage": set(),
        "done": {"summary", "result", "metadata"}, "review": {"summary", "metadata"},
        "blocked": {"reason"}, "scheduled": {"reason"},
    }
    _action_fields(args, allowed[status] | {"task_id", "status"})
    if status in {"ready", "todo", "triage"}:
        from hermes_cli.kanban_db_status import move_task_to_queue
        return move_task_to_queue(conn, tid, status, actor=_persisted_identity(),
            reason=args.get("reason"), expected_hold_id=args.get("expected_hold_id"))
    from tools.registry import registry
    tool = {"done": "kanban_complete", "review": "kanban_request_review",
            "blocked": "kanban_block", "scheduled": "kanban_schedule"}[status]
    fields = {k: v for k, v in args.items() if k in {"board", "task_id", "summary", "result", "metadata", "reason"}}
    if status in {"blocked", "scheduled"}:
        fields = {k: v for k, v in fields.items() if k in {"board", "task_id", "reason"}}
    elif status == "review":
        fields.pop("result", None)
        fields.pop("reason", None)
    else:
        fields.pop("reason", None)
    # Lifecycle tools retain goal validation and expected-run ownership rules.
    return registry.dispatch(tool, fields)


def _delete(kb, conn, tid):
    task = _existing_task(kb, conn, tid)
    if task.status != "archived":
        _check(kb.archive_task(conn, tid), "cannot archive card before deleting it")
    return kb.delete_archived_task(conn, tid)


def _decompose(kb, conn, tid, args):
    from tools.kanban_admin_tools import _reply
    if "children" in args:
        from hermes_cli.kanban_db_graph import decompose_triage_task
        for child in args["children"]:
            _check(child["title"].strip(), "child title cannot be empty")
            _require_profile(child["assignee"])
        root_profile = args.get("assignee") or _existing_task(kb, conn, tid).assignee
        if root_profile:
            _require_profile(root_profile)
        ids = decompose_triage_task(conn, tid, root_assignee=root_profile, children=args["children"],
            author=_persisted_identity(), auto_promote=args.get("auto_promote", True))
        _check(ids is not None, "decomposition refused: task must be undecomposed triage with no dispatch hold")
        return _reply(task_id=tid, child_ids=ids)
    from hermes_cli.kanban_decompose import decompose_task
    from hermes_cli.kanban_db import scoped_current_board
    with scoped_current_board(args.get("board") or kb.get_current_board()):
        outcome = decompose_task(tid, author=_persisted_identity())
    _check(outcome.ok, outcome.reason)
    return _reply(**asdict(outcome))


def _require_profile(name):
    from hermes_cli.profiles import profile_exists
    _check(not name or profile_exists(name), f"unknown profile: {name}")


def _assign(kb, conn, tid, args):
    _check("assignee" in args, "assignee is required; empty string unassigns")
    _require_profile(args["assignee"])
    return kb.reassign_task(conn, tid, args["assignee"] or None,
        reclaim_first=args.get("reclaim_first", False), reason=args.get("reason"))


def _promote(kb, conn, tid, args):
    ok, reason = kb.promote_task(conn, tid, actor=_persisted_identity(),
        reason=args.get("reason"), expected_hold_id=args.get("expected_hold_id"))
    _check(ok, reason)
    return True


def _model(kb, conn, tid, args):
    _check("model" in args, "model is required; empty string clears the override")
    return kb.set_model_override(conn, tid, args["model"] or None, provider=args.get("provider"))


def _reasoning(kb, conn, tid, args):
    _check("reasoning_effort" in args, "reasoning_effort is required; empty string clears it")
    return kb.set_reasoning_effort(conn, tid, args["reasoning_effort"] or None)


def manage_task(args):
    from tools.kanban_admin_tools import _action_fields, _reply
    action = args["action"]
    _action_fields(args, _FIELDS[action] | {"task_id"})
    tid = _require_text(args, "task_id")
    with _board(args.get("board")) as (kb, conn):
        _existing_task(kb, conn, tid)
        if action == "move":
            result = _move(kb, conn, tid, args)
            if isinstance(result, str):
                return result
        elif action == "decompose":
            return _decompose(kb, conn, tid, args)
        elif action == "specify":
            from hermes_cli.kanban_db import scoped_current_board
            from hermes_cli.kanban_specify import specify_task
            with scoped_current_board(args.get("board") or kb.get_current_board()):
                outcome = specify_task(tid, author=_persisted_identity())
            _check(outcome.ok, outcome.reason)
            return _reply(**asdict(outcome))
        else:
            handlers = {
                "edit": lambda: _edit(kb, conn, tid, args),
                "assign": lambda: _assign(kb, conn, tid, args),
                "reassign": lambda: _assign(kb, conn, tid, args),
                "promote": lambda: _promote(kb, conn, tid, args),
                "reopen_review": lambda: kb.reopen_review_task(conn, tid),
                "archive": lambda: kb.archive_task(conn, tid),
                "delete": lambda: _delete(kb, conn, tid),
                "unlink": lambda: kb.unlink_tasks(conn, _require_text(args, "parent_id"), tid),
                "model": lambda: _model(kb, conn, tid, args),
                "reasoning": lambda: _reasoning(kb, conn, tid, args),
            }
            result = handlers[action]()
        _check(result, f"{action} refused: state, claim or dependencies do not allow this operation")
        task = kb.get_task(conn, tid)
        return _reply(task_id=tid, **({"task": task} if task else {"deleted": True}))
