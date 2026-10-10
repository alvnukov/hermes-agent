"""Review readiness has a different phase contract from goal completion."""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("author", "reviewer"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("{}\n")
    with kbc.connect_closing() as conn:
        yield conn


@pytest.mark.parametrize("entry", ["tool", "cli"])
@pytest.mark.parametrize("verdict", ["done", "continue", "blocked", "transport"])
def test_review_gate_bootstraps_from_author_evidence(board, monkeypatch, entry, verdict):
    import agent.auxiliary_client as aux
    from agent.portal_tags import get_affinity_scope
    from hermes_cli import kanban
    from tools import kanban_tools
    from tools.registry import registry

    suffix = "Candidate ready_for_review=true. Independent review and review_requested are future outputs."
    tid = kb.create_task(board, title="Review candidate", body="Author criteria. " + "x" * 6500 + suffix, assignee="author", goal_mode=True)
    author = kb.claim_task(board, tid)
    assert author is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(author.current_run_id))
    monkeypatch.setenv("HERMES_PROFILE", "author")
    monkeypatch.setattr(aux, "get_text_auxiliary_client", lambda *a, **k: (object(), "test-model"))
    captured = []

    def transport(**kwargs):
        captured.append((kwargs, get_affinity_scope()))
        if verdict == "transport":
            raise RuntimeError("offline")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"verdict": verdict, "reason": "author evaluation"})))])

    monkeypatch.setattr(aux, "call_llm", transport)
    metadata = {"candidate_revision": "v4", "phase": "author", "ready_for_review": True}
    if entry == "tool":
        result = registry.dispatch("kanban_request_review", {"task_id": tid, "reviewer": "reviewer", "summary": "Author candidate prepared", "metadata": metadata})
        result = json.loads(result) if isinstance(result, str) else result
        allowed = "error" not in result
    else:
        rc = kanban._cmd_request_review(argparse.Namespace(task_id=tid, reviewer="reviewer", summary="Author candidate prepared", metadata=json.dumps(metadata), force=False))
        allowed = rc == 0
    assert allowed is (verdict in ("done", "transport"))
    assert len(captured) == 1
    kwargs, affinity = captured[0]
    prompt = kwargs["messages"][-1]["content"]
    assert suffix in prompt
    assert '"operation": "request_review"' in prompt
    assert '"effective_reviewer": "reviewer"' in prompt
    assert '"candidate_revision": "v4"' in prompt
    assert f'"author_run_id": {author.current_run_id}' in prompt
    assert affinity == f"kanban:{tid}"
    task = kb.get_task(board, tid)
    assert task is not None
    handoffs = [e for e in kb.list_events(board, tid) if e.kind == "review_requested"]
    if allowed:
        assert (task.status, task.assignee) == ("review", "reviewer") and len(handoffs) == 1
        assert kb.claim_review_task(board, tid) is not None
    else:
        assert task.status == "running" and not handoffs


@pytest.mark.parametrize("goal_length,allowed", [(32000, True), (32001, False)])
def test_review_gate_never_silently_truncates_goal(board, monkeypatch, goal_length, allowed):
    import agent.auxiliary_client as aux
    from tools.registry import registry
    from tools import kanban_tools  # register the real handler

    tid = kb.create_task(board, title="X", body="a" * (goal_length - 3), assignee="author", goal_mode=True)
    task = kb.claim_task(board, tid)
    assert task is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setattr(aux, "get_text_auxiliary_client", lambda *a, **k: (object(), "test-model"))
    prompts = []

    def transport(**kwargs):
        prompts.append(kwargs["messages"][-1]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"verdict":"done","reason":"ready"}'))])

    monkeypatch.setattr(aux, "call_llm", transport)
    raw = registry.dispatch("kanban_request_review", {"task_id": tid, "summary": "Prepared", "reviewer": "reviewer"})
    result = json.loads(raw) if isinstance(raw, str) else raw
    assert ("error" not in result) is allowed
    assert len(prompts) == (1 if allowed else 0)
    if allowed:
        assert "a" * (goal_length - 3) in prompts[0]
    else:
        assert "32000" in result["error"]
        current = kb.get_task(board, tid)
        assert current is not None and current.status == "running"


