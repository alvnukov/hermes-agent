"""Fallback restoration must adopt the fresh token of the manually chosen login."""
import json
from types import SimpleNamespace

from agent.agent_runtime_helpers import _rebind_primary_credential_pool
from agent.credential_pool import credential_pool_matches_provider, load_pool


def test_restore_adopts_only_selected_primary_account(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / "auth.json").write_text(json.dumps({
        "version": 1, "credential_pool_selections": {"openai-codex": True},
        "credential_pool": {"openai-codex": [{
            "id": name, "label": name, "priority": index, "source": "manual:device_code",
            "auth_type": "oauth", "access_token": f"fresh-{name}",
        } for index, name in enumerate(("primary", "fallback"))]},
    }))
    swaps = []
    agent = SimpleNamespace(
        _credential_pool=load_pool("openai-codex:fallback"),
        _credential_pool_entry_id="fallback", _swap_credential=lambda entry: swaps.append(entry),
    )
    route = "openai-codex:primary"
    _rebind_primary_credential_pool(
        agent, "openai-codex", "gpt-5",
        lambda candidate: credential_pool_matches_provider(candidate, route),
        lambda: load_pool(route), None, None,
    )
    assert [(entry.id, entry.access_token) for entry in swaps] == [("primary", "fresh-primary")]
    assert [entry.id for entry in agent._credential_pool.entries()] == ["primary"]
