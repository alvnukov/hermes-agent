"""Account providers must stay visible and bind execution to the chosen login."""
import json

import pytest

from agent.credential_pool import load_pool
from hermes_cli.auth import AuthError
from hermes_cli.inventory import build_models_payload, load_picker_context
from hermes_cli.runtime_provider import resolve_runtime_provider, resolve_runtime_with_fallback


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    rows = [{
        "id": name, "label": f"Codex {name}", "source": "manual:device_code",
        "auth_type": "oauth", "access_token": f"token-{name}", "priority": index,
    } for index, name in enumerate(("first", "second"))]
    store = {
        "version": 1, "credential_pool_selections": {"openai-codex": True},
        "credential_pool": {"openai-codex": rows},
    }
    (tmp_path / "auth.json").write_text(json.dumps(store))
    (tmp_path / "config.yaml").write_text("model:\n  provider: openai-codex:second\n  default: gpt-5\n")
    return tmp_path, store


def test_picker_exposes_named_account_providers_without_credentials(accounts, monkeypatch):
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", lambda **kw: [{
        "slug": "openai-codex", "name": "ChatGPT or Codex Subscription", "models": ["gpt-5"],
        "authenticated": True,
    }])
    monkeypatch.setattr("hermes_cli.inventory._local_runtime_row", lambda ctx: None)
    payload = build_models_payload(load_picker_context())
    providers = {row["slug"]: row for row in payload["providers"]}
    for name in ("first", "second"):
        row = providers[f"openai-codex:{name}"]
        assert f"Codex {name}" in row["name"]
        assert row["models"] == providers["openai-codex"]["models"]
        assert row["authenticated"] is True
    assert providers["openai-codex:second"]["is_current"] is True
    assert "token-first" not in json.dumps(payload)
    assert "token-second" not in json.dumps(payload)


@pytest.mark.parametrize("requested", ["openai-codex:second", "auto", None])
def test_account_route_pins_runtime_rotation_and_auxiliary_client(accounts, requested):
    runtime = resolve_runtime_provider(requested=requested, target_model="gpt-5")
    assert runtime["api_key"] == "token-second"
    assert runtime["api_mode"] == "codex_responses"
    pool = runtime["credential_pool"]
    assert [row.id for row in pool.entries()] == ["second"]
    from agent.auxiliary_client import resolve_provider_client

    client, model = resolve_provider_client("openai-codex:second", "gpt-5", raw_codex=True)
    assert client.api_key == "token-second"
    assert model == "gpt-5"
    assert pool.mark_exhausted_and_rotate(status_code=429, credential_id="second") is None
    assert [row.id for row in load_pool("openai-codex:second").entries()] == ["second"]
    disk = json.loads((accounts[0] / "auth.json").read_text())
    first = next(row for row in disk["credential_pool"]["openai-codex"] if row["id"] == "first")
    assert not first.get("last_status")


def test_unavailable_account_uses_only_declared_fallback(accounts):
    root, store = accounts
    store["credential_pool"]["openai-codex"][1]["enabled"] = False
    (root / "auth.json").write_text(json.dumps(store))
    with pytest.raises(AuthError, match="account"):
        resolve_runtime_provider(requested="openai-codex:second", target_model="gpt-5")
    fallback = {"provider": "openai-codex:first", "model": "gpt-5"}
    runtime, entry = resolve_runtime_with_fallback(
        {"fallback_providers": [fallback]}, requested="openai-codex:second", target_model="gpt-5",
    )
    assert entry == fallback
    assert runtime["api_key"] == "token-first"
    assert [row.id for row in runtime["credential_pool"].entries()] == ["first"]


def test_main_model_assignment_preserves_selected_account_provider(accounts):
    from fastapi.testclient import TestClient
    from hermes_cli.web_server import _SESSION_TOKEN, app

    client = TestClient(app)
    response = client.post("/api/model/set", headers={"X-Hermes-Session-Token": _SESSION_TOKEN}, json={
        "scope": "main", "provider": "openai-codex:second", "model": "gpt-6.1-sol",
        "confirm_expensive_model": True,
    })
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "openai-codex:second"
    assert load_picker_context().current_provider == "openai-codex:second"
    runtime = resolve_runtime_provider(target_model="gpt-6.1-sol")
    assert runtime["api_key"] == "token-second"


def test_auto_auxiliary_inherits_selected_account(accounts):
    from agent.auxiliary_client import resolve_provider_client

    runtime = resolve_runtime_provider(requested="openai-codex:second", target_model="gpt-5")
    client, model = resolve_provider_client("auto", raw_codex=True, main_runtime={**runtime, "model": "gpt-5"})
    assert getattr(client, "_real_client", client).api_key == "token-second"
    assert model == "gpt-5"