@pytest.mark.parametrize("entry", ["tool", "cli"])
@pytest.mark.parametrize("mutation", ["body", "run", "none"])
def test_judge_decision_is_bound_to_native_task_state(board, monkeypatch, entry, mutation):
    import agent.auxiliary_client as aux
    from hermes_cli import kanban
    from tools.registry import registry
    from tools import kanban_tools

    tid = kb.create_task(board, title="Candidate", body="v1", assignee="author", goal_mode=True)
    old = kb.claim_task(board, tid)
    assert old is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(old.current_run_id))
    monkeypatch.setattr(aux, "get_text_auxiliary_client", lambda *a, **k: (object(), "test"))

    def transport(**kwargs):
        if mutation == "body":
            assert kb.edit_task(board, tid, body="v2 changed requirements")
        elif mutation == "run":
            assert kb.block_task(board, tid, reason="Pause", kind="needs_input", expected_run_id=old.current_run_id)
            assert kb.unblock_task(board, tid)
            assert kb.claim_task(board, tid) is not None
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"verdict":"done","reason":"ready"}'))])

    monkeypatch.setattr(aux, "call_llm", transport)
    meta = {"candidate_revision": "v4", "phase": "author"}
    if entry == "tool":
        raw = registry.dispatch("kanban_request_review", {"task_id": tid, "summary": "Candidate", "reviewer": "reviewer", "metadata": meta})
        output = json.loads(raw) if isinstance(raw, str) else raw
        allowed = "error" not in output
    else:
        allowed = kanban._cmd_request_review(argparse.Namespace(task_id=tid, summary="Candidate", reviewer="reviewer", metadata=json.dumps(meta), force=False)) == 0
    assert allowed is (mutation == "none")
    task = kb.get_task(board, tid)
    assert task is not None
    if mutation != "none":
        assert task.status == "running"
        assert not [e for e in kb.list_events(board, tid) if e.kind == "review_requested"]
    else:
        run = kb.latest_run(board, tid)
        assert run is not None and run.metadata is not None and run.metadata["candidate_revision"] == "v4"
        native = run.metadata["_kanban_handoff"]
        assert native["schema"] == 1 and native["author_run_id"] == old.current_run_id
        assert native["effective_reviewer"] == "reviewer"


@pytest.mark.parametrize("cause", ["overflow", "reserved", "not_ready", "stale", "unknown_schema"])
def test_rejection_has_native_identity_and_cannot_be_spoofed(board, monkeypatch, cause):
    import agent.auxiliary_client as aux
    from tools import kanban_tools
    from tools.registry import registry

    tid = kb.create_task(board, title="Candidate", body="x" * 33000 if cause == "overflow" else "Criteria", assignee="author", goal_mode=True)
    author = kb.claim_task(board, tid)
    assert author is not None
    if cause == "unknown_schema":
        assert kb.request_review(board, tid, reviewer="reviewer", summary="v1", metadata={"_kanban_handoff": {"schema": 99}}, expected_run_id=author.current_run_id)
        assert kb.reopen_review_task(board, tid)
        author = kb.claim_task(board, tid)
        assert author is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(author.current_run_id))
    monkeypatch.setattr(aux, "get_text_auxiliary_client", lambda *a, **k: (object(), "test"))

    def transport(**kwargs):
        if cause == "stale":
            assert kb.block_task(board, tid, reason="Pause", kind="needs_input", expected_run_id=author.current_run_id)
            assert kb.unblock_task(board, tid)
            assert kb.claim_task(board, tid) is not None
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"verdict":"continue","reason":"author work missing"}'))])

    monkeypatch.setattr(aux, "call_llm", transport)
    meta = {"_kanban_handoff": {"schema": 99}} if cause == "reserved" else {}
    raw = registry.dispatch("kanban_request_review", {"task_id": tid, "summary": "Candidate", "reviewer": "reviewer", "metadata": meta})
    output = json.loads(raw) if isinstance(raw, str) else raw
    assert "error" in output
    rejections = [e for e in kb.list_events(board, tid) if e.kind == "goal_gate_rejected"]
    if cause in ("reserved", "stale"):
        assert not rejections
    else:
        assert len(rejections) == 1 and rejections[0].run_id == author.current_run_id
        event = rejections[0]
        assert event.payload is not None
        assert event.payload["gate_id"] == "kanban.review_readiness"
        assert event.payload["blockable"] is (cause in ("overflow", "unknown_schema"))
        assert str(event.id) in output["error"]
