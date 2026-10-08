"""Named-board administration; destructive operations never orphan workers."""
from pathlib import Path

from hermes_cli import kanban_db as kb
from tools.kanban_tools import _board, _check, _require_text

_META = {"name", "description", "icon", "color", "default_workdir", "project"}
_FIELDS = {
    "list": {"include_archived"}, "show": set(), "stats": set(),
    "create": _META, "update": _META, "switch": set(), "archive": set(), "delete": set(),
    "export": {"path", "include_attachments", "include_logs"}, "import": {"path", "activate"},
}


def _metadata(args):
    fields = {key: args[key] for key in _META - {"project"} if key in args}
    workdir = fields.get("default_workdir")
    if workdir:
        path = Path(workdir).expanduser()
        _check(path.is_absolute() and path.is_dir(), "default_workdir must be an existing absolute directory")
        fields["default_workdir"] = str(path.resolve())
    if "project" in args:
        project = args["project"]
        if project:
            from hermes_cli.projects_db import connect_closing, get_project
            with connect_closing() as conn:
                resolved = get_project(conn, project)
            if resolved is None:
                raise ValueError(f"unknown project: {project}")
            fields["project_id"] = resolved.id
            if "default_workdir" not in fields:
                fields["default_workdir"] = resolved.primary_path
        else:
            fields["project_id"] = ""
    return fields


def _remove(slug, *, archive):
    # Removing pinned storage while a worker lives would recreate an empty DB.
    from hermes_cli.kanban_db_connect import _dispatch_tick_lock
    from hermes_cli.kanban_db_control import board_lifecycle_lock
    path = kb.kanban_db_path(slug)
    with _dispatch_tick_lock(path) as held, board_lifecycle_lock(path) as lifecycle_held:
        _check(held, "board dispatcher is busy; retry after its current tick")
        _check(lifecycle_held, "board is being claimed; retry after the claim completes")
        with _board(slug) as (_, conn):
            active = conn.execute("SELECT id FROM tasks WHERE status = 'running' OR claim_lock IS NOT NULL").fetchall()
            _check(not active, "board owns active workers; reclaim or archive their cards before removing the board")
        return kb.remove_board(slug, archive=archive)


def manage_board(args):
    from tools.kanban_admin_tools import _action_fields, _reply
    action = args["action"]
    _action_fields(args, _FIELDS[action])
    if action == "list":
        return _reply(boards=kb.list_boards(include_archived=args.get("include_archived", False)), current=kb.get_current_board())
    if action == "import":
        from hermes_cli.kanban_transfer import import_board
        result = import_board(_require_text(args, "path"), slug=args.get("board"), activate=args.get("activate", False))
        return _reply(**result)
    slug = _require_text(args, "board")
    if action == "create":
        return _reply(board=kb.create_board(slug, **_metadata(args)))
    _check(kb.board_exists(slug), f"board {slug!r} does not exist")
    if action == "show":
        return _reply(board=kb.read_board_metadata(slug), current=kb.get_current_board())
    if action == "update":
        fields = _metadata(args)
        _check(fields, "update requires a metadata field")
        return _reply(board=kb.write_board_metadata(slug, **fields))
    if action == "stats":
        with _board(slug) as (_, conn):
            return _reply(stats=kb.board_stats(conn))
    if action == "switch":
        kb.set_current_board(slug)
        return _reply(current=kb.get_current_board())
    if action in {"archive", "delete"}:
        return _reply(result=_remove(slug, archive=action == "archive"), current=kb.get_current_board())
    from hermes_cli.kanban_transfer import export_board
    result = export_board(slug, _require_text(args, "path"),
        include_attachments=args.get("include_attachments", True), include_logs=args.get("include_logs", False))
    return _reply(path=result["archive"], **result)
