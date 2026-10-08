"""Profile-opted-in management tools, with execution-time authorization.

The registry deliberately does not enforce check_fn at dispatch time. These
privileged handlers must check both the saved profile grant and the calling
session's selection, including direct and Tool Search calls.
"""
from __future__ import annotations

import functools
import json
import os
from dataclasses import asdict, is_dataclass
from typing import Any

from hermes_cli.config import load_config
from tools.kanban_admin_schemas import SCHEMAS
from tools.kanban_toolset_context import kanban_admin_requested, scoped_kanban_toolset_selection
from agent.delegation_context import is_delegated_child_process_context
from tools.kanban_tools import _check, _kanban_handler, _redact
from tools.registry import no_cache_check_fn, registry


def _profile_admin_enabled() -> bool:
    from hermes_cli.tools_config import _get_platform_tools
    config = load_config()
    platforms = config.get("platform_toolsets") or {}
    return any("kanban_admin" in _get_platform_tools(
        config, platform, include_default_mcp_servers=False)
        for platform in {"cli", *platforms})


@no_cache_check_fn
def _check_admin_mode() -> bool:
    if os.environ.get("HERMES_KANBAN_TASK") or is_delegated_child_process_context():
        return False
    if kanban_admin_requested() is False:
        return False
    return _profile_admin_enabled()


def _validate(value: Any, schema: dict[str, Any], path="arguments"):
    """Validate before any write, including nested settings/child graph fields."""
    kinds = {"string": str, "boolean": bool, "integer": int, "object": dict, "array": list}
    kind = schema.get("type")
    if kind:
        _check(type(value) is kinds[kind], f"{path} must be {kind}")
    if "enum" in schema:
        _check(value in schema["enum"], f"{path} must be one of {schema['enum']}")
    if kind == "integer":
        _check(value >= schema.get("minimum", value), f"{path} is below its minimum")
        _check(value <= schema.get("maximum", value), f"{path} exceeds its maximum")
    if kind == "object" and "properties" in schema:
        props = schema["properties"]
        _check(set(schema.get("required", ())) <= value.keys(), f"{path} is missing required fields")
        if schema.get("additionalProperties") is False:
            _check(not (value.keys() - props.keys()), f"{path} has unknown fields: {sorted(value.keys() - props.keys())}")
        for key, item in value.items():
            if key in props:
                _validate(item, props[key], f"{path}.{key}")
    if kind == "array":
        _check(len(value) >= schema.get("minItems", 0), f"{path} must not be empty")
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{index}]")


def _reply(**fields):
    return _redact(json.dumps({"ok": True, **fields}, ensure_ascii=False,
        default=lambda value: asdict(value) if is_dataclass(value) and not isinstance(value, type) else str(value)))


def _action_fields(args, fields):
    """Reject misplaced fields rather than silently discard an operator's intent."""
    extra = args.keys() - {"action", "board"} - set(fields)
    _check(not extra, f"{args.get('action')}: unsupported fields: {sorted(extra)}. Nothing changed.")


def _admin_handler(schema):
    def decorate(fn):
        @_kanban_handler(schema["name"])
        @functools.wraps(fn)
        def checked(args, **kw):
            selection = kw.get("enabled_toolsets")
            # Direct registry calls have no session selection: only the saved
            # profile grant can authorize those. Model calls carry their scope.
            if "enabled_toolsets" in kw:
                from model_tools import _select_tool_names
                with scoped_kanban_toolset_selection(selection):
                    _check(_check_admin_mode(), "kanban_admin is not enabled for this profile/session, or caller is a worker/child")
                _check(schema["name"] in _select_tool_names(selection, kw.get("disabled_toolsets"), True),
                       "kanban_admin is disabled in this session")
            else:
                _check(_check_admin_mode(), "kanban_admin is not enabled for this profile, or caller is a worker/child")
            _validate(args, schema["parameters"])
            return fn(args)
        return checked
    return decorate


def _tasks(args, **kw):
    from tools.kanban_admin_tasks import manage_task
    return manage_task(args)


def _boards(args, **kw):
    from tools.kanban_admin_boards import manage_board
    return manage_board(args)


def _workers(args, **kw):
    from tools.kanban_admin_operations import manage_workers
    return manage_workers(args)


def _dispatch(args, **kw):
    from tools.kanban_admin_operations import dispatch
    return dispatch(args)


def _attachments(args, **kw):
    from tools.kanban_admin_operations import manage_attachment
    return manage_attachment(args)


def _notifications(args, **kw):
    from tools.kanban_admin_operations import manage_notifications
    return manage_notifications(args)


def _orchestration(args, **kw):
    from tools.kanban_admin_operations import manage_orchestration
    return manage_orchestration(args)


for _schema, _handler in zip(SCHEMAS, (_tasks, _boards, _workers, _dispatch, _attachments, _notifications, _orchestration)):
    registry.register(name=_schema["name"], toolset="kanban_admin", schema=_schema,
                      handler=_admin_handler(_schema)(_handler), emoji="📌", check_fn=_check_admin_mode)
