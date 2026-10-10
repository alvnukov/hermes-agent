"""A Codex login in any profile immediately exposes the account to the whole fleet."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hermes_cli import auth, auth_commands
from hermes_cli.web_routers import oauth
from hermes_cli.web_server import _SESSION_TOKEN, app
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    (root / "config.yaml").write_text("{}\n")
    for name in ("owner", "peer"):
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n")
    (root / "profiles" / "shell").mkdir()
    for name in (".peer.staging-123", "default"):
        home = root / "profiles" / name
        home.mkdir()
        (home / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None
    auth._global_auth_store_cache = None
    peer = root / "profiles" / "peer"
    (peer / "auth.json").write_text(json.dumps({
        "providers": {}, "credential_pool_selections": {"openai-codex": True},
        "credential_pool": {"openai-codex": [{
            "id": "existing", "label": "Existing disabled account", "priority": 0,
            "source": "manual:device_code", "auth_type": "oauth", "enabled": False,
            "access_token": "existing-token", "refresh_token": "existing-refresh",
        }]},
    }))
    return root


def _assert_shared(fleet, account_id):
    for home in (fleet, fleet / "profiles" / "peer"):
        assert (home / "auth.json").is_file(), f"new Codex account missing from {home.name}"
        store = json.loads((home / "auth.json").read_text())
        rows = store["credential_pool"]["openai-codex"]
        shared = next((r for r in rows if r.get("owner_credential_id") == account_id), None)
        assert shared is not None, f"new Codex account missing from {home.name}"
        assert shared["owner_profile"] == "owner" and shared["source"] == "shared"
        assert "access_token" not in shared and "refresh_token" not in shared
    peer_rows = json.loads((fleet / "profiles" / "peer" / "auth.json").read_text())["credential_pool"]["openai-codex"]
    existing = next(r for r in peer_rows if r["id"] == "existing")
    assert existing["enabled"] is False and existing["priority"] == 0
    for name in ("shell", ".peer.staging-123", "default"):
        assert not (fleet / "profiles" / name / "auth.json").exists()


@pytest.mark.parametrize("add_account", [False, True])
def test_desktop_login_shares_account_with_explicit_and_default_profiles(fleet, monkeypatch, add_account):
    monkeypatch.setattr(oauth, "_codex_request_user_code", lambda _: {
        "user_code": "TEST-CODE", "device_auth_id": "test-device", "interval": 1,
    })
    monkeypatch.setattr(oauth, "_codex_poll_authorization", lambda *args: {})
    monkeypatch.setattr(oauth, "_codex_exchange_tokens", lambda *args: {
        "access_token": "new-token", "refresh_token": "new-refresh",
    })
    monkeypatch.setattr(oauth, "_start_poller", lambda target, sid, **kw: target(sid))
    catalog = next(p for p in oauth._OAUTH_PROVIDER_CATALOG if p["id"] == "openai-codex")
    monkeypatch.setattr(oauth, "_build_oauth_catalog", lambda: [catalog])

    async def no_metric(*args):
        return None

    monkeypatch.setattr(oauth, "_begin_oauth_setup_metric", no_metric)
    client = TestClient(app)
    headers = {"X-Hermes-Session-Token": _SESSION_TOKEN}
    try:
        response = client.post(
            f"/api/providers/oauth/openai-codex/start?profile=owner&add_account={str(add_account).lower()}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        sid = response.json()["session_id"]
        assert client.get(f"/api/providers/oauth/openai-codex/poll/{sid}?profile=owner",
                          headers=headers).json()["status"] == "approved"
        rows = json.loads((fleet / "profiles" / "owner" / "auth.json").read_text()).get("credential_pool", {}).get("openai-codex", [])
        assert len(rows) == 1, "login must materialize the canonical account"
        _assert_shared(fleet, rows[0]["id"])
        for profile in ("default", "peer"):
            response = client.get(f"/api/providers/oauth?profile={profile}", headers=headers)
            assert response.status_code == 200, response.text
            accounts = response.json()["providers"][0]["accounts"]
            assert any(a["owner_credential_id"] == rows[0]["id"] for a in accounts)
            assert "new-token" not in response.text and "new-refresh" not in response.text
    finally:
        oauth._oauth_sessions.clear()


@pytest.mark.parametrize("command", ["add", "login"])
def test_cli_login_shares_account_without_overwriting_other_profile_controls(fleet, monkeypatch, command):
    from agent.credential_pool import load_pool
    from hermes_cli import auth_codex_browser
    monkeypatch.setattr(auth_codex_browser, "codex_oauth_login", lambda _: {
        "tokens": {"access_token": "new-token", "refresh_token": "new-refresh"},
    })
    token = set_hermes_home_override(fleet / "profiles" / "peer")
    try:
        live_pool = load_pool("openai-codex")
        assert live_pool.select() is None
    finally:
        reset_hermes_home_override(token)
    token = set_hermes_home_override(fleet / "profiles" / "owner")
    try:
        args = SimpleNamespace(provider="openai-codex", auth_type="oauth")
        if command == "add":
            auth_commands.auth_add_command(args)
        else:
            auth._login_openai_codex(args, None, force_new_login=True)
    finally:
        reset_hermes_home_override(token)
    rows = json.loads((fleet / "profiles" / "owner" / "auth.json").read_text())["credential_pool"]["openai-codex"]
    _assert_shared(fleet, rows[0]["id"])
    token = set_hermes_home_override(fleet / "profiles" / "peer")
    try:
        selected = live_pool.select()
        assert selected is not None, "running profiles must observe the new shared account"
        assert selected.extra["owner_credential_id"] == rows[0]["id"]
    finally:
        reset_hermes_home_override(token)
