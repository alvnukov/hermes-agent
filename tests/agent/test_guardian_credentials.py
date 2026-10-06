"""Guardian approvals belong to the authenticated owner, workspace and login grant."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import threading
import time

import pytest

from agent.credential_pool import CredentialPool, PooledCredential
from hermes_cli import auth


BASE_URL = "https://chatgpt.com/backend-api/codex"


def _jwt(account="workspace-a", subject="owner-a", *, exp=None, residency="eu", nonce="old"):
    payload = {"sub": subject, "exp": int(exp or time.time() + 3600), "nonce": nonce,
               "https://api.openai.com/auth": {
                   "chatgpt_account_id": account, "chatgpt_data_residency": residency}}
    segment = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"e30.{segment}.sig"


def _row(credential_id="a", **updates):
    return {"id": credential_id, "label": credential_id, "auth_type": "oauth", "priority": 0,
            "source": "manual:device_code", "access_token": _jwt(), "refresh_token": "fake-refresh-a",
            "base_url": BASE_URL, "enabled": True, "identity_generation": "login-a",
            "guardian_enabled": True, "guardian_generation": "guardian-a", **updates}


def _save(home, rows, *, providers=None):
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({
        "version": 1, "providers": providers or {}, "credential_pool": {"openai-codex": rows},
        "credential_pool_selections": {"openai-codex": True}}), encoding="utf-8")


def _pool(rows):
    pool = CredentialPool("openai-codex", [PooledCredential.from_dict("openai-codex", r) for r in rows])
    pool._persisted_token_pairs = auth._token_pairs_by_id(rows)
    return pool


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_capture_uses_exact_transport_token_and_contains_no_secrets(home):
    from agent.guardian_credentials import capture_identity

    rows = [_row(), _row("b", access_token=_jwt("workspace-b", "owner-b"), priority=1)]
    _save(home, rows)
    pool = _pool(rows)
    pool._current_id = "b"
    identity = capture_identity(pool, rows[0]["access_token"], BASE_URL + "/")

    assert identity.entry_id == "a"
    assert identity.owner_subject == "owner-a"
    assert identity.account_id == "workspace-a"
    assert identity.identity_generation == "login-a"
    assert identity.residency == "eu"
    assert identity.base_url == BASE_URL
    assert identity.auth_store_scope == str(home / "auth.json")
    assert identity.profile_scope == str(home)
    assert rows[0]["access_token"] not in repr(identity)
    assert "fake-refresh-a" not in repr(identity)
    with pytest.raises(FrozenInstanceError):
        identity.entry_id = "b"


def test_capture_refuses_ambiguous_unowned_or_non_subscription_credentials(home):
    from agent.guardian_credentials import capture_identity

    rows = [_row(), _row("b")]
    _save(home, rows)
    assert capture_identity(_pool(rows), rows[0]["access_token"], BASE_URL) is None
    for row in (_row(access_token="opaque"), _row(auth_type="api_key"), _row(access_token=_jwt(subject=""))):
        _save(home, [row])
        assert capture_identity(_pool([row]), row["access_token"], BASE_URL) is None


@pytest.mark.parametrize("mutation", ["disabled", "deleted", "logged_out", "dead", "owner", "workspace",
                                       "generation", "residency", "base_url"])
def test_pinned_resolution_reads_live_store_and_never_rotates(home, mutation):
    from agent.guardian_credentials import PinnedCredentialError, capture_identity, resolve_pinned

    rows = [_row(), _row("b", access_token=_jwt("workspace-b", "owner-b"), priority=1)]
    _save(home, rows)
    pool = _pool(rows)
    identity = capture_identity(pool, rows[0]["access_token"], BASE_URL)
    changes = {
        "disabled": lambda: rows[0].update(enabled=False),
        "deleted": lambda: rows.pop(0),
        "logged_out": lambda: rows[0].update(access_token="", refresh_token=""),
        "dead": lambda: rows[0].update(last_status="dead"),
        "owner": lambda: rows[0].update(access_token=_jwt(subject="other-owner")),
        "workspace": lambda: rows[0].update(access_token=_jwt(account="other-workspace")),
        "generation": lambda: rows[0].update(identity_generation="login-b"),
        "residency": lambda: rows[0].update(access_token=_jwt(residency="us")),
        "base_url": lambda: rows[0].update(base_url="https://different.example/codex"),
    }
    changes[mutation]()
    _save(home, rows)
    pool._current_id = "b"

    with pytest.raises(PinnedCredentialError) as failure:
        resolve_pinned(pool, identity, refresh=False)
    assert pool._current_id == "b"
    assert all(row["access_token"] not in str(failure.value) for row in rows if row["access_token"])


def test_peer_refresh_keeps_identity_and_does_not_select_current_account(home):
    from agent.guardian_credentials import capture_identity, resolve_pinned

    rows = [_row(), _row("b", access_token=_jwt("workspace-b", "owner-b"))]
    _save(home, rows)
    pool = _pool(rows)
    identity = capture_identity(pool, rows[0]["access_token"], BASE_URL)
    rows[0].update(access_token=_jwt(nonce="peer"), refresh_token="fake-refresh-peer")
    _save(home, rows)
    pool._current_id = "b"

    resolved = resolve_pinned(pool, identity)

    assert resolved.id == "a"
    assert resolved.access_token == rows[0]["access_token"]
    assert resolved.refresh_token == "fake-refresh-peer"
    assert pool._current_id == "b"


def test_concurrent_pinned_refresh_rotates_only_once_and_preserves_generation(home, monkeypatch):
    from agent.guardian_credentials import capture_identity, resolve_pinned

    row = _row(access_token=_jwt(exp=time.time() + 10))
    _save(home, [row])
    pool = _pool([row])
    identity = capture_identity(pool, row["access_token"], BASE_URL)
    new_token = _jwt(nonce="refreshed")
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def refresh(access_token, refresh_token):
        calls.append(refresh_token)
        entered.set()
        assert release.wait(5)
        return {"access_token": new_token, "refresh_token": "fake-refresh-b", "last_refresh": "2026-10-06T00:00:00Z"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", refresh)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(resolve_pinned, pool, identity)
        assert entered.wait(5)
        second = workers.submit(resolve_pinned, pool, identity)
        release.set()
        resolved = [first.result(timeout=10), second.result(timeout=10)]

    assert calls == ["fake-refresh-a"]
    assert all(entry.access_token == new_token for entry in resolved)
    assert all(entry.extra["identity_generation"] == "login-a" for entry in resolved)
    persisted = json.loads((home / "auth.json").read_text())["credential_pool"]["openai-codex"][0]
    assert persisted["identity_generation"] == "login-a"
    assert persisted["access_token"] == new_token


def test_legacy_capture_persists_generation_and_fresh_reauth_invalidates_it(home):
    from agent.guardian_credentials import PinnedCredentialError, capture_identity, resolve_pinned

    row = _row(source="device_code")
    row.pop("identity_generation")
    singleton = {"tokens": {"access_token": row["access_token"], "refresh_token": row["refresh_token"]},
                 "auth_mode": "chatgpt"}
    _save(home, [row], providers={"openai-codex": singleton})
    pool = _pool([row])
    identity = capture_identity(pool, row["access_token"], BASE_URL)
    assert identity.identity_generation
    auth._save_codex_tokens({"access_token": _jwt(nonce="reauth"), "refresh_token": "fake-new-login"})

    with pytest.raises(PinnedCredentialError):
        resolve_pinned(pool, identity, refresh=False)


def test_explicit_pool_reauth_changes_generation_but_stale_flush_cannot_restore_it(home):
    from agent.guardian_credentials import PinnedCredentialError, capture_identity, resolve_pinned

    row = _row()
    _save(home, [row])
    pool = _pool([row])
    identity = capture_identity(pool, row["access_token"], BASE_URL)
    replacement = {**row, "access_token": _jwt(nonce="reauth"), "refresh_token": "fake-new-login"}
    auth.write_credential_pool("openai-codex", [replacement])
    pool._persist()

    with pytest.raises(PinnedCredentialError):
        resolve_pinned(pool, identity, refresh=False)


def test_identity_cannot_cross_profile_scope(home, monkeypatch):
    from agent.guardian_credentials import PinnedCredentialError, capture_identity, resolve_pinned

    row = _row()
    _save(home, [row])
    pool = _pool([row])
    identity = capture_identity(pool, row["access_token"], BASE_URL)
    other = home / "profiles" / "other"
    _save(other, [row])
    monkeypatch.setenv("HERMES_HOME", str(other))

    with pytest.raises(PinnedCredentialError):
        resolve_pinned(pool, identity, refresh=False)


@pytest.mark.parametrize("replacement", ["owner", "workspace", "residency"])
def test_refresh_cannot_change_the_approved_principal(home, monkeypatch, replacement):
    from agent.guardian_credentials import PinnedCredentialError, capture_identity, resolve_pinned

    row = _row(access_token=_jwt(exp=time.time() + 10))
    _save(home, [row])
    pool = _pool([row])
    identity = capture_identity(pool, row["access_token"], BASE_URL)
    new_token = {"owner": _jwt(subject="other-owner"), "workspace": _jwt(account="other-workspace"),
                 "residency": _jwt(residency="us")}[replacement]
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", lambda *_: {
        "access_token": new_token, "refresh_token": "fake-refresh-b", "last_refresh": "2026-10-06T00:00:00Z"})

    with pytest.raises(PinnedCredentialError) as failure:
        resolve_pinned(pool, identity)
    assert failure.value.code == "identity_changed"


def test_borrowed_root_binding_does_not_follow_a_new_local_account(home, monkeypatch):
    from agent.guardian_credentials import PinnedCredentialError, capture_identity, resolve_pinned

    row = _row()
    _save(home, [row])
    profile = home / "profiles" / "caller"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    pool = _pool([row])
    pool._borrowed_root_ids.add("a")
    identity = capture_identity(pool, row["access_token"], BASE_URL)
    assert identity.auth_store_scope == str(home / "auth.json")
    assert resolve_pinned(pool, identity).id == "a"
    _save(profile, [row])

    with pytest.raises(PinnedCredentialError):
        resolve_pinned(pool, identity)


def test_shared_binding_follows_only_its_live_owner_row(home, monkeypatch):
    from agent.credential_accounts import hydrate_reference
    from agent.guardian_credentials import PinnedCredentialError, capture_identity, resolve_pinned

    owner = home / "profiles" / "owner"
    caller = home / "profiles" / "caller"
    row = _row()
    _save(owner, [row])
    reference = {"id": "assignment", "label": "linked", "priority": 0, "enabled": True,
                 "source": "shared", "auth_type": "oauth", "owner_profile": "owner", "owner_credential_id": "a",
                 "guardian_enabled": True}
    _save(caller, [reference])
    monkeypatch.setenv("HERMES_HOME", str(caller))
    pool = _pool([reference])
    pool._entries = [hydrate_reference(pool.entries()[0])]
    identity = capture_identity(pool, row["access_token"], BASE_URL)
    assert identity.entry_id == "assignment"
    assert identity.owner_entry_id == "a"
    assert identity.auth_store_scope == str(owner / "auth.json")
    row.update(access_token=_jwt(nonce="peer"), refresh_token="fake-refresh-peer")
    _save(owner, [row])
    resolved = resolve_pinned(pool, identity)
    assert resolved.id == "assignment"
    assert resolved.access_token == row["access_token"]
    row["enabled"] = False
    _save(owner, [row])
    with pytest.raises(PinnedCredentialError):
        resolve_pinned(pool, identity)


def test_normal_singleton_refresh_preserves_login_generation(home):
    from agent.guardian_credentials import capture_identity, resolve_pinned

    row = _row(source="device_code")
    singleton = {"tokens": {"access_token": row["access_token"], "refresh_token": row["refresh_token"]},
                 "auth_mode": "chatgpt", "identity_generation": "login-a"}
    _save(home, [row], providers={"openai-codex": singleton})
    pool = _pool([row])
    identity = capture_identity(pool, row["access_token"], BASE_URL)
    new_token = _jwt(nonce="normal-refresh")
    auth._save_codex_tokens({"access_token": new_token, "refresh_token": "fake-rotated"}, write_through=True)

    assert resolve_pinned(pool, identity).access_token == new_token


def test_capture_refuses_a_transport_that_no_longer_matches_the_persisted_route(home):
    from agent.guardian_credentials import capture_identity

    row = _row()
    _save(home, [row])
    pool = _pool([row])
    _save(home, [{**row, "base_url": "https://different.example/codex"}])

    assert capture_identity(pool, row["access_token"], BASE_URL) is None


def test_guardian_is_opt_in_and_disabling_revokes_pending_binding(home):
    from agent.guardian_credentials import PinnedCredentialError, capture_identity, resolve_pinned

    row = _row()
    row.pop("guardian_enabled")
    _save(home, [row])
    pool = _pool([row])
    unchecked = capture_identity(pool, row["access_token"], BASE_URL)
    assert unchecked.guardian_enabled is False
    with pytest.raises(PinnedCredentialError) as failure:
        resolve_pinned(pool, unchecked)
    assert failure.value.code == "guardian_disabled"
    pool.set_guardian_enabled("a", True)
    checked = capture_identity(pool, row["access_token"], BASE_URL)
    assert checked.guardian_enabled is True
    assert checked.identity_generation == unchecked.identity_generation
    pool.set_guardian_enabled("a", False)
    with pytest.raises(PinnedCredentialError) as failure:
        resolve_pinned(pool, checked)
    assert failure.value.code == "guardian_disabled"


def test_guardian_toggle_survives_stale_token_rotation_and_is_assignment_local(home, monkeypatch):
    from agent.credential_accounts import account_metadata, hydrate_reference, update_account
    from agent.credential_pool import load_pool

    row = _row(guardian_enabled=False)
    _save(home, [row])
    stale_pool = _pool([row])
    update_account("openai-codex", "a", guardian_enabled=True)
    stale_pool._persist()
    assert account_metadata(load_pool("openai-codex"), load_pool("openai-codex").entries()[0])["guardian_enabled"] is True
    caller = home / "profiles" / "caller"
    reference = {"id": "assignment", "label": "linked", "priority": 0, "enabled": True,
                 "source": "shared", "auth_type": "oauth", "owner_profile": "default", "owner_credential_id": "a"}
    _save(caller, [reference])
    monkeypatch.setenv("HERMES_HOME", str(caller))
    shared = hydrate_reference(PooledCredential.from_dict("openai-codex", reference))
    assert shared.extra.get("guardian_enabled", False) is False
    updated = update_account("openai-codex", "assignment", guardian_enabled=True)
    assert updated.extra["guardian_enabled"] is True
    update_account("openai-codex", "assignment", guardian_enabled=False)
    root_row = json.loads((home / "auth.json").read_text())["credential_pool"]["openai-codex"][0]
    assert root_row["guardian_enabled"] is True


def test_unchecked_capture_does_not_write_or_materialize_identity_metadata(home):
    from agent.guardian_credentials import capture_identity

    row = _row()
    for field in ("identity_generation", "guardian_enabled", "guardian_generation"):
        row.pop(field)
    _save(home, [row])
    before = (home / "auth.json").read_bytes()

    identity = capture_identity(_pool([row]), row["access_token"], BASE_URL)

    assert identity.guardian_enabled is False
    assert identity.identity_generation == ""
    assert (home / "auth.json").read_bytes() == before


def test_reenabling_guardian_does_not_restore_an_old_approval_binding(home):
    from agent.guardian_credentials import PinnedCredentialError, capture_identity, resolve_pinned

    row = _row()
    _save(home, [row])
    pool = _pool([row])
    old = capture_identity(pool, row["access_token"], BASE_URL)
    pool.set_guardian_enabled("a", False)
    pool.set_guardian_enabled("a", True)
    new = capture_identity(pool, row["access_token"], BASE_URL)

    assert new.guardian_generation != old.guardian_generation
    assert new.identity_generation == old.identity_generation
    assert resolve_pinned(pool, new).id == "a"
    with pytest.raises(PinnedCredentialError):
        resolve_pinned(pool, old)


@pytest.mark.parametrize("target", ["account", "shared_assignment", "shared_owner"])
def test_reenabling_account_revokes_pending_binding_in_each_ownership_scope(home, monkeypatch, target):
    from agent.credential_accounts import _owner_scope, hydrate_reference, update_account
    from agent.guardian_credentials import PinnedCredentialError, capture_identity, resolve_pinned

    row = _row()
    if target == "account":
        _save(home, [row])
        pool = _pool([row])
        settings_home, settings_id = home, "a"
    else:
        owner = home / "profiles" / "owner"
        caller = home / "profiles" / "caller"
        reference = {"id": "assignment", "label": "linked", "priority": 0, "enabled": True,
                     "source": "shared", "auth_type": "oauth", "owner_profile": "owner",
                     "owner_credential_id": "a", "guardian_enabled": True, "guardian_generation": "assignment-a"}
        _save(owner, [row])
        _save(caller, [reference])
        monkeypatch.setenv("HERMES_HOME", str(caller))
        pool = _pool([reference])
        pool._entries = [hydrate_reference(pool.entries()[0])]
        settings_home, settings_id = (owner, "a") if target == "shared_owner" else (caller, "assignment")
    old = capture_identity(pool, row["access_token"], BASE_URL)
    assert resolve_pinned(pool, old).access_token == row["access_token"]

    with _owner_scope(settings_home):
        update_account("openai-codex", settings_id, enabled=False)
        update_account("openai-codex", settings_id, enabled=True)
    # A pre-toggle runtime flush must not restore a revoked control generation.
    pool._persist()
    new = capture_identity(pool, row["access_token"], BASE_URL)

    assert new != old
    assert new.identity_generation == old.identity_generation
    assert resolve_pinned(pool, new).access_token == row["access_token"]
    with pytest.raises(PinnedCredentialError) as failure:
        resolve_pinned(pool, old)
    assert failure.value.code == "identity_changed"
