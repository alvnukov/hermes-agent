"""Desktop Codex account additions use the CLI pool and preserve the singleton."""
import base64
import json
import time
import pytest

from fastapi.testclient import TestClient

from hermes_cli.web_server import _SESSION_TOKEN, app
from hermes_cli.web_routers import oauth


def test_guardian_checkbox_updates_only_the_explicit_profile_account(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    (tmp_path / "config.yaml").write_text("{}\n")
    target = tmp_path / "profiles" / "coder"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text("{}\n")
    token = _tokens("test")
    entry = {"id": "stable-account", "label": "Test", "priority": 0, "auth_type": "oauth",
             "source": "manual:device_code", "access_token": token["access_token"],
             "refresh_token": token["refresh_token"], "expires_at": time.time() + 86400}
    store = {"version": 1, "providers": {}, "credential_pool": {"openai-codex": [entry]}}
    (tmp_path / "auth.json").write_text(json.dumps(store))
    (target / "auth.json").write_text(json.dumps(store))
    client = TestClient(app)
    headers = {"X-Hermes-Session-Token": _SESSION_TOKEN}
    path = "/api/providers/oauth/openai-codex/accounts/stable-account?profile=coder"
    response = client.patch(path, json={"guardian_enabled": True}, headers=headers)
    assert response.status_code == 200, response.text
    saved = json.loads((target / "auth.json").read_text())
    assert saved["credential_pool"]["openai-codex"][0]["guardian_enabled"] is True
    assert json.loads((tmp_path / "auth.json").read_text()) == store
    response = client.patch(path, json={"guardian_enabled": False}, headers=headers)
    assert response.status_code == 200, response.text
    assert json.loads((target / "auth.json").read_text())["credential_pool"]["openai-codex"][0]["guardian_enabled"] is False


@pytest.mark.parametrize("value", ["true", 1, {}])
def test_guardian_checkbox_request_requires_boolean(value):
    from pydantic import ValidationError
    from hermes_cli.web_models import OAuthAccountUpdateBody
    with pytest.raises(ValidationError):
        OAuthAccountUpdateBody(guardian_enabled=value)


def _tokens(account):
    payload = base64.urlsafe_b64encode(json.dumps({
        "email": f"{account}@example.com", "sub": account, "exp": int(time.time()) + 86400,
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
    }).encode()).rstrip(b"=").decode()
    return {"access_token": f"e30.{payload}.sig", "refresh_token": f"refresh-{account}"}


def test_desktop_add_preserves_existing_account_and_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    (tmp_path / "config.yaml").write_text("{}\n")
    target = tmp_path / "profiles" / "coder"
    target.mkdir(parents=True)
    (target / "config.yaml").write_text("{}\n")
    root_auth = {"version": 1, "providers": {}, "sentinel": "root untouched"}
    (tmp_path / "auth.json").write_text(json.dumps(root_auth))
    first, second = _tokens("first"), _tokens("second")
    logins = iter([first, second])
    monkeypatch.setattr(oauth, "_codex_request_user_code", lambda _: {
        "user_code": "TEST-CODE", "device_auth_id": "test-device", "interval": 1,
    })
    monkeypatch.setattr(oauth, "_codex_poll_authorization", lambda *args: {})
    monkeypatch.setattr(oauth, "_codex_exchange_tokens", lambda *args: next(logins))
    monkeypatch.setattr(oauth, "_start_poller", lambda target, sid, **kw: target(sid))
    catalog = next(p for p in oauth._OAUTH_PROVIDER_CATALOG if p["id"] == "openai-codex")
    monkeypatch.setattr(oauth, "_build_oauth_catalog", lambda: [catalog])

    async def no_metric(*args):
        return None

    monkeypatch.setattr(oauth, "_begin_oauth_setup_metric", no_metric)
    client = TestClient(app)
    headers = {"X-Hermes-Session-Token": _SESSION_TOKEN}
    path = "/api/providers/oauth/openai-codex/start?profile=coder"
    try:
        initial = client.post(path, headers=headers)
        assert initial.status_code == 200, initial.text
        # Read the same Accounts endpoint as Desktop, seeding the legacy singleton.
        before = client.get("/api/providers/oauth?profile=coder", headers=headers)
        assert before.status_code == 200, before.text
        added = client.post(path + "&add_account=true", headers=headers)
        assert added.status_code == 200, added.text
        sid = added.json()["session_id"]
        assert client.get(f"/api/providers/oauth/openai-codex/poll/{sid}?profile=coder",
                          headers=headers).json()["status"] == "approved"
        store = json.loads((target / "auth.json").read_text())
        assert store["providers"]["openai-codex"]["tokens"] == first
        entries = store["credential_pool"]["openai-codex"]
        assert {e["access_token"] for e in entries} == {first["access_token"], second["access_token"]}
        assert len({e["id"] for e in entries}) == 2
        independent = next(e for e in entries if e["access_token"] == second["access_token"])
        assert independent["source"] == "manual:device_code"
        assert independent["refresh_token"] == second["refresh_token"]
        accounts = client.get("/api/providers/oauth?profile=coder", headers=headers).json()["providers"][0]
        assert accounts["supports_add_account"] is True
        assert {a["id"] for a in accounts["accounts"]} == {e["id"] for e in entries}
        assert "access_token" not in json.dumps(accounts)
        assert "refresh-second" not in json.dumps(accounts)
        shared_root = json.loads((tmp_path / "auth.json").read_text())
        assert shared_root["sentinel"] == root_auth["sentinel"]
        assert shared_root["providers"] == root_auth["providers"]
        references = shared_root["credential_pool"]["openai-codex"]
        assert {r["owner_credential_id"] for r in references} == {e["id"] for e in entries}
        assert all("access_token" not in r and "refresh_token" not in r for r in references)
        # Cancellation after token exchange must not commit a third account.
        saved = (target / "auth.json").read_text()

        def cancel_before_save(*args):
            pending = next(s for s in oauth._oauth_sessions.values() if s["status"] == "pending")
            cancelled = client.delete(
                f'/api/providers/oauth/sessions/{pending["session_id"]}?profile=coder', headers=headers)
            assert cancelled.status_code == 200, cancelled.text
            return _tokens("cancelled")

        monkeypatch.setattr(oauth, "_codex_exchange_tokens", cancel_before_save)
        cancelled_sid, session = oauth._new_oauth_session("openai-codex", "device_code", profile="coder")
        session["add_account"] = True
        oauth._codex_full_login_worker(cancelled_sid)
        assert (target / "auth.json").read_text() == saved
        assert json.loads((tmp_path / "auth.json").read_text()) == shared_root
    finally:
        oauth._oauth_sessions.clear()
