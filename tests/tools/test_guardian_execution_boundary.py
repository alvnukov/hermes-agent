"""Execution checks receipt after CWD resolution and before shell or kernel effects."""

from types import ModuleType, SimpleNamespace
import json
import sys

import pytest


@pytest.fixture
def invalid_receipt(monkeypatch):
    module = ModuleType("agent.guardian_review")
    module.consume_current_allow = lambda command, **kwargs: False
    monkeypatch.setitem(sys.modules, "agent.guardian_review", module)
    return module


def test_stale_receipt_prevents_foreground_execute(tmp_path, monkeypatch, invalid_receipt):
    from tools import terminal_tool as tt
    effects = []
    env = SimpleNamespace(execute=lambda *a, **kw: effects.append(a) or {"returncode": 0, "output": "ran"})
    plan = SimpleNamespace(env_type="local", effective_task_id="test", effective_timeout=1,
                           cwd=str(tmp_path), host_cwd=None)
    monkeypatch.setattr(tt, "_yield_kwargs", lambda *a, **kw: {})
    result = json.loads(tt._run_foreground(
        "echo test", env, plan, task_id="test", session_id="test", session_key="test",
        workdir=str(tmp_path), approval_note=None, clear_interrupt=False, metered=False))
    assert effects == []
    assert result["status"] == "blocked"


def test_stale_receipt_prevents_background_spawn(tmp_path, monkeypatch, invalid_receipt):
    from tools import terminal_tool_background as background
    effects = []
    monkeypatch.setattr(background, "_spawn", lambda *a, **kw: effects.append(kw) or SimpleNamespace(id="spawn", pid=1))
    result = json.loads(background.spawn_background_process(
        command="echo test", env=SimpleNamespace(), env_type="local", effective_task_id="test", task_id="test",
        session_key="test", workdir=str(tmp_path), cwd=str(tmp_path), effective_pty=False,
        notify_on_complete=False, watch_patterns=None, approval_note=None, pty_disabled_reason=None))
    assert effects == []
    assert result["status"] == "blocked"


def test_stale_receipt_prevents_local_python_kernel(tmp_path, monkeypatch, invalid_receipt):
    from tools import code_kernel
    import threading
    effects = []
    kernel = SimpleNamespace(proc=SimpleNamespace(stdin=SimpleNamespace(write=lambda data: effects.append(data))),
                             lock=threading.Lock(), execution_count=0)
    result = json.loads(code_kernel._run_cell(
        kernel, ("test",), "print(1)", task_id="test", child_python="python", child_cwd=str(tmp_path),
        sandbox_tools=frozenset(), timeout=1, max_tool_calls=1, is_interrupted=lambda: False,
        exec_start=0, state_reset=False))
    assert effects == []
    assert "Guardian authorization" in result["error"]


def test_foreground_consumes_with_actual_directory(tmp_path, monkeypatch, invalid_receipt):
    from tools import terminal_tool as tt
    calls = []
    invalid_receipt.consume_current_allow = lambda command, **kw: calls.append((command, kw)) or False
    env = SimpleNamespace(execute=lambda *a, **kw: pytest.fail("no side effects"))
    plan = SimpleNamespace(env_type="local", effective_task_id="test", effective_timeout=1,
                           cwd=str(tmp_path), host_cwd=None)
    tt._run_foreground("echo test", env, plan, task_id="test", session_id="test", session_key="test",
                       workdir=str(tmp_path), approval_note=None, clear_interrupt=False, metered=False)
    assert calls == [("echo test", {"cwd": str(tmp_path), "kernel_execution_count": None})]


def test_legacy_backend_preserves_arbitrary_transient_backend_retries(tmp_path, monkeypatch, invalid_receipt):
    from tools import terminal_tool as tt
    invalid_receipt.consume_current_allow = lambda command, **kwargs: True
    invalid_receipt.current_backend = lambda: "legacy"
    attempts = []
    class BackendError(Exception):
        pass
    def execute(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            raise BackendError("transient")
        return {"returncode": 0, "output": "ok"}
    monkeypatch.setattr(tt, "_yield_kwargs", lambda *a, **kw: {})
    monkeypatch.setattr(tt.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(tt, "finalize_foreground_result", lambda **kw: json.dumps(kw["result"]))
    plan = SimpleNamespace(env_type="local", effective_task_id="test", effective_timeout=1,
                           cwd=str(tmp_path), host_cwd=None)
    result = json.loads(tt._run_foreground(
        "echo test", SimpleNamespace(execute=execute), plan, task_id="test", session_id="test",
        session_key="test", workdir=str(tmp_path), approval_note=None, clear_interrupt=False, metered=False))
    assert result["output"] == "ok"
    assert len(attempts) == 2


def test_invalid_receipt_is_checked_before_clearing_worker_interrupt(tmp_path, monkeypatch, invalid_receipt):
    from tools import terminal_tool as tt
    cleared = []
    monkeypatch.setattr("tools.interrupt.clear_current_thread_interrupt", lambda: cleared.append(True))
    plan = SimpleNamespace(env_type="local", effective_task_id="test", effective_timeout=1,
                           cwd=str(tmp_path), host_cwd=None)
    result = json.loads(tt._run_foreground(
        "echo test", SimpleNamespace(execute=lambda *a, **kw: pytest.fail("no side effects")), plan,
        task_id="test", session_id="test", session_key="test", workdir=str(tmp_path),
        approval_note=None, clear_interrupt=True, metered=False))
    assert result["status"] == "blocked"
    assert cleared == []
