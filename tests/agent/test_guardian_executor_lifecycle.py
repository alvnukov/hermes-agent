"""Real executor retirement and nested RPC cannot inherit a parent ALLOW."""

import json
import threading
from types import SimpleNamespace
import pytest

from tests.agent.test_guardian_review import _setup
from tests.agent.test_tool_call_guardrail_runtime import _make_agent, _mock_tool_call
from tests.tools.test_guardian_approval_backend import _no_human


def _terminal_environment(monkeypatch, tmp_path):
    from tools import terminal_tool as terminal
    effects = []
    env = SimpleNamespace(execute=lambda *a, **kw: effects.append(a) or {"returncode": 0, "output": "ran"})
    plan = terminal._ExecPlan(config={}, env_type="local", effective_task_id="task", image="",
                              cwd=str(tmp_path), host_cwd=None, effective_timeout=1)
    monkeypatch.setattr(terminal, "_plan_execution", lambda *a, **kw: plan)
    monkeypatch.setattr(terminal, "_acquire_env", lambda *a: env)
    monkeypatch.setattr(terminal, "_pre_exec_block", lambda *a, **kw: None)
    monkeypatch.setattr(terminal, "_yield_kwargs", lambda *a, **kw: {})
    monkeypatch.setattr(terminal, "finalize_foreground_result", lambda **kw: json.dumps(kw["result"]))
    return terminal, effects


def test_delayed_allow_after_real_sequential_worker_timeout_never_executes(monkeypatch, tmp_path):
    review, origin, args, _ = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr("agent.agent_init.fetch_model_metadata", lambda *a, **kw: {})
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata", lambda *a, **kw: {})
    agent = _make_agent("terminal")
    agent._guardian_actions = origin._guardian_actions
    agent._credential_pool = origin._credential_pool
    agent._guardian_response_backend = "codex_guardian"
    _no_human(monkeypatch)
    terminal, effects = _terminal_environment(monkeypatch, tmp_path)
    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    class DelayedTransport:
        def review(self, *a, **kw):
            entered.set()
            assert release.wait(10)
            return SimpleNamespace(assessment=SimpleNamespace(outcome="allow"), usage={}, response_id="late")
    monkeypatch.setattr(review, "GuardianTransport", DelayedTransport)
    monkeypatch.setattr("agent.tool_executor._resolve_sequential_tool_timeout", lambda: 1.0)
    def dispatch(name, tool_args, task_id, **kwargs):
        try:
            return terminal.terminal_tool(task_id=task_id, **tool_args)
        finally:
            settled.set()
    monkeypatch.setattr("model_tools.handle_function_call", dispatch)
    call = _mock_tool_call("terminal", json.dumps(args), "T1")
    messages = []
    try:
        agent._execute_tool_calls_sequential(SimpleNamespace(tool_calls=[call]), messages, "task")
        assert entered.is_set(), "review did not begin before tool deadline"
        assert "timed out" in messages[0]["content"]
        assert effects == []
    finally:
        release.set()
        assert settled.wait(5)
    assert effects == []


def test_nested_rpc_manual_approval_does_not_consume_outer_receipt(monkeypatch, tmp_path):
    from tools import approval, code_kernel
    from tools.approval_guardian import consume_current_allow
    review, agent, args, calls = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(approval, "_presence", lambda callback=None: (lambda *a, **kw: "once", True, False, False))
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda command: False)
    monkeypatch.setattr(approval, "is_approved", lambda *a: False)
    monkeypatch.setattr(approval, "_tirith_scan", lambda command: {"action": "allow"})
    monkeypatch.setattr(approval, "_unattended_contexts", lambda: [])
    observations = []
    def dispatch(name, nested_args, **kw):
        observations.append(review.current_backend())
        result = approval.check_all_command_guards(nested_args["command"], "local")
        assert result.get("user_approved") is True
        assert consume_current_allow(nested_args["command"], cwd=str(tmp_path))
        return json.dumps(result)
    monkeypatch.setattr("model_tools.handle_function_call", dispatch)
    with review.bind_guardian_action(agent, "terminal", args, "T1"):
        assert review.review_current_action(args["command"], "script").outcome is review.ReviewOutcome.ALLOW
        authority = code_kernel.CellAuthority("task")
        result = json.loads(authority.dispatch("terminal", {"command": "python -c 'print(2)'"}))
        assert result["approved"] is True
        assert review.consume_current_allow(args["command"], cwd=str(tmp_path))
    assert observations == ["codex_guardian"]
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", ["deny", "timeout", "http_401", "http_403", "http_429", "http_500",
                                   "invalid_assessment", "capability_unavailable", "cancelled"])
def test_denial_or_review_error_never_reaches_real_terminal_executor(monkeypatch, tmp_path, outcome):
    from agent.guardian_protocol import GuardianProtocolError
    review, origin, args, _ = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr("agent.agent_init.fetch_model_metadata", lambda *a, **kw: {})
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata", lambda *a, **kw: {})
    agent = _make_agent("terminal")
    agent._credential_pool = origin._credential_pool
    agent._guardian_actions = origin._guardian_actions
    agent._guardian_response_backend = "codex_guardian"
    _no_human(monkeypatch)
    terminal, effects = _terminal_environment(monkeypatch, tmp_path)
    class Transport:
        def review(self, *a, **kw):
            if outcome != "deny":
                raise GuardianProtocolError(outcome)
            return SimpleNamespace(assessment=SimpleNamespace(outcome="deny"), usage={}, response_id="declined")
    monkeypatch.setattr(review, "GuardianTransport", Transport)
    monkeypatch.setattr("model_tools.handle_function_call",
                        lambda name, tool_args, task_id, **kwargs: terminal.terminal_tool(task_id=task_id, **tool_args))
    messages = []
    call = _mock_tool_call("terminal", json.dumps(args), "T1")
    agent._execute_tool_calls_sequential(SimpleNamespace(tool_calls=[call]), messages, "task")
    assert effects == []
    assert json.loads(messages[0]["content"])["status"] == "blocked"
