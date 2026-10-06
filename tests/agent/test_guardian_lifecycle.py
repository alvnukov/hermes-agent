"""Physical attempts and concurrent account-bound HTTP reviews do not mix ancestry."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import OpenAI
import pytest

from tests.agent.test_guardian_credentials import BASE_URL, _jwt, _pool, _row, _save
from tests.agent.test_codex_first_event_timing import _agent


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _calls(call_id, args):
    return SimpleNamespace(tool_calls=[SimpleNamespace(id=call_id, function=SimpleNamespace(
        name="terminal", arguments=json.dumps(args)))])


def _events(parent, call_id, args):
    if parent:
        yield {"type": "response.created", "response": {"id": parent}}
    yield {"type": "response.output_item.done", "item": {
        "type": "function_call", "id": "fc-" + call_id, "call_id": call_id,
        "name": "terminal", "arguments": json.dumps(args)}}
    yield {"type": "response.completed", "response": {"status": "completed"}}


def _config(monkeypatch):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"backend": "legacy", "mode": "smart"})


def test_parallel_a_r1_t1_b_r2_t2_ignore_active_account(home, monkeypatch, tmp_path):
    from agent import guardian_review as review
    from agent.guardian_provenance import prepare_main_request, bind_response_actions
    from agent.guardian_transport import GuardianTransport
    from agent.codex_headers import codex_cloudflare_headers
    from agent.codex_runtime import _consume_codex_event_stream
    _config(monkeypatch)
    rows = [_row("a", label="same alias"), _row("b", label="same alias",
            access_token=_jwt("workspace-b", "owner-b"), identity_generation="login-b")]
    _save(home, rows)
    pool = _pool(rows)
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"slug": "codex-auto-review",
                "supported_reasoning_levels": [{"effort": "low"}], "default_reasoning_level": "low"}]})
        events = [{"type": "response.created", "response": {"id": "review-" + request.headers["chatgpt-account-id"]}},
                  {"type": "response.output_text.delta", "delta": '{"outcome":"allow"}'},
                  {"type": "response.completed", "response": {"status": "completed"}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                             text="".join("data: " + json.dumps(event) + "\n\n" for event in events))
    def factory(identity, entry, remaining):
        return OpenAI(api_key=entry.runtime_api_key, base_url=identity.base_url, max_retries=0,
                      default_headers=codex_cloudflare_headers(entry.runtime_api_key),
                      http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(review, "GuardianTransport", lambda: GuardianTransport(client_factory=factory))
    monkeypatch.setattr(review, "_action_environment", lambda _: (str(tmp_path), "local"))
    origins = []
    args = {"command": "python -c 'print(1)'"}
    for index, row in enumerate(rows, 1):
        agent = SimpleNamespace(_credential_pool=pool, _interrupt_requested=False, session_id=f"S{index}")
        client = SimpleNamespace(api_key=row["access_token"], base_url=BASE_URL)
        _, attempt = prepare_main_request(agent, client, {"model": "main"})
        response = _consume_codex_event_stream(_events(f"R{index}", f"T{index}", args), model="main")
        response._guardian_attempt = attempt
        bind_response_actions(agent, response, _calls(f"T{index}", args), [{"role": "user", "content": "Print one"}])
        origins.append((agent, f"T{index}"))
    pool._current_id = "b"
    def run(origin):
        agent, call_id = origin
        with review.bind_guardian_action(agent, "terminal", dict(args), call_id):
            assert review.review_current_action(args["command"], "script").outcome is review.ReviewOutcome.ALLOW
            assert review.consume_current_allow(args["command"])
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(run, origins))
    posts = [request for request in requests if request.method == "POST"]
    assert {(request.headers["chatgpt-account-id"], json.loads(request.content)["client_metadata"]["parent_response_id"])
            for request in posts} == {("workspace-a", "R1"), ("workspace-b", "R2")}
    assert pool._current_id == "b"


@pytest.mark.parametrize("early_id", [None, "old-response"])
@pytest.mark.parametrize("new_id", [None, "new-response"])
def test_stream_retry_cannot_reuse_old_id_or_account(home, monkeypatch, early_id, new_id):
    from agent.codex_runtime import run_codex_stream
    from agent.guardian_provenance import bind_response_actions
    _config(monkeypatch)
    rows = [_row("a"), _row("b", access_token=_jwt("workspace-b", "owner-b"))]
    _save(home, rows)
    agent = _agent()
    agent._credential_pool = _pool(rows)
    args = {"command": "pwd"}
    client = SimpleNamespace(api_key=rows[0]["access_token"], base_url=BASE_URL)
    attempts = []
    def create(**kwargs):
        attempts.append(client.api_key)
        if len(attempts) == 1:
            def failed():
                if early_id:
                    yield {"type": "response.created", "response": {"id": early_id}}
                client.api_key = rows[1]["access_token"]
                raise httpx.ReadError("synthetic broken stream")
                yield
            return failed()
        return _events(new_id, "new-call", args)
    client.responses = SimpleNamespace(create=create)
    response = run_codex_stream(agent, {"model": "main", "input": "synthetic"}, client=client)
    bind_response_actions(agent, response, _calls("new-call", args), [{"role": "user", "content": "pwd"}])
    assert response._guardian_attempt.identity.entry_id == "b"
    assert response.id == new_id
    assert agent._guardian_response_backend == "codex_guardian"
    if new_id:
        assert agent._guardian_actions["new-call"].parent_response_id == new_id
    else:
        assert agent._guardian_actions == {}


def test_persisted_checkbox_is_not_lost_when_identity_resolution_fails(home, monkeypatch):
    from agent.guardian_provenance import prepare_main_request
    _config(monkeypatch)
    rows = [_row(guardian_enabled=False)]
    _save(home, rows)
    pool = _pool(rows)
    rows[0].update(guardian_enabled=True, enabled=False)
    _save(home, rows)
    agent = SimpleNamespace(_credential_pool=pool)
    client = SimpleNamespace(api_key=rows[0]["access_token"], base_url=BASE_URL)
    request = {"model": "main"}
    actual, attempt = prepare_main_request(agent, client, request)
    assert actual is request
    assert attempt.guardian_requested is True and attempt.identity is None
