"""Foreground terminal dispatch and retries after final execution authorization.

Facade-owned helpers are resolved at call time to preserve its public patch targets.
"""

from __future__ import annotations

import json
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from tools.terminal_tool import _ExecPlan


def _run_foreground(
    command: str, env: Any, plan: _ExecPlan, *,
    task_id: Optional[str], session_id: Optional[str], session_key: str,
    workdir: Optional[str], approval_note: Optional[str], clear_interrupt: bool,
    metered: bool = True,
) -> str:
    """Execute in the foreground with retry on transient errors, then finalize. ``metered``
    is False for Hermes' own control-plane commands (``_host_local``)."""
    from tools import terminal_tool as tt
    from tools.approval_guardian import consume_current_allow, get_approval_backend
    from hermes_cli.observability.shared_metrics_harness import record_terminal_outcome
    max_retries = 3
    env_type, eff, effective_timeout = plan.env_type, plan.effective_task_id, plan.effective_timeout
    legacy = get_approval_backend() == "legacy"
    if clear_interrupt and legacy:
        from tools.interrupt import clear_current_thread_interrupt
        clear_current_thread_interrupt()

    for retry_count in range(max_retries + 1):
        try:
            command_cwd = tt._resolve_command_cwd(
                workdir=workdir, default_cwd=plan.cwd, session_key=session_key, env_type=env_type,
                mounted_host=getattr(env, "host_cwd", None) or plan.host_cwd,
                env=env,
            )
            if not consume_current_allow(command, cwd=command_cwd):
                return tt._error_json("BLOCKED: Guardian authorization changed or was already used; command was not run.",
                                      status="blocked")
            # Clear an explicitly human-approved command only AFTER validating
            # its receipt; a tool timeout must not be erased before this check.
            if clear_interrupt and not legacy and retry_count == 0:
                from tools.interrupt import clear_current_thread_interrupt
                clear_current_thread_interrupt()
            # bounded_capture: model-facing output keeps a head/tail window
            # while streaming so a verbose command can't OOM the gateway;
            # internal env.execute() consumers stay unbounded.
            result = env.execute(
                command, timeout=effective_timeout, cwd=command_cwd, bounded_capture=True,
                **tt._yield_kwargs(command, env_type=env_type, cwd=command_cwd, effective_task_id=eff,
                                task_id=task_id, session_key=session_key),
            )
            break
        except Exception as e:  # health: allow BLE001 -- Backend plugins expose arbitrary exception types; Guardian logs only the class and never retries, legacy preserves its existing retry contract.
            # A backend exception (e.g. an SSH connect timeout) never reached an exit status, so it
            # is not a terminal outcome; Hermes' own deadline arrives as ``hermes_timed_out``.
            if "timeout" in str(e).lower():
                return tt._error_json(f"Command timed out after {effective_timeout} seconds", exit_code=124)
            if not legacy:
                tt.logger.warning("Guardian command execution failed (%s); authorization cannot be reused", type(e).__name__)
                return tt._error_json("Command execution failed; authorization cannot be reused.")
            # Retry on transient errors
            if retry_count < max_retries:
                wait_time = 2 ** (retry_count + 1)
                tt.logger.warning("Execution error, retrying in %ds (attempt %d/%d) - Command: %s - Error: %s: %s - Task: %s, Backend: %s",
                               wait_time, retry_count + 1, max_retries, tt._safe_command_preview(command), type(e).__name__, e, eff, env_type)
                tt.time.sleep(wait_time)
                continue
            tt.logger.error("Execution failed after %d retries - Command: %s - Error: %s: %s - Task: %s, Backend: %s",
                         max_retries, tt._safe_command_preview(command), type(e).__name__, e, eff, env_type)
            return tt._error_json(tt._redact_terminal_error_text(f"Command execution failed: {type(e).__name__}: {e}"))

    if result.get("yielded_session_id"):  # handed to the background: no exit status yet
        return json.dumps({
            "output": result.get("output", ""), "exit_code": None, "error": None,
            "status": "yielded_to_background", "session_id": result["yielded_session_id"],
            "pid": result.get("pid"), "notify_on_complete": True, "note": tt._YIELDED_NOTE,
        }, ensure_ascii=False)
    if metered:
        record_terminal_outcome(command, env_type, result)
    return tt.finalize_foreground_result(
        command=command, result=result, env=env, env_type=env_type, effective_task_id=eff,
        task_id=task_id, session_id=session_id, session_key=session_key, workdir=workdir,
        command_cwd=command_cwd, approval_note=approval_note,
    )
