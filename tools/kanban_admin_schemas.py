"""Opt-in management schemas; worker lifecycle tools remain in ``kanban``."""
from tools.kanban_tools_schemas import _schema, _prop


def _enum(values, description):
    return {"type": "string", "enum": values, "description": description}


TASK_FIELDS = {
    "task_id": _prop("string", "Existing task id; required for every action."),
    "title": _prop("string", "New nonempty title for edit."),
    "body": _prop("string", "New task description for edit."),
    "priority": _prop("integer", "Priority for edit; larger values dispatch first."),
    "result": _prop("string", "Backfill a completed card's result with edit."),
    "summary": _prop("string", "Handoff summary when editing a completed result."),
    "metadata": {"type": "object", "description": "Structured handoff facts for edit."},
    "assignee": _prop("string", "Profile for assign/reassign, or empty string to unassign; root profile for decompose."),
    "reclaim_first": _prop("boolean", "For reassign: stop the current worker before changing its profile."),
    "reason": _prop("string", "Audit reason for move, promote or reassign."),
    "expected_hold_id": _prop("string", "Current dispatch hold id for explicit operator release."),
    "status": _enum(["ready", "todo", "triage", "blocked", "scheduled", "review", "done"],
        "For move: desired state. Running can only be entered by the dispatcher. Terminal/review moves use lifecycle checks."),
    "parent_id": _prop("string", "For unlink: remove this parent dependency from task_id."),
    "model": _prop("string", "For model: model override; empty string clears it."),
    "provider": _prop("string", "Provider paired with model override."),
    "reasoning_effort": _prop("string", "For reasoning: effort override; empty string clears it."),
    "auto_promote": _prop("boolean", "For structured decompose: make dependency-free children ready (default true)."),
    "children": {"type": "array", "minItems": 1, "description": "For decompose: explicit child graph. Omit to use the configured auxiliary decomposer.",
        "items": {"type": "object", "properties": {
            "title": _prop("string", "Child title."), "body": _prop("string", "Child description."),
            "assignee": _prop("string", "Execution profile."),
            "parents": {"type": "array", "items": {"type": "integer", "minimum": 0}, "description": "Zero-based parent indices within this children array."},
            "workspace_kind": _enum(["scratch", "dir", "worktree"], "Workspace flavor."),
            "workspace_path": _prop("string", "Absolute workspace path when required."),
        }, "required": ["title", "assignee"], "additionalProperties": False}},
}

KANBAN_TASKS_SCHEMA = _schema("kanban_tasks",
    "Manage cards as an explicitly enabled Kanban administrator. Edit, route, move, archive, delete, "
    "remove dependencies or decompose triage work. Delete archives first to terminate any worker safely. "
    "State transitions preserve dependency, review and goal checks. All actions require task_id. "
    "For move to done/review include summary; use reason for blocked/scheduled. "
    "Specify/decompose without children may call the configured auxiliary model.",
    {"action": _enum(["edit", "assign", "reassign", "move", "promote", "reopen_review", "archive", "delete", "unlink", "model", "reasoning", "specify", "decompose"], "Operation to perform."), **TASK_FIELDS},
    ["action", "task_id"])

BOARD_FIELDS = {
    "name": _prop("string", "Board display name for create/update."),
    "description": _prop("string", "Board description for create/update."),
    "icon": _prop("string", "Board icon for create/update."),
    "color": _prop("string", "Board color for create/update."),
    "default_workdir": _prop("string", "Existing absolute working directory for create/update; empty clears it."),
    "project": _prop("string", "Project id/slug for create/update; empty clears it."),
    "include_archived": _prop("boolean", "For list: include boards marked archived."),
    "path": _prop("string", "Export destination or import archive path (on the agent host)."),
    "include_attachments": _prop("boolean", "For export: include attachment files (default true)."),
    "include_logs": _prop("boolean", "For export: include worker logs (default false)."),
    "activate": _prop("boolean", "For import: select the imported board (default false)."),
}
KANBAN_BOARDS_SCHEMA = _schema("kanban_boards",
    "Manage Kanban boards: list/show/stats, create/update, select the default for future calls, "
    "archive/delete, and portable export/import. Except list/import, pass an explicit board slug. "
    "Archive/delete are refused while the board owns an active worker; reclaim it first. "
    "Import creates a new board and resolves slug collisions; default cannot be removed.",
    {"action": _enum(["list", "show", "stats", "create", "update", "switch", "archive", "delete", "export", "import"], "Operation to perform."), **BOARD_FIELDS}, ["action"])

