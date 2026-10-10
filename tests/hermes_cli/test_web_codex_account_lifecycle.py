"""Desktop account management uses stable identities and tokenless profile links."""
import json

import pytest
from fastapi.testclient import TestClient

from hermes_cli.web_server import _SESSION_TOKEN, app
from hermes_cli.web_routers import oauth


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    (tmp_path / "config.yaml").write_text("{}\n")
    for profile in ("alpha", "beta"):
        home = tmp_path / "profiles" / profile
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n")
    rows = [dict(id=f"account-{i}", label=f"Account {i}", source="manual:device_code",
                 auth_type="oauth", priority=i, access_token=f"private-access-{i}",
                 refresh_token=f"private-refresh-{i}") for i in range(2)]
    source = tmp_path / "profiles" / "alpha" / "auth.json"
    source.write_text(json.dumps({"version": 1, "credential_pool": {"openai-codex": rows}}))
    catalog = next(p for p in oauth._OAUTH_PROVIDER_CATALOG if p["id"] == "openai-codex")
    monkeypatch.setattr(oauth, "_build_oauth_catalog", lambda: [catalog])
    yield TestClient(app), {"X-Hermes-Session-Token": _SESSION_TOKEN}, tmp_path, source


def _list(client, headers, profile):
    response = client.get(f"/api/providers/oauth?profile={profile}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["providers"][0]


def test_link_existing_account_then_disable_enable_and_unlink(accounts):
    client, headers, root, source = accounts
    before = source.read_text()
    listing = _list(client, headers, "beta")
    assert listing["supports_account_management"] is True
    assert {(a["owner_profile"], a["id"]) for a in listing["available_accounts"]} == {
        ("alpha", "account-0"), ("alpha", "account-1")}
    base = "/api/providers/oauth/openai-codex/accounts"
    linked = client.post(base + "/link?profile=beta", headers=headers,
                         json={"owner_profile": "alpha", "account_id": "account-1"})
    assert linked.status_code == 200, linked.text
    linked = client.post(base + "/link?profile=beta", headers=headers,
                         json={"owner_profile": "alpha", "account_id": "account-1"})
    assert linked.status_code == 200, linked.text
    assert len(_list(client, headers, "beta")["accounts"]) == 1
    row = _list(client, headers, "beta")["accounts"][0]
    assert row["shared"] is True and row["owner_profile"] == "alpha"
    path = f'{base}/{row["id"]}?profile=beta'
    for enabled in (False, True):
        changed = client.patch(path, headers=headers, json={"enabled": enabled})
        assert changed.status_code == 200, changed.text
        assert _list(client, headers, "beta")["accounts"][0]["enabled"] is enabled
    stored = (root / "profiles" / "beta" / "auth.json").read_text()
    assert "private-access" not in stored and "private-refresh" not in stored
    assert "private-access" not in json.dumps(_list(client, headers, "beta"))
    assert source.read_text() == before
    removed = client.delete(path, headers=headers)
    assert removed.status_code == 200, removed.text
    assert _list(client, headers, "beta")["accounts"] == []
    assert source.read_text() == before


def test_owned_account_controls_and_delete_target_stable_id(accounts):
    client, headers, root, source = accounts
    base = "/api/providers/oauth/openai-codex/accounts"
    changed = client.patch(base + "/account-1?profile=alpha", headers=headers,
                           json={"label": "Work subscription", "priority": 0, "enabled": False})
    assert changed.status_code == 200, changed.text
    rows = _list(client, headers, "alpha")["accounts"]
    assert rows[0]["id"] == "account-1" and rows[0]["label"] == "Work subscription"
    assert rows[0]["enabled"] is False
    removed = client.delete(base + "/account-0?profile=alpha", headers=headers)
    assert removed.status_code == 200, removed.text
    assert [a["id"] for a in _list(client, headers, "alpha")["accounts"]] == ["account-1"]
    assert not (root / "profiles" / "beta" / "auth.json").exists()


def test_account_mutations_require_session_token_and_reject_unknown_owner(accounts):
    client, headers, root, source = accounts
    base = "/api/providers/oauth/openai-codex/accounts"
    before = source.read_text()
    rejected = client.patch(base + "/account-0?profile=alpha", json={"enabled": False})
    assert rejected.status_code in (401, 403)
    rejected = client.post(base + "/link?profile=beta", headers=headers,
                           json={"owner_profile": "../alpha", "account_id": "account-0"})
    assert rejected.status_code in (400, 404, 422)
    assert source.read_text() == before


def test_removed_owner_remains_visible_as_unavailable_link(accounts):
    client, headers, root, source = accounts
    base = "/api/providers/oauth/openai-codex/accounts"
    response = client.post(base + "/link?profile=beta", headers=headers,
                           json={"owner_profile": "alpha", "account_id": "account-1"})
    assert response.status_code == 200, response.text
    response = client.delete(base + "/account-1?profile=alpha", headers=headers)
    assert response.status_code == 200, response.text
    rows = _list(client, headers, "beta")["accounts"]
    assert len(rows) == 1
    assert rows[0]["missing"] is True and rows[0]["enabled"] is False


def test_add_account_preserves_inherited_account_as_tokenless_assignment(accounts, monkeypatch):
    client, headers, root, source = accounts
    root_row = dict(id="root-first", label="First account", source="manual:device_code",
                    auth_type="oauth", priority=0, access_token="root-private-access",
                    refresh_token="root-private-refresh")
    (root / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"openai-codex": [root_row]}}))
    monkeypatch.setattr(oauth, "_codex_request_user_code", lambda _: {
        "user_code": "TEST-CODE", "device_auth_id": "test-device", "interval": 1,
    })
    monkeypatch.setattr(oauth, "_codex_poll_authorization", lambda *args: {})
    monkeypatch.setattr(oauth, "_codex_exchange_tokens", lambda *args: {
        "access_token": "second-private-access", "refresh_token": "second-private-refresh",
    })
    monkeypatch.setattr(oauth, "_start_poller", lambda target, sid, **kw: target(sid))

    async def no_metric(*args):
        return None

    monkeypatch.setattr(oauth, "_begin_oauth_setup_metric", no_metric)
    response = client.post("/api/providers/oauth/openai-codex/start?profile=beta&add_account=true", headers=headers)
    assert response.status_code == 200, response.text
    rows = _list(client, headers, "beta")["accounts"]
    assert len(rows) == 2
    assert any(row["shared"] and row["owner_credential_id"] == "root-first" for row in rows)
    target = (root / "profiles" / "beta" / "auth.json").read_text()
    assert "root-private-access" not in target and "root-private-refresh" not in target


