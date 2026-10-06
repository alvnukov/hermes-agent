"""Real SDK and mock HTTP/SSE verify Guardian's OAuth wire boundary."""
from types import SimpleNamespace
import json

import httpx
import pytest
from openai import OpenAI


def _events(text='{"outcome":"allow"}', terminal=True):
    events = [
        {"type": "response.created", "response": {"id": "review-id"}},
        {"type": "response.output_text.delta", "delta": text},
    ]
    if terminal:
        events.append({"type": "response.completed", "response": {
            "id": "review-id", "status": "completed", "usage": {
                "input_tokens": 10, "output_tokens": 4, "total_tokens": 14}}})
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events)


def _transport(handler):
    from agent.guardian_transport import GuardianTransport
    from agent.codex_headers import codex_cloudflare_headers
    identity = SimpleNamespace(base_url="https://chatgpt.com/backend-api/codex", entry_id="A")
    entry = SimpleNamespace(runtime_api_key="fake-access-A")
    def factory(identity, entry, remaining):
        return OpenAI(api_key=entry.runtime_api_key, base_url=identity.base_url, max_retries=0,
                      default_headers=codex_cloudflare_headers(entry.runtime_api_key),
                      http_client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))
    return GuardianTransport(client_factory=factory), identity, entry


def _catalog():
    return {"models": [{"slug": "codex-auto-review", "default_reasoning_level": "medium",
                         "supported_reasoning_levels": [{"effort": "medium"}], "use_responses_lite": True}]}


def test_real_sdk_reviewer_fields_and_usage():
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_catalog())
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_events())
    transport, identity, entry = _transport(handler)
    result = transport.review(identity, entry, parent_response_id="R1", parent_model="main",
                              transcript='[{"role":"user","content":"safe synthetic goal"}]',
                              action={"tool": "terminal", "args": {"command": "pwd"}}, policy="", config={})
    assert result.assessment.outcome == "allow"
    assert result.usage == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
    post = requests[-1]
    assert post.url.path == "/backend-api/codex/responses"
    assert post.headers["x-codex-guardian"] == "reviewer"
    assert post.headers["authorization"] == "Bearer fake-access-A"
    assert post.headers["originator"] == "hermes-agent"
    body = json.loads(post.content)
    assert body["client_metadata"] == {"parent_response_id": "R1", "x-openai-subagent": "guardian"}
    assert "service_tier" not in body and "previous_response_id" not in body and "tools" not in body
    assert body["reasoning"]["effort"] == "medium"


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_error_never_uses_another_endpoint_or_model(status):
    posts = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_catalog())
        posts.append(request)
        return httpx.Response(status, json={"error": {"message": "private-error-secret"}})
    transport, identity, entry = _transport(handler)
    from agent.guardian_protocol import GuardianProtocolError
    with pytest.raises(GuardianProtocolError) as error:
        transport.review(identity, entry, parent_response_id="R1", parent_model="main", transcript="[]",
                         action={}, policy="", config={"max_retries": 0})
    assert "private-error-secret" not in str(error.value)
    assert len(posts) == 1
    assert json.loads(posts[0].content)["model"] == "codex-auto-review"


def test_redirect_and_incomplete_stream_are_not_verdicts():
    from agent.guardian_protocol import GuardianProtocolError
    for response in [httpx.Response(302, headers={"location": "https://evil.test/collect"}),
                     httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_events(terminal=False))]:
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=_catalog()) if request.method == "GET" else response
        transport, identity, entry = _transport(handler)
        with pytest.raises(GuardianProtocolError):
            transport.review(identity, entry, parent_response_id="R1", parent_model="main", transcript="[]",
                             action={}, policy="", config={"max_retries": 0})
        assert all(request.url.host == "chatgpt.com" for request in requests)


def test_reported_usage_survives_invalid_assessment():
    from agent.guardian_protocol import GuardianProtocolError
    def handler(request):
        return (httpx.Response(200, json=_catalog()) if request.method == "GET" else
                httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_events("invalid")))
    transport, identity, entry = _transport(handler)
    usage = []
    with pytest.raises(GuardianProtocolError):
        transport.review(identity, entry, parent_response_id="R1", parent_model="main", transcript="[]",
                         action={}, policy="", config={}, on_usage=lambda rid, counts: usage.append((rid, counts)))
    assert usage == [("review-id", {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14})]


def test_sdk_debug_logs_do_not_expose_review_body_or_token(caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    def handler(request):
        return (httpx.Response(200, json=_catalog()) if request.method == "GET" else
                httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_events()))
    transport, identity, entry = _transport(handler)
    transport.review(identity, entry, parent_response_id="R1", parent_model="main", transcript="private-user-goal",
                     action={}, policy="private-local-policy", config={})
    assert "private-user-goal" not in caplog.text
    assert "private-local-policy" not in caplog.text
    assert "fake-access-A" not in caplog.text
    logging.getLogger("openai._base_client").debug("ordinary-call-logging-remains")
    assert "ordinary-call-logging-remains" in caplog.text


def test_client_initialization_error_is_a_safe_protocol_failure():
    from openai import OpenAIError
    from agent.guardian_transport import GuardianTransport
    from agent.guardian_protocol import GuardianProtocolError
    def factory(*args):
        raise OpenAIError("private-credential-detail")
    transport = GuardianTransport(client_factory=factory)
    identity = SimpleNamespace(base_url="https://chatgpt.com/backend-api/codex")
    with pytest.raises(GuardianProtocolError) as error:
        transport.review(identity, SimpleNamespace(), parent_response_id="R1", parent_model="main",
                         transcript="[]", action={}, policy="", config={})
    assert error.value.code == "transport_error" and "private" not in str(error.value)