KANBAN_WORKERS_SCHEMA = _schema("kanban_workers",
    "Inspect and control Kanban execution: list active workers, list a card's runs, show one run, "
    "read a bounded worker log, reclaim a card or terminate its current run. Termination verifies "
    "run ownership and uses the existing PID fingerprint and claim recovery rules.",
    {"action": _enum(["list", "runs", "show", "log", "reclaim", "terminate"], "Operation to perform."),
     "task_id": _prop("string", "Required for runs, log and reclaim."),
     "run_id": {"type": "integer", "minimum": 1, "description": "Required for show and terminate."},
     "reason": _prop("string", "Audit reason for reclaim/terminate."),
     "tail_bytes": {"type": "integer", "minimum": 1, "maximum": 65536, "description": "Log byte cap (default 16384)."}}, ["action"])

KANBAN_DISPATCH_SCHEMA = _schema("kanban_dispatch",
    "Run one Kanban dispatcher tick for a board, or preview with dry_run=true. This may spawn "
    "assigned workers. Uses configured host/profile concurrency caps, failure limits and default assignee.",
    {"dry_run": _prop("boolean", "Preview only (default false)."),
     "max_spawn": {"type": "integer", "minimum": 1, "description": "Maximum new workers this tick; defaults to configuration."}}, [])

KANBAN_ATTACHMENT_MANAGE_SCHEMA = _schema("kanban_attachment_manage",
    "Show one attachment's metadata and host path, or remove its stored file and board record.",
    {"action": _enum(["show", "delete"], "Operation to perform."),
     "attachment_id": {"type": "integer", "minimum": 1, "description": "Existing attachment id."}}, ["action", "attachment_id"])

KANBAN_NOTIFICATIONS_SCHEMA = _schema("kanban_notifications",
    "List, subscribe or unsubscribe Kanban task notifications owned by the calling profile. "
    "Subscribe/unsubscribe require task_id, platform and chat_id; thread_id is optional. "
    "New subscriptions start at the current event, without replaying historical notifications.",
    {"action": _enum(["list", "subscribe", "unsubscribe"], "Operation to perform."),
     "task_id": _prop("string", "Task id; optional for list."),
     "platform": _prop("string", "Messaging platform for subscribe/unsubscribe."),
     "chat_id": _prop("string", "Destination chat id for subscribe/unsubscribe."),
     "thread_id": _prop("string", "Optional destination thread."),
     "delivery_mode": _enum(["notify", "wake", "notify+wake"], "For subscribe: notification delivery mode.")}, ["action"])

ORCHESTRATION_FIELDS = {
    "orchestrator_profile": _prop("string", "Profile for triage/decomposition, or empty to clear."),
    "default_assignee": _prop("string", "Default execution profile, or empty to clear."),
    **{k: _prop("boolean", desc) for k, desc in {
        "auto_decompose": "Automatically decompose triage tasks.",
        "auto_promote_children": "Automatically promote decomposition children.",
        "dispatch_in_gateway": "Run the dispatcher in the gateway (restart may be required).",
        "review_dispatch": "Allow autonomous reviewer dispatch.",
    }.items()},
    **{k: {"type": "integer", "minimum": 1, "description": desc} for k, desc in {
        "max_spawn": "Maximum workers spawned per tick.",
        "max_in_progress": "Host concurrency cap.",
        "max_in_progress_per_profile": "Per-assignee concurrency cap.",
        "failure_limit": "Consecutive failures before blocking a task.",
    }.items()},
}
KANBAN_ORCHESTRATION_SCHEMA = _schema("kanban_orchestration",
    "Read or update Kanban orchestration settings in the calling profile. Unspecified settings "
    "are preserved. Update cannot grant tool access or modify other configuration sections. "
    "Gateway dispatcher settings take effect when its configuration is reloaded/restarted.",
    {"action": _enum(["show", "update"], "Operation to perform."),
     "settings": {"type": "object", "properties": ORCHESTRATION_FIELDS, "additionalProperties": False,
                  "description": "Only fields to change; required for update."}}, ["action"])

SCHEMAS = (KANBAN_TASKS_SCHEMA, KANBAN_BOARDS_SCHEMA, KANBAN_WORKERS_SCHEMA,
           KANBAN_DISPATCH_SCHEMA, KANBAN_ATTACHMENT_MANAGE_SCHEMA,
           KANBAN_NOTIFICATIONS_SCHEMA, KANBAN_ORCHESTRATION_SCHEMA)
