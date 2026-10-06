"""An account-bound review, a single-use receipt, and safe manual escalation."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
import logging
from pathlib import Path
import threading
import time
from typing import Any

from agent.guardian_action import ActionSnapshot, snapshot_action
from agent.guardian_credentials import PinnedCredentialError, resolve_pinned
from agent.guardian_protocol import GuardianProtocolError
from agent.guardian_provenance import ReviewBinding, canonical, current_policy
from agent.guardian_transport import GuardianTransport

logger = logging.getLogger(__name__)


class ReviewOutcome(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class ReviewResult:
    outcome: ReviewOutcome
    code: str
    reason: str


@dataclass
class _ActionContext:
    agent: Any
    tool: str
    args: dict = field(repr=False)
    call_id: str
    binding: ReviewBinding | None = field(repr=False)
    backend: str
    task_id: str | None = None
    snapshot: ActionSnapshot | None = field(default=None, repr=False)
    result: ReviewResult | None = None
    command: str | None = field(default=None, repr=False)
    consumed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    worker_tid: int = field(default_factory=threading.get_ident)
    turn_id: str = ""
    request_id: str = ""
    account_alias: str = field(default="unbound", repr=False)

    def cancelled(self) -> bool:
        from tools.interrupt import is_interrupted, is_thread_interrupted
        return (bool(getattr(self.agent, "_interrupt_requested", False)) or is_interrupted()
                or is_thread_interrupted(self.worker_tid)
                or str(getattr(self.agent, "_current_turn_id", "") or "") != self.turn_id
                or str(getattr(self.agent, "_current_api_request_id", "") or "") != self.request_id)


_context: ContextVar[_ActionContext | None] = ContextVar("guardian_action", default=None)


def backend_for_call(agent, call_id: str) -> str:
    from hermes_cli.guardian_config import approval_backend
    from tools.approval_context import _get_approval_config
    binding = (getattr(agent, "_guardian_actions", None) or {}).get(call_id)
    if binding is not None:
        return "codex_guardian" if binding.attempt.identity.guardian_enabled else "legacy"
    selected = getattr(agent, "_guardian_response_backend", None)
    return selected if selected is not None else approval_backend(_get_approval_config())


def current_backend() -> str:
    context = _context.get()
    if context is not None:
        return context.backend
    from hermes_cli.guardian_config import approval_backend
    from tools.approval_context import _get_approval_config
    return approval_backend(_get_approval_config())


@contextmanager
def bind_guardian_action(agent, name: str, args: dict, call_id: str, *, task_id: str | None = None):
    binding = (getattr(agent, "_guardian_actions", None) or {}).get(call_id)
    context = _ActionContext(agent, name, args, call_id, binding, backend_for_call(agent, call_id), task_id)
    context.turn_id = str(getattr(agent, "_current_turn_id", "") or "")
    context.request_id = str(getattr(agent, "_current_api_request_id", "") or "")
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


@contextmanager
def bind_nested_guardian_action(name: str, args: dict):
    """An inner RPC action has no parent response binding and must use manual approval."""
    parent = _context.get()
    if parent is None:
        yield
        return
    nested = _ActionContext(parent.agent, name, args, "nested", None, parent.backend, parent.task_id,
                            turn_id=parent.turn_id, request_id=parent.request_id)
    token = _context.set(nested)
    try:
        yield
    finally:
        _context.reset(token)


def revoke_current_allow() -> None:
    context = _context.get()
    if context is not None:
        context.result = ReviewResult(ReviewOutcome.ESCALATE, "action_changed", "Manual approval is required.")


def _action_environment(context: _ActionContext) -> tuple[str, str]:
    from tools.terminal_tool import _get_env_config, get_session_cwd
    from tools.terminal_tool_lifecycle import get_active_env
    from tools.approval_context import get_current_session_key
    config = _get_env_config()
    env = get_active_env(context.task_id or "default")
    cwd = (context.args.get("workdir") or get_session_cwd(get_current_session_key())
           or get_session_cwd(context.task_id) or getattr(env, "cwd", None) or config["cwd"])
    if context.tool == "execute_code":
        from tools.code_execution_tool import _get_execution_mode
        if _get_execution_mode() != "project":
            raise GuardianProtocolError("environment_unavailable")
        from tools.code_execution_env import _resolve_child_cwd
        from tools.code_kernel import _KERNELS, _REGISTRY, _resolve_owner
        cwd = _resolve_child_cwd("project", "", task_id=context.task_id or "")
        with _REGISTRY.lock:
            owner = _resolve_owner(context.task_id or "") or ""
            reused = any(kernel.owner == owner and kernel.execution_count > 0 for kernel in _KERNELS.values())
        if reused and context.args.get("reset") is not True:
            raise GuardianProtocolError("python_state_unavailable")
    return str(Path(cwd).resolve()), config["env_type"]


def _expected_command(context: _ActionContext) -> str | None:
    if context.tool == "terminal":
        return context.args.get("command")
    if context.tool == "execute_code":
        return "execute_code <<'PY'\n" + context.args.get("code", "") + "\nPY"
    return None


def _prove_action(context: _ActionContext, command: str) -> ReviewBinding:
    binding = context.binding
    if (binding is None or binding.attempt.identity is None or binding.tool != context.tool or binding.tool_call_id != context.call_id
            or binding.args_json != canonical(context.args) or _expected_command(context) != command):
        raise GuardianProtocolError("action_binding_unavailable")
    if context.cancelled():
        raise GuardianProtocolError("cancelled")
    if current_policy()[1] != binding.attempt.policy_version:
        raise GuardianProtocolError("policy_changed")
    return binding


def _bounded_context(binding: ReviewBinding, descriptor: dict, settings: dict) -> tuple[str, dict]:
    from agent.redact import redact_sensitive_text
    payload = canonical({"transcript": binding.transcript_json, "action": descriptor, "policy": binding.policy})
    if len(payload.encode()) > settings["max_context_bytes"]:
        raise GuardianProtocolError("context_too_large")
    # If redaction would erase potentially decisive facts, do not auto-approve a changed action.
    if redact_sensitive_text(payload, force=True) != payload:
        raise GuardianProtocolError("sensitive_context")
    return binding.transcript_json, descriptor


def _record_usage(context: _ActionContext, response_id: str | None, counts: dict) -> None:
    if context.binding is None:
        return
    usage = getattr(context.agent, "_guardian_usage", None)
    if usage is None:
        context.agent._guardian_usage = usage = []
    correlation = (context.binding.attempt.attempt_id, context.call_id, response_id)
    if any((row["attempt_id"], row["tool_call_id"], row["response_id"]) == correlation for row in usage):
        return
    usage.append({"attempt_id": correlation[0], "tool_call_id": context.call_id,
                  "response_id": response_id, "usage": counts})
    logger.info("Guardian review usage account=%s attempt=%s tokens=%s", context.account_alias,
                correlation[0], canonical(counts))


def _account_alias(entry, identity) -> str:
    from agent.redact import redact_sensitive_text
    alias = str(getattr(entry, "label", "") or identity.entry_id)
    return "".join(character if character.isprintable() else " " for character in
                   redact_sensitive_text(alias, force=True))[:80]


def _review(context: _ActionContext, command: str) -> ReviewResult:
    from hermes_cli.guardian_config import guardian_settings
    from tools.approval_context import _get_approval_config
    settings = guardian_settings(_get_approval_config())
    started = time.monotonic()
    deadline = started + settings["timeout"]
    binding = _prove_action(context, command)
    identity = binding.attempt.identity
    pool = getattr(context.agent, "_credential_pool", None)
    if identity is None or pool is None:
        raise GuardianProtocolError("identity_unavailable")
    entry = resolve_pinned(pool, identity)
    context.account_alias = _account_alias(entry, identity)
    cwd, environment = _action_environment(context)
    context.snapshot = snapshot_action(context.tool, context.args, cwd, environment)
    transcript, descriptor = _bounded_context(binding, context.snapshot.descriptor(context.args), settings)
    response = GuardianTransport().review(identity, entry, parent_response_id=binding.parent_response_id,
                                          parent_model=binding.attempt.parent_model, transcript=transcript,
                                          action=descriptor, policy=binding.policy, config=settings,
                                          cancelled=context.cancelled, deadline=deadline,
                                          on_usage=lambda response_id, usage: _record_usage(context, response_id, usage))
    _record_usage(context, response.response_id, response.usage)
    resolve_pinned(pool, identity, refresh=False)
    _prove_action(context, command)
    if _action_environment(context) != (context.snapshot.cwd, context.snapshot.env_type):
        raise GuardianProtocolError("environment_changed")
    if time.monotonic() >= deadline:
        raise GuardianProtocolError("timeout")
    if not context.snapshot.unchanged(context.args):
        raise GuardianProtocolError("action_state_changed")
    # Separate reviewer usage has no inferred currency value and never changes main usage totals.
    outcome = ReviewOutcome.ALLOW if response.assessment.outcome == "allow" else ReviewOutcome.DENY
    return ReviewResult(outcome, "guardian_verdict", "Guardian approved this action." if outcome is ReviewOutcome.ALLOW
                        else "Guardian declined this action; explicit human approval is required.")


def review_current_action(command: str, description: str) -> ReviewResult:
    context = _context.get()
    if context is None:
        return ReviewResult(ReviewOutcome.ESCALATE, "action_binding_unavailable", "Guardian requires manual approval.")
    with context.lock:
        if context.result is not None:
            return context.result
        context.command = command
        started = time.monotonic()
        try:
            result = _review(context, command)
        except (GuardianProtocolError, PinnedCredentialError) as error:
            result = ReviewResult(ReviewOutcome.ESCALATE, error.code, "Guardian unavailable; manual approval is required.")
        except (ValueError, TypeError, KeyError, OSError, TimeoutError, AttributeError):
            result = ReviewResult(ReviewOutcome.ESCALATE, "review_unavailable", "Guardian unavailable; manual approval is required.")
        context.result = result
        logger.info("Guardian account=%s outcome=%s code=%s latency_ms=%d", context.account_alias,
                    result.outcome.value, result.code, int((time.monotonic() - started) * 1000))
        return result


def _validate(context: _ActionContext, command: str, cwd: str | None = None) -> bool:
    if (context.result is None or context.result.outcome is not ReviewOutcome.ALLOW
            or context.consumed or command != context.command or context.snapshot is None):
        return False
    try:
        binding = _prove_action(context, command)
        pool = getattr(context.agent, "_credential_pool", None)
        if pool is None or binding.attempt.identity is None:
            return False
        resolve_pinned(pool, binding.attempt.identity, refresh=False)
        if _action_environment(context) != (context.snapshot.cwd, context.snapshot.env_type):
            return False
        return context.snapshot.unchanged(context.args, cwd)
    except (GuardianProtocolError, PinnedCredentialError, ValueError, TypeError, OSError, AttributeError):
        return False


def validate_current_action(command: str) -> bool:
    context = _context.get()
    return context is not None and _validate(context, command)


def consume_current_allow(command: str, *, cwd: str | None = None,
                          kernel_execution_count: int | None = None) -> bool:
    context = _context.get()
    if context is None or context.result is None or context.result.outcome is not ReviewOutcome.ALLOW:
        return True
    with context.lock:
        fresh_kernel = context.tool != "execute_code" or kernel_execution_count == 0
        valid = fresh_kernel and _validate(context, command, cwd)
        context.consumed = True
        return valid
