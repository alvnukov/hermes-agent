"""Account administration and profile references exercise real temporary auth stores."""
import json
import time
from pathlib import Path

import pytest

from agent.credential_pool import PooledCredential, load_pool
from hermes_cli import auth
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def homes(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    for name in ("a", "b"):
        (root / "profiles" / name).mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None
    auth._global_auth_store_cache = None
    return root


def row(cid="one", **overrides):
    return {"id": cid, "label": cid, "priority": 0, "source": "manual",
            "auth_type": "api_key", "access_token": "test-secret-" + cid, **overrides}


def store(home, provider="openrouter", rows=()):
    (home / "auth.json").write_text(json.dumps({"credential_pool": {provider: list(rows)}}))


def disk(home, provider="openrouter"):
    return json.loads((home / "auth.json").read_text())["credential_pool"][provider]


def scope(home):
    from contextlib import contextmanager
    @contextmanager
    def bound():
        token = set_hermes_home_override(home)
        try:
            yield
        finally:
            reset_hermes_home_override(token)
    return bound()


def test_enabled_defaults_and_disabled_never_selected(homes):
    store(homes, rows=[row(enabled=False)])
    pool = load_pool("openrouter")
    assert PooledCredential.from_dict("openrouter", row()).enabled is True
    assert pool.select() is None
    assert pool.peek() is None
    assert pool.acquire_lease("one") is None


def test_management_survives_stale_flush_and_preserves_cooldown(homes):
    from agent.credential_accounts import update_account, remove_account
    reset = time.time() + 3600
    store(homes, rows=[row(last_status="exhausted", last_status_at=time.time(),
                          last_error_code=429, last_error_reset_at=reset)])
    stale = load_pool("openrouter")
    update_account("openrouter", "one", label="Renamed", enabled=False)
    stale._persist()
    persisted = disk(homes)[0]
    assert persisted["enabled"] is False
    assert persisted["label"] == "Renamed"
    update_account("openrouter", "one", enabled=True)
    assert disk(homes)[0]["last_error_reset_at"] == reset
    remove_account("openrouter", "one")
    stale._persist()
    assert disk(homes) == []
    assert stale.select() is None


def test_link_any_profile_tokenless_and_owner_status_writeback(homes):
    from agent.credential_accounts import account_metadata, link_account, list_available_accounts
    a, b = homes / "profiles/a", homes / "profiles/b"
    store(a, rows=[row()])
    store(b, rows=[row("local")])
    with scope(b):
        available = list_available_accounts("openrouter")
        assert any(item["owner_profile"] == "a" and item["id"] == "one" for item in available)
        linked = link_account("openrouter", "a", "one")
        pool = load_pool("openrouter")
        entry = next(e for e in pool.entries() if e.id == linked.id)
        assert entry.runtime_api_key == "test-secret-one"
        assert account_metadata(pool, entry)["shared"] is True
        pool._mark_exhausted(entry, 429, {"reason": "rate_limit", "reset_at": time.time() + 900})
    reference = next(item for item in disk(b) if item["source"] == "shared")
    assert "access_token" not in reference and "refresh_token" not in reference
    assert reference["owner_profile"] == "a" and reference["owner_credential_id"] == "one"
    assert disk(a)[0]["last_status"] == "exhausted"
    assert len(disk(b)) == 2


def test_owner_disable_delete_observed_by_live_borrower(homes):
    from agent.credential_accounts import account_metadata, link_account, remove_account, update_account
    a, b = homes / "profiles/a", homes / "profiles/b"
    store(a, rows=[row()])
    with scope(b):
        linked = link_account("openrouter", "a", "one")
        pool = load_pool("openrouter")
        assert pool.select() is not None
    with scope(a):
        update_account("openrouter", "one", enabled=False)
    with scope(b):
        assert pool.select() is None
        assert pool.current() is None
        assert pool.acquire_lease(linked.id) is None
    with scope(a):
        remove_account("openrouter", "one")
    with scope(b):
        pool = load_pool("openrouter")
        assert len(pool.entries()) == 1
        assert pool.select() is None
        assert account_metadata(pool, pool.entries()[0])["missing"] is True


def test_unlink_last_reference_blocks_root_fallback(homes):
    from agent.credential_accounts import link_account, remove_account
    store(homes, rows=[row("root")])
    a = homes / "profiles/a"
    with scope(a):
        linked = link_account("openrouter", "default", "root")
        remove_account("openrouter", linked.id)
        assert auth.read_credential_pool("openrouter") == []
        assert load_pool("openrouter").select() is None
    assert disk(homes)[0]["id"] == "root"


def test_references_fail_closed_and_cannot_form_chains(homes):
    from agent.credential_accounts import link_account
    a, b = homes / "profiles/a", homes / "profiles/b"
    store(a, rows=[row()])
    with scope(b):
        linked = link_account("openrouter", "a", "one")
        with pytest.raises(ValueError):
            link_account("openrouter", "../outside", "one")
        with pytest.raises(ValueError):
            link_account("openrouter", "a", "unknown")
    with scope(a), pytest.raises(ValueError):
        link_account("openrouter", "b", linked.id)


def test_shared_refresh_reloads_owner_under_lock_and_never_copies_tokens(homes, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from agent.credential_accounts import link_account
    from agent.credential_pool import CredentialPool
    a, b = homes / "profiles/a", homes / "profiles/b"
    store(a, "openai-codex", [row(auth_type="oauth", access_token="old", refresh_token="refresh-old")])
    calls = []
    monkeypatch.setattr(CredentialPool, "_entry_needs_refresh", lambda self, entry: entry.access_token == "old")
    def rotate(access, refresh):
        calls.append(refresh)
        return {"access_token": "new", "refresh_token": "refresh-new", "last_refresh": "2026-10-05T00:00:00Z"}
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", rotate)
    with scope(b):
        link_account("openai-codex", "a", "one")
        pools = [load_pool("openai-codex"), load_pool("openai-codex")]
    def refresh(pool):
        with scope(b):
            return pool._refresh_entry(pool.entries()[0], force=False)
    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(refresh, pools))
    assert calls == ["refresh-old"]
    assert all(entry.access_token == "new" for entry in results)
    assert disk(a, "openai-codex")[0]["refresh_token"] == "refresh-new"
    assert "refresh_token" not in disk(b, "openai-codex")[0]


def test_inherited_root_management_is_local_assignment(homes):
    from agent.credential_accounts import account_metadata, remove_account, update_account
    store(homes, rows=[row("first"), row("second", priority=1)])
    a = homes / "profiles/a"
    with scope(a):
        pool = load_pool("openrouter")
        assert account_metadata(pool, pool.entries()[0])["shared"] is True
        update_account("openrouter", "first", enabled=False, label="Local label")
        assert pool.select().id == "second"
        assert load_pool("openrouter").select().id == "second"
        remove_account("openrouter", "second")
        assert pool.select() is None
        assert load_pool("openrouter").select() is None
    assert disk(homes)[0].get("enabled", True) is True
    assert disk(homes)[0]["label"] == "first"
    assert len(disk(homes)) == 2
    assert all("access_token" not in item for item in disk(a))


def test_cross_profile_references_with_distinct_owned_accounts_do_not_recurse(homes):
    from agent.credential_accounts import link_account
    a, b = homes / "profiles/a", homes / "profiles/b"
    store(a, rows=[row("a")])
    store(b, rows=[row("b")])
    with scope(a):
        link_account("openrouter", "b", "b")
    with scope(b):
        link_account("openrouter", "a", "a")
        assert {e.runtime_api_key for e in load_pool("openrouter").entries()} == {"test-secret-a", "test-secret-b"}
    with scope(a):
        assert {e.runtime_api_key for e in load_pool("openrouter").entries()} == {"test-secret-a", "test-secret-b"}


def test_legacy_owner_singleton_gets_stable_identity_before_link(homes):
    from agent.credential_accounts import link_account, list_available_accounts
    a, b = homes / "profiles/a", homes / "profiles/b"
    (a / "auth.json").write_text(json.dumps({"providers": {"openai-codex": {
        "tokens": {"access_token": "legacy-access", "refresh_token": "legacy-refresh"}}}}))
    with scope(b):
        available = list_available_accounts("openai-codex")
        owned = next(item for item in available if item["owner_profile"] == "a")
        link_account("openai-codex", "a", owned["id"])
        assert load_pool("openai-codex").entries()[0].runtime_api_key == "legacy-access"
    assert disk(a, "openai-codex")[0]["id"] == owned["id"]
    assert disk(b, "openai-codex")[0]["owner_credential_id"] == owned["id"]


def test_assignment_disable_preserves_owner_and_priority_survives_stale_flush(homes):
    from agent.credential_accounts import account_metadata, link_account, update_account
    a, b = homes / "profiles/a", homes / "profiles/b"
    store(a, rows=[row()])
    store(b, rows=[row("local")])
    with scope(b):
        linked = link_account("openrouter", "a", "one")
        stale = load_pool("openrouter")
        update_account("openrouter", linked.id, enabled=False, priority=0)
        stale._persist()
        pool = load_pool("openrouter")
        assert pool.entries()[0].id == linked.id
        metadata = account_metadata(pool, pool.entries()[0])
        assert metadata["configured_enabled"] is False and metadata["owner_enabled"] is True
    assert disk(a)[0].get("enabled", True) is True


def test_shared_quota_probe_rotates_only_owner_and_keeps_reference_tokenless(homes, monkeypatch):
    from agent.credential_accounts import link_account
    a, b = homes / "profiles/a", homes / "profiles/b"
    store(a, "openai-codex", [row(auth_type="oauth", access_token="old", refresh_token="refresh-old",
        last_status="exhausted", last_status_at=time.time(), last_error_code=429,
        last_error_reason="quota_exhausted", last_error_reset_at=time.time() + 900)])
    rotations = []
    def refresh(token, refresh_token):
        rotations.append(refresh_token)
        return {"access_token": "new", "refresh_token": "refresh-new"}
    monkeypatch.setattr(auth, "_refresh_expired_codex_probe_token", refresh)
    monkeypatch.setattr(auth, "_probe_codex_quota_restored", lambda *args, **kwargs: True)
    with scope(b):
        link_account("openai-codex", "a", "one")
        pool = load_pool("openai-codex")
        assert pool._codex_quota_restored_upstream(pool.entries()[0]) is True
    assert rotations == ["refresh-old"]
    assert disk(a, "openai-codex")[0]["refresh_token"] == "refresh-new"
    assert "access_token" not in disk(b, "openai-codex")[0]


@pytest.mark.parametrize("probe", [False, True])
def test_local_manual_codex_refresh_adopts_rotated_pool_generation(homes, monkeypatch, probe):
    from agent.credential_pool import CredentialPool
    store(homes, "openai-codex", [row(auth_type="oauth", access_token="old", refresh_token="refresh-old",
        last_status="exhausted" if probe else None, last_status_at=time.time(),
        last_error_code=429 if probe else None, last_error_reason="quota_exhausted" if probe else None,
        last_error_reset_at=time.time() + 900 if probe else None)])
    calls = []
    def rotate(access, refresh):
        if access != "old":
            return None
        calls.append(refresh)
        return {"access_token": "new", "refresh_token": "refresh-new"}
    monkeypatch.setattr(CredentialPool, "_entry_needs_refresh", lambda self, entry: entry.access_token == "old")
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", rotate)
    monkeypatch.setattr(auth, "_refresh_expired_codex_probe_token", rotate)
    monkeypatch.setattr(auth, "_probe_codex_quota_restored", lambda *args, **kwargs: True)
    pools = [load_pool("openai-codex"), load_pool("openai-codex")]
    for pool in pools:
        if probe:
            assert pool._codex_quota_restored_upstream(pool.entries()[0]) is True
        else:
            assert pool._refresh_entry(pool.entries()[0], force=False).access_token == "new"
    assert calls == ["refresh-old"]


def test_disabled_cooldown_does_not_offer_recovery_time(homes):
    store(homes, rows=[row(enabled=False, last_status="exhausted", last_status_at=time.time(),
                          last_error_code=429, last_error_reset_at=time.time() + 900)])
    assert load_pool("openrouter").next_available_at() is None


def test_remove_account_requires_exact_id(homes):
    from agent.credential_accounts import remove_account
    store(homes, rows=[row(label="nonexistent-id")])
    with pytest.raises(ValueError):
        remove_account("openrouter", "nonexistent-id")
    assert disk(homes)[0]["id"] == "one"


def test_inherited_root_reference_keeps_physical_owner_during_management(homes):
    from agent.credential_accounts import account_metadata, link_account, update_account
    a, b = homes / "profiles/a", homes / "profiles/b"
    store(a, rows=[row()])
    linked = link_account("openrouter", "a", "one")
    with scope(b):
        pool = load_pool("openrouter")
        metadata = account_metadata(pool, pool.entries()[0])
        assert metadata["owner_profile"] == "a"
        assert metadata["owner_credential_id"] == "one"
        update_account("openrouter", linked.id, label="Inherited name")
        selected = load_pool("openrouter").select()
        assert selected is not None and selected.runtime_api_key == "test-secret-one"
    assert disk(b)[0]["owner_profile"] == "a"
    assert disk(b)[0]["owner_credential_id"] == "one"
    assert disk(homes)[0]["label"] == "one"


def test_additive_login_preserves_inherited_oauth_as_tokenless_assignment(homes):
    from agent.credential_accounts import account_pool
    from hermes_cli.auth_commands import store_oauth_credential
    provider = "openai-codex"
    store(homes, provider, [row(auth_type="oauth", source="manual:device_code", refresh_token="root-refresh")])
    before = (homes / "auth.json").read_bytes()
    a = homes / "profiles/a"
    with scope(a):
        pool = account_pool(provider)
        assert len(pool.entries()) == 1
        store_oauth_credential(provider, {"tokens": {"access_token": "new-access", "refresh_token": "new-refresh"}}, pool=pool)
        entries = load_pool(provider).entries()
        assert len(entries) == 2
        assert any(e.source == "shared" and e.extra["owner_credential_id"] == "one" for e in entries)
    reference = next(item for item in disk(a, provider) if item["source"] == "shared")
    assert "access_token" not in reference and "refresh_token" not in reference
    assert (homes / "auth.json").read_bytes() == before


def test_link_preserves_inherited_oauth_as_tokenless_assignment(homes):
    from agent.credential_accounts import link_account
    provider = "openai-codex"
    store(homes, provider, [row("root", auth_type="oauth", source="manual:device_code", refresh_token="root-refresh")])
    a, b = homes / "profiles/a", homes / "profiles/b"
    store(a, provider, [row("other", auth_type="oauth", source="manual:device_code", refresh_token="other-refresh")])
    root_before = (homes / "auth.json").read_bytes()
    a_before = (a / "auth.json").read_bytes()
    with scope(b):
        link_account(provider, "a", "other")
        entries = load_pool(provider).entries()
        assert len(entries) == 2
        assert {e.extra["owner_credential_id"] for e in entries} == {"root", "other"}
        assert {e.runtime_api_key for e in entries} == {"test-secret-root", "test-secret-other"}
    assert all("access_token" not in item and "refresh_token" not in item for item in disk(b, provider))
    assert (homes / "auth.json").read_bytes() == root_before
    assert (a / "auth.json").read_bytes() == a_before


def test_normal_login_reconnects_after_deleting_last_managed_singleton(homes):
    from agent.credential_accounts import finalize_account_login, remove_account
    provider = "openai-codex"
    auth._save_codex_tokens({"access_token": "first-access", "refresh_token": "first-refresh"})
    entry = load_pool(provider).entries()[0]
    remove_account(provider, entry.id)
    assert load_pool(provider).entries() == []
    assert auth.is_source_suppressed(provider, "device_code") is True
    auth._save_codex_tokens({"access_token": "new-access", "refresh_token": "new-refresh"})
    finalize_account_login(provider)
    entries = load_pool(provider).entries()
    assert len(entries) == 1
    assert entries[0].source == "device_code" and entries[0].enabled is True
    assert entries[0].access_token == "new-access" and entries[0].refresh_token == "new-refresh"
    assert auth.is_source_suppressed(provider, "device_code") is False
    assert load_pool(provider).select() is not None


def test_normal_login_enables_only_canonical_singleton_in_managed_selection(homes):
    from agent.credential_accounts import finalize_account_login, link_account
    provider = "openai-codex"
    a, b = homes / "profiles/a", homes / "profiles/b"
    store(a, provider, [
        row("canonical", source="device_code", auth_type="oauth", enabled=False,
            access_token="old-access", refresh_token="old-refresh", last_status="dead"),
        row("independent", source="manual:device_code", auth_type="oauth", enabled=False,
            refresh_token="independent-refresh", last_status="exhausted",
            last_error_reset_at=time.time() + 900),
    ])
    store(b, provider, [row("owner", auth_type="oauth", refresh_token="owner-refresh")])
    owner_before = (b / "auth.json").read_bytes()
    with scope(a):
        linked = link_account(provider, "b", "owner")
        entries_before = {e["id"]: e for e in disk(a, provider)}
        auth._save_codex_tokens({"access_token": "new-access", "refresh_token": "new-refresh"})
        finalize_account_login(provider)
        entries_after = {e["id"]: e for e in disk(a, provider)}
        assert entries_after["canonical"]["enabled"] is True
        assert entries_after["canonical"]["last_status"] is None
        assert entries_after["independent"] == entries_before["independent"]
        assert entries_after[linked.id] == entries_before[linked.id]
    assert (b / "auth.json").read_bytes() == owner_before


def test_normal_login_finalizer_leaves_unmanaged_legacy_store_unchanged(homes):
    from agent.credential_accounts import finalize_account_login
    auth._save_codex_tokens({"access_token": "legacy-access", "refresh_token": "legacy-refresh"})
    before = (homes / "auth.json").read_bytes()
    finalize_account_login("openai-codex")
    assert (homes / "auth.json").read_bytes() == before


def test_explicit_readd_of_stable_id_survives_runtime_persist(homes):
    provider = "openrouter"
    store(homes, provider, [row()])
    auth.write_credential_pool(provider, [], removed_ids=["one"])
    auth.write_credential_pool(provider, [row(access_token="readded-access")])
    pool = load_pool(provider)
    pool._adopt(pool.entries()[0], request_count=1, persist=False)
    pool._persist()
    persisted = disk(homes, provider)
    assert len(persisted) == 1
    assert persisted[0]["id"] == "one"
    assert persisted[0]["access_token"] == "readded-access"
    assert persisted[0].get("request_count", 0) == 1
