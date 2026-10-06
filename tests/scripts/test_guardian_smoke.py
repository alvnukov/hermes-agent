"""The opt-in smoke command cannot touch accounts before all flags are explicit."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("argv", [
    [],
    ["--account", "account-entry", "--model", "main-model"],
    ["--live", "--model", "main-model"],
    ["--live", "--account", "account-entry"],
    ["--live", "--account", "", "--model", "main-model"],
    ["--live", "--account", "account-entry", "--model", ""],
    ["--live", "--account", " account-entry ", "--model", "main-model"],
])
def test_missing_explicit_opt_in_never_opens_accounts_or_network(argv, monkeypatch, capsys):
    smoke = importlib.import_module("scripts.guardian_smoke")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Invalid smoke invocation reached live accounts or transport")

    monkeypatch.setattr(smoke, "_run_live", forbidden, raising=False)
    assert smoke.main(argv) == 2
    result = json.loads(capsys.readouterr().out)
    assert result == {"status": "error", "code": "explicit_opt_in_required", "billing": "unknown"}


def test_invalid_cli_outputs_only_safe_code(monkeypatch, capsys):
    smoke = importlib.import_module("scripts.guardian_smoke")
    monkeypatch.setattr(smoke, "_run_live", lambda *_a, **_k: pytest.fail("Live path entered"), raising=False)
    assert smoke.main(["--live", "--account"]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out) == {"status": "error", "code": "invalid_cli", "billing": "unknown"}
    assert not output.err


def test_provider_exception_details_are_not_printed(monkeypatch, capsys):
    smoke = importlib.import_module("scripts.guardian_smoke")

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("private provider body and bearer token")

    monkeypatch.setattr(smoke, "_run_live", unavailable, raising=False)
    assert smoke.main(["--live", "--account", "account-entry", "--model", "main-model"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"status": "error", "code": "smoke_unavailable", "billing": "unknown"}
    assert "private" not in output.out + output.err


@pytest.mark.parametrize("arguments", [
    '{"command":"pwd","command":"rm private-file"}',
    '{"command":NaN}',
    'prefix {"command":"pwd"}',
])
def test_ambiguous_or_malformed_actual_proposal_is_never_substituted(arguments):
    smoke = importlib.import_module("scripts.guardian_smoke")
    response = SimpleNamespace(id="main-response", guardian_completed=True, output=[
        SimpleNamespace(type="function_call", call_id="actual-call", name="terminal", arguments=arguments),
    ])
    with pytest.raises(smoke._SmokeError) as error:
        smoke._proposal(response)
    assert error.value.code == "malformed_proposal"


def test_incomplete_main_cannot_supply_a_reviewer_parent():
    smoke = importlib.import_module("scripts.guardian_smoke")
    with pytest.raises(smoke._SmokeError) as error:
        smoke._proposal(SimpleNamespace(id="real-but-incomplete", guardian_completed=False, output=[]))
    assert error.value.code == "incomplete_main_response"


def _accounts(monkeypatch, tmp_path, *, guardian_enabled=True):
    from agent import credential_pool, guardian_credentials, guardian_provenance
    from tools import approval_context

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    entry = credential_pool.PooledCredential(
        provider="openai-codex", id="entry-A", label="account label", auth_type="oauth",
        priority=0, source="device_code", access_token="fake-access-only",
        base_url="https://chatgpt.com/backend-api/codex")
    pool = credential_pool.CredentialPool("openai-codex", [entry])
    identity = SimpleNamespace(base_url=entry.base_url, entry_id=entry.id, guardian_enabled=guardian_enabled)
    monkeypatch.setattr(credential_pool, "load_pool", lambda _provider: pool)
    monkeypatch.setattr(guardian_credentials, "capture_identity", lambda *_args: identity)
    monkeypatch.setattr(guardian_provenance, "capture_identity", lambda *_args: identity)
    monkeypatch.setattr(guardian_credentials, "resolve_pinned", lambda *_args, **_kwargs: entry)
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"codex_guardian": {"max_retries": 0}})
    return entry, identity


@pytest.mark.parametrize("account,enabled,code", [
    ("account label", True, "account_unavailable"),
    ("1", True, "account_unavailable"),
    ("entry-A", False, "guardian_disabled"),
])
def test_account_must_be_exact_id_with_checkbox_enabled(account, enabled, code, monkeypatch, tmp_path, capsys):
    from agent import guardian_transport
    smoke = importlib.import_module("scripts.guardian_smoke")
    _accounts(monkeypatch, tmp_path, guardian_enabled=enabled)
    monkeypatch.setattr(guardian_transport, "_client_factory",
                        lambda *_a, **_k: pytest.fail("Rejected account opened network transport"))
    assert smoke.main(["--live", "--account", account, "--model", "main-model"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == code


def test_real_sdk_stream_parent_and_exact_proposal_reach_real_reviewer(monkeypatch, tmp_path, capsys):
    import httpx
    from openai import OpenAI
    from agent import guardian_transport

    smoke = importlib.import_module("scripts.guardian_smoke")
    entry, identity = _accounts(monkeypatch, tmp_path)
    requests = []
    main_events = [
        {"type": "response.created", "response": {"id": "main-real-id"}},
        {"type": "response.output_item.done", "output_index": 0, "item": {
            "type": "function_call", "id": "fc_actual", "call_id": "call_actual", "name": "terminal",
            "status": "completed", "arguments": '{"command":"pwd"}'}},
        {"type": "response.completed", "response": {"id": "main-real-id", "status": "completed",
         "usage": {"input_tokens": 15, "output_tokens": 5, "total_tokens": 20}}},
    ]
    review_events = [
        {"type": "response.created", "response": {"id": "review-real-id"}},
        {"type": "response.output_text.delta", "delta": '{"outcome":"allow","rationale":"private review rationale"}'},
        {"type": "response.completed", "response": {"id": "review-real-id", "status": "completed",
         "usage": {"input_tokens": 30, "output_tokens": 7, "total_tokens": 37}}},
    ]

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{
                "slug": "codex-auto-review", "default_reasoning_level": "medium",
                "supported_reasoning_levels": [{"effort": "medium"}], "use_responses_lite": True,
            }]})
        body = json.loads(request.content)
        events = main_events if body["model"] == "main-model" else review_events
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join("data: " + json.dumps(event) + "\n\n" for event in events))

    def factory(selected_identity, selected_entry, remaining):
        assert selected_identity is identity and selected_entry is entry
        return OpenAI(api_key=entry.runtime_api_key, base_url=identity.base_url, max_retries=0,
                      http_client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))

    transport_type = guardian_transport.GuardianTransport
    monkeypatch.setattr(guardian_transport, "_client_factory", factory)
    monkeypatch.setattr(guardian_transport, "GuardianTransport", lambda: transport_type(client_factory=factory))
    assert smoke.main(["--live", "--account", "entry-A", "--model", "main-model"]) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["main_response_id"] == "main-real-id"
    assert result["review_response_id"] == "review-real-id"
    assert result["main_usage"] == {"input_tokens": 15, "output_tokens": 5, "total_tokens": 20}
    assert result["review_usage"] == {"input_tokens": 30, "output_tokens": 7, "total_tokens": 37}
    assert result["outcome"] == "allow" and result["billing"] == "unknown"
    assert "private" not in output.out + output.err and "fake-access" not in output.out + output.err
    posts = [request for request in requests if request.method == "POST"]
    assert len(posts) == 2
    main_body, review_body = [json.loads(request.content) for request in posts]
    assert main_body["client_metadata"] == {"guardian_credits_requested": "true"}
    assert review_body["client_metadata"]["parent_response_id"] == "main-real-id"
    assert review_body["model"] == "codex-auto-review"
    evidence = " ".join(part["text"] for item in review_body["input"] for part in item["content"])
    planned = evidence.split("Planned action JSON:\n", 1)[1].split(">>> APPROVAL REQUEST END", 1)[0]
    assert json.loads(planned) == {"tool_call_id": "call_actual", "tool": "terminal", "arguments": {"command": "pwd"}}
    assert all(request.url.path.startswith("/backend-api/codex/") for request in requests)


def test_setuptools_includes_guardian_policy_and_license_resources(monkeypatch):
    from setuptools import Distribution
    from setuptools.command.build_py import build_py
    from setuptools.config.pyprojecttoml import read_configuration

    root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(root)
    config = read_configuration(str(root / "pyproject.toml"))
    distribution = Distribution({
        "packages": ["agent"],
        "package_data": config["tool"]["setuptools"]["package-data"],
    })
    command = build_py(distribution)
    command.finalize_options()
    # Verify declared package data independently of an sdist manifest.
    command.manifest_files = {}
    included = {Path(path).name for path in command.find_data_files("agent", "agent")}
    assert {"policy.md", "policy_template.md", "output_contract.md", "LICENSE", "NOTICE", "UPSTREAM.md"} <= included
