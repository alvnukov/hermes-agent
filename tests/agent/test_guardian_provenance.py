"""Guardian provenance belongs to a completed physical attempt, never agent globals."""

from types import SimpleNamespace

from agent.codex_runtime import _consume_codex_event_stream
import pytest


def _guardian(monkeypatch, enabled=True):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {
        "backend": "codex_guardian" if enabled else "legacy", "mode": "smart", "smart_policy": ""})


@pytest.mark.parametrize("enabled", [True, False])
def test_main_metadata_is_local_and_reserved(monkeypatch, enabled):
    from agent import guardian_provenance as provenance
    _guardian(monkeypatch, enabled)
    identity = SimpleNamespace(entry_id="A", base_url="https://chatgpt.com/backend-api/codex", guardian_enabled=enabled)
    monkeypatch.setattr(provenance, "capture_identity", lambda *a: identity)
    agent = SimpleNamespace(_credential_pool=object(), session_id="S1", _current_turn_id="U1")
    client = SimpleNamespace(base_url=identity.base_url, api_key="fake-token")
    request = {"model": "primary", "input": [], "extra_body": {
        "client_metadata": {"custom": "preserved", "parent_response_id": "forged"}},
        "extra_headers": {"x-codex-guardian": "reviewer", "Custom-Header": "value"}}
    kwargs, attempt = provenance.prepare_main_request(agent, client, request)
    assert request["extra_body"]["client_metadata"]["parent_response_id"] == "forged"
    if enabled:
        assert kwargs["extra_body"]["client_metadata"] == {"custom": "preserved", "guardian_credits_requested": "true"}
        assert "x-codex-guardian" not in kwargs["extra_headers"]
        assert kwargs["extra_headers"]["Custom-Header"] == "value"
        assert attempt.identity is identity
    else:
        assert kwargs == request
        assert attempt.identity is identity


def test_custom_backend_never_gets_oauth_guardian_metadata(monkeypatch):
    from agent import guardian_provenance as provenance
    _guardian(monkeypatch)
    agent = SimpleNamespace(_credential_pool=object())
    request = {"model": "model", "input": []}
    client = SimpleNamespace(base_url="https://example.test/backend-api/codex", api_key="fake")
    kwargs, attempt = provenance.prepare_main_request(agent, client, request)
    assert kwargs == request and attempt is None


def test_created_id_survives_terminal_frame_without_id():
    result = _consume_codex_event_stream([
        SimpleNamespace(type="response.created", response=SimpleNamespace(id="R1")),
        SimpleNamespace(type="response.completed", response=SimpleNamespace(status="completed")),
    ], model="test-model")
    assert result.id == "R1"
    assert result.guardian_completed is True


def test_interrupted_stream_cannot_prove_completed_parent():
    result = _consume_codex_event_stream([
        SimpleNamespace(type="response.created", response=SimpleNamespace(id="R-old")),
        SimpleNamespace(type="response.output_item.done", item=SimpleNamespace(
            type="function_call", id="fc1", call_id="T1", name="terminal", arguments='{"command":"pwd"}')),
    ], model="test-model")
    assert result.guardian_completed is False


def test_conflicting_response_ids_invalidate_parent():
    result = _consume_codex_event_stream([
        SimpleNamespace(type="response.created", response=SimpleNamespace(id="R1")),
        SimpleNamespace(type="response.completed", response=SimpleNamespace(id="R2", status="completed")),
    ], model="test-model")
    assert result.guardian_completed is False


def test_main_wire_cannot_inherit_reviewer_default_headers(monkeypatch):
    import httpx
    from openai import OpenAI
    from agent import guardian_provenance as provenance
    _guardian(monkeypatch)
    identity = SimpleNamespace(guardian_enabled=True)
    monkeypatch.setattr(provenance, "capture_identity", lambda *a: identity)
    received = []
    def handler(request):
        received.append(request)
        return httpx.Response(200, json={"id": "main", "object": "response", "output": []})
    headers = {"X-Codex-Guardian": "reviewer", "X-OpenAI-Subagent": "guardian", "Custom": "kept"}
    client = OpenAI(api_key="fake", base_url="https://chatgpt.com/backend-api/codex",
                    default_headers=headers, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        request, _ = provenance.prepare_main_request(SimpleNamespace(), client, {"model": "main", "input": []})
        client.responses.create(**request)
        assert "x-codex-guardian" not in received[0].headers
        assert "x-openai-subagent" not in received[0].headers
        assert received[0].headers["custom"] == "kept"
        assert client.default_headers["X-Codex-Guardian"] == "reviewer"
    finally:
        client.close()


def test_non_network_response_cannot_mint_parent_binding(monkeypatch):
    from agent import guardian_provenance as provenance
    _guardian(monkeypatch)
    monkeypatch.setattr(provenance, "capture_identity", lambda *a: SimpleNamespace(guardian_enabled=True))
    agent = SimpleNamespace()
    client = SimpleNamespace(api_key="fake", base_url="https://chatgpt.com/backend-api/codex")
    attempt = provenance.unproved_main_attempt(agent, client, {"model": "main"})
    response = SimpleNamespace(id="middleware-invented", guardian_completed=True, _guardian_attempt=attempt)
    provenance.bind_response_actions(agent, response, SimpleNamespace(tool_calls=[]), [])
    assert agent._guardian_response_backend == "codex_guardian"
    assert agent._guardian_actions == {}


def test_unchecked_account_does_not_depend_on_guardian_policy_resources(monkeypatch):
    from agent import guardian_provenance as provenance
    monkeypatch.setattr(provenance, "capture_identity", lambda *a: SimpleNamespace(guardian_enabled=False))
    monkeypatch.setattr(provenance, "current_policy", lambda: pytest.fail("off path loaded Guardian resources"))
    agent = SimpleNamespace()
    client = SimpleNamespace(api_key="fake", base_url="https://chatgpt.com/backend-api/codex")
    request = {"model": "main"}
    actual, attempt = provenance.prepare_main_request(agent, client, request)
    assert actual is request and attempt.guardian_requested is False
    response = SimpleNamespace(_guardian_attempt=attempt)
    provenance.bind_response_actions(agent, response, SimpleNamespace(tool_calls=[]), [])
    assert agent._guardian_response_backend == "legacy"