def test_main_agent_can_fall_back_to_same_model_on_another_account(accounts):
    from unittest.mock import patch
    from run_agent import AIAgent

    runtime = resolve_runtime_provider(target_model="gpt-5")
    with (patch("model_tools.get_tool_definitions", return_value=[]),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        agent = AIAgent(
            model="gpt-5", provider=runtime["provider"], requested_provider=runtime["requested_provider"],
            api_key=runtime["api_key"], base_url=runtime["base_url"], api_mode=runtime["api_mode"],
            credential_pool=runtime["credential_pool"], quiet_mode=True,
            skip_context_files=True, skip_memory=True,
            fallback_model=[{"provider": "openai-codex:first", "model": "gpt-5"}],
        )
    assert agent._try_activate_fallback() is True
    assert agent.api_key == "token-first"
    assert agent.requested_provider == "openai-codex:first"
    assert agent.api_mode == "codex_responses"
    assert [row.id for row in agent._credential_pool.entries()] == ["first"]


@pytest.mark.parametrize("policy", ["auto", "explicit"])
def test_unavailable_auxiliary_account_reaches_declared_fallback(accounts, monkeypatch, policy):
    from types import SimpleNamespace
    from agent import auxiliary_client as auxiliary

    root, store = accounts
    runtime = resolve_runtime_provider(target_model="gpt-5")
    store["credential_pool"]["openai-codex"][1]["enabled"] = False
    (root / "auth.json").write_text(json.dumps(store))
    fallback = "  - provider: openai-codex:first\n    model: gpt-5\n"
    with (root / "config.yaml").open("a") as config:
        config.write("fallback_providers:\n" + fallback if policy == "auto" else
                     "auxiliary:\n  compression:\n    provider: openai-codex:second\n"
                     "    model: gpt-5\n    fallback_chain:\n    " + fallback.replace("\n", "\n    ").rstrip() + "\n")
    sent = []
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")])

    def complete(client, kwargs, **options):
        sent.append(client.api_key)
        return response

    monkeypatch.setattr(auxiliary, "_relay_sync_completion", complete)
    assert auxiliary.call_llm(task="compression", main_runtime={**runtime, "model": "gpt-5"},
                              messages=[{"role": "user", "content": "test"}]) is response
    assert sent == ["token-first"]


@pytest.mark.parametrize("refresh_succeeds", [True, False])
def test_auto_auxiliary_recovery_keeps_account_or_declared_fallback(accounts, monkeypatch, refresh_succeeds):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import httpx
    from openai import AuthenticationError
    from agent import auxiliary_client as auxiliary

    root, store = accounts
    for row in store["credential_pool"]["openai-codex"]:
        row.update(refresh_token=f'rt-{row["id"]}', last_refresh=datetime.now(timezone.utc).isoformat())
    (root / "auth.json").write_text(json.dumps(store))
    if not refresh_succeeds:
        with (root / "config.yaml").open("a") as config:
            config.write("fallback_providers:\n  - provider: openai-codex:first\n    model: gpt-5\n")
    sent, refreshed = [], []
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")])

    def complete(client, kwargs, **options):
        sent.append(client.api_key)
        if len(sent) == 1:
            raise AuthenticationError("expired", response=httpx.Response(
                401, request=httpx.Request("POST", "https://chatgpt.com/backend-api/codex")), body=None)
        return response

    def refresh(access_token, refresh_token):
        refreshed.append(refresh_token)
        if not refresh_succeeds:
            raise AuthError("grant rejected", provider="openai-codex", code="invalid_grant")
        return {"access_token": "fresh-second", "refresh_token": "rt-second-fresh",
                "last_refresh": datetime.now(timezone.utc).isoformat()}

    monkeypatch.setattr(auxiliary, "_relay_sync_completion", complete)
    monkeypatch.setattr("hermes_cli.auth.refresh_codex_oauth_pure", refresh)
    runtime = resolve_runtime_provider(target_model="gpt-5")
    result = auxiliary.call_llm(task="compression", main_runtime={**runtime, "model": "gpt-5"},
                               messages=[{"role": "user", "content": "test"}])
    assert result is response
    assert sent == ["token-second", "fresh-second" if refresh_succeeds else "token-first"]
    assert refreshed == ["rt-second"]
    disk = json.loads((root / "auth.json").read_text())
    first = next(row for row in disk["credential_pool"]["openai-codex"] if row["id"] == "first")
    assert first["access_token"] == "token-first"
    assert first["refresh_token"] == "rt-first"
    assert not first.get("last_status")