def test_normal_login_after_removing_last_account_restores_access(accounts, monkeypatch):
    client, headers, root, source = accounts
    home = root / "profiles" / "beta"
    row = dict(id="legacy", label="First account", source="device_code", auth_type="oauth",
               priority=0, access_token="old-access", refresh_token="old-refresh")
    (home / "auth.json").write_text(json.dumps({"version": 1,
        "providers": {"openai-codex": {"tokens": {
            "access_token": "old-access", "refresh_token": "old-refresh"}}},
        "credential_pool": {"openai-codex": [row]}}))
    response = client.delete("/api/providers/oauth/openai-codex/accounts/legacy?profile=beta", headers=headers)
    assert response.status_code == 200, response.text
    assert _list(client, headers, "beta")["accounts"] == []
    monkeypatch.setattr(oauth, "_codex_request_user_code", lambda _: {
        "user_code": "TEST-CODE", "device_auth_id": "test-device", "interval": 1})
    monkeypatch.setattr(oauth, "_codex_poll_authorization", lambda *args: {})
    monkeypatch.setattr(oauth, "_codex_exchange_tokens", lambda *args: {
        "access_token": "new-access", "refresh_token": "new-refresh"})
    monkeypatch.setattr(oauth, "_start_poller", lambda target, sid, **kw: target(sid))

    async def no_metric(*args):
        return None

    monkeypatch.setattr(oauth, "_begin_oauth_setup_metric", no_metric)
    response = client.post("/api/providers/oauth/openai-codex/start?profile=beta", headers=headers)
    assert response.status_code == 200, response.text
    rows = _list(client, headers, "beta")["accounts"]
    assert len(rows) == 1
    assert rows[0]["enabled"] is True and rows[0]["shared"] is False
    assert "new-access" in (home / "auth.json").read_text()
