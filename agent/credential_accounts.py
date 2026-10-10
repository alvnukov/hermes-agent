"""Account lifecycle and tokenless references to a profile's canonical pool row."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import uuid

from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from hermes_cli import auth
from agent.credential_persistence import is_borrowed_credential_source


def _root_home():
    home = get_hermes_home()
    return home.parent.parent if home.parent.name == "profiles" else home


def _profile_name():
    home = get_hermes_home()
    return home.name if home.parent.name == "profiles" else "default"


def _owner_home(profile):
    from hermes_cli.profiles import _PROFILE_ID_RE, named_profile_is_live
    if profile == "default":
        return _root_home()
    if not isinstance(profile, str) or not _PROFILE_ID_RE.fullmatch(profile):
        raise ValueError("Invalid owner profile")
    home = _root_home() / "profiles" / profile
    if not named_profile_is_live(home) or home.is_symlink():
        raise ValueError("Owner profile not found")
    return home


@contextmanager
def _owner_scope(home):
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    home_token = set_hermes_home_override(home)
    secret_token = None
    try:
        secret_token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
        yield
    finally:
        if secret_token is not None:
            reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def _raw_rows(provider):
    pool = auth._load_auth_store().get("credential_pool", {})
    rows = pool.get(provider, []) if isinstance(pool, dict) else []
    return rows if isinstance(rows, list) else []


def _owned_row(provider, credential_id):
    row = next((r for r in _raw_rows(provider) if isinstance(r, dict) and r.get("id") == credential_id), None)
    if row is None or is_borrowed_credential_source(row.get("source"), provider):
        raise ValueError("Owned account not found")
    return row


def _materialize_singleton(provider):
    """Give a locally owned legacy singleton a stable, persisted pool identity."""
    from agent.credential_pool import PooledCredential, _seed_from_singletons
    local = auth._load_auth_store()
    providers = local.get("providers", {})
    has_local = isinstance(providers, dict) and bool(providers.get(provider))
    if provider == "anthropic":
        has_local = (get_hermes_home() / ".anthropic_oauth.json").is_file()
    if not has_local:
        return
    entries = [PooledCredential.from_dict(provider, r) for r in _raw_rows(provider) if isinstance(r, dict)]
    changed, _sources = _seed_from_singletons(provider, entries)
    if changed:
        owned = [e.to_dict() for e in entries if not is_borrowed_credential_source(e.source, provider)]
        auth.write_credential_pool(provider, owned)


def _reference(entry, owner):
    """Hydrate runtime data while preserving this profile's assignment controls."""
    from agent.credential_pool import PooledCredential
    canonical = PooledCredential.from_dict(entry.provider, owner)
    extra = {**entry.extra, **canonical.extra, "owner_profile": entry.extra.get("owner_profile"),
             "owner_credential_id": entry.extra.get("owner_credential_id"), "_owner_enabled": canonical.enabled}
    if "failure_reason" not in canonical.extra:
        extra.pop("failure_reason", None)
    extra.pop("shared_unavailable", None)
    if not canonical.enabled:
        extra["shared_unavailable"] = "owner_disabled"
    return replace(canonical, id=entry.id, label=entry.label, priority=entry.priority,
                   source="shared", enabled=entry.enabled, extra=extra)


def hydrate_reference(entry):
    if entry.source != "shared":
        return entry
    try:
        home = _owner_home(entry.extra.get("owner_profile"))
        with _owner_scope(home):
            owner = _owned_row(entry.provider, entry.extra.get("owner_credential_id"))
        return _reference(entry, owner)
    except (ValueError, OSError):
        return replace(entry, access_token="", refresh_token=None, agent_key=None,
                       extra={**entry.extra, "shared_unavailable": "owner_missing", "_owner_enabled": False})


def account_metadata(pool, entry):
    inherited = entry.id in getattr(pool, "_borrowed_root_ids", set())
    inherited_owned = inherited and entry.source != "shared"
    shared = entry.source == "shared" or inherited
    live = hydrate_reference(entry) if entry.source == "shared" else entry
    reason = live.extra.get("shared_unavailable")
    configured = bool(live.enabled)
    owner_enabled = bool(live.extra.get("_owner_enabled", True))
    return {"id": live.id, "label": live.label, "priority": live.priority,
            "enabled": configured and owner_enabled and not reason,
            "configured_enabled": configured, "owner_enabled": owner_enabled,
            "shared": shared, "owner_profile": "default" if inherited_owned else
            live.extra.get("owner_profile", _profile_name()),
            "owner_credential_id": live.id if inherited_owned else live.extra.get("owner_credential_id", live.id),
            "last_status": live.last_status, "last_error_reset_at": live.last_error_reset_at,
            "missing": reason == "owner_missing", "unavailable_reason": reason}


def _account_homes():
    root = _root_home()
    profiles = [("default", root)]
    directory = root / "profiles"
    if directory.is_dir():
        from hermes_cli.profiles import _PROFILE_ID_RE, named_profile_is_live
        profiles += [(p.name, p) for p in sorted(directory.iterdir())
                     if p.name != "default" and _PROFILE_ID_RE.fullmatch(p.name)
                     and not p.is_symlink() and named_profile_is_live(p)]
    return profiles


def list_available_accounts(provider):
    from agent.credential_pool import CredentialPool, PooledCredential
    result = []
    for profile, home in _account_homes():
        with _owner_scope(home):
            if not _raw_rows(provider):
                with auth._auth_store_lock():
                    _materialize_singleton(provider)
            for raw in _raw_rows(provider):
                if not isinstance(raw, dict) or not raw.get("id") or is_borrowed_credential_source(raw.get("source"), provider):
                    continue
                entry = PooledCredential.from_dict(provider, raw)
                metadata = account_metadata(CredentialPool(provider, [entry]), entry)
                metadata["owner_profile"] = profile
                result.append(metadata)
    return result


def _select_explicitly(provider):
    store = auth._load_auth_store()
    store.setdefault("credential_pool_selections", {})[provider] = True
    auth._save_auth_store(store)


def _local_assignments(provider):
    """Claim inherited root rows as tokenless assignments before local administration."""
    from agent.credential_pool import PooledCredential, load_pool
    pool = load_pool(provider)
    if not _raw_rows(provider) and auth._global_auth_file_path() is not None:
        entries = []
        for entry in pool.entries():
            if entry.id in pool._borrowed_root_ids and entry.source != "shared":
                entries.append(PooledCredential.from_dict(provider, {
                    "id": entry.id, "label": entry.label, "priority": entry.priority,
                    "enabled": entry.enabled, "auth_type": entry.auth_type, "source": "shared",
                    "owner_profile": "default", "owner_credential_id": entry.id,
                }))
            else:
                entries.append(entry)
        if entries:
            auth.write_credential_pool(provider, [e.to_dict() for e in entries])
            _select_explicitly(provider)
            pool = load_pool(provider)
    return pool


def _prepare_inherited_assignment(provider):
    """Persist a borrowed singleton's identity before taking the target profile lock."""
    if _raw_rows(provider) or auth._global_auth_file_path() is None:
        return
    with _owner_scope(_root_home()), auth._auth_store_lock():
        _materialize_singleton(provider)


def account_pool(provider):
    """Prepare an additive login without dropping inherited account assignments."""
    _prepare_inherited_assignment(provider)
    with auth._auth_store_lock():
        pool = _local_assignments(provider)
        _select_explicitly(provider)
        return pool


def finalize_account_login(provider):
    """Materialize a freshly saved Codex singleton and share it with every profile."""
    from agent.credential_pool import PooledCredential, _Seeder, _seed_tokens_singleton
    from agent.credential_pool_admin import _cleared_status_copy

    if provider != "openai-codex":
        raise ValueError("Normal account login finalization is supported only for Codex")
    with auth._auth_store_lock():
        store = auth._load_auth_store()
        # A normal login is a deliberate new grant; removal must not keep suppressing its seed.
        auth.unsuppress_credential_source(provider, "device_code")
        entries = [PooledCredential.from_dict(provider, row) for row in _raw_rows(provider)
                   if isinstance(row, dict)]
        _seed_tokens_singleton(_Seeder(provider, entries), store)
        canonical = next((entry for entry in entries if entry.source == "device_code"), None)
        if canonical is None:
            raise ValueError("Saved account singleton not found")
        canonical = replace(_cleared_status_copy(canonical), enabled=True)
        # Write only the canonical row; the auth boundary retains independent/shared siblings.
        auth.write_credential_pool(provider, [canonical.to_dict()], status_cleared_ids=[canonical.id])
        _select_explicitly(provider)
    share_codex_account(canonical.id)


def share_codex_account(credential_id):
    """Publish tokenless assignments without holding locks across profile boundaries."""
    owner = _profile_name()
    _owned_row("openai-codex", credential_id)
    for profile, home in _account_homes():
        if profile != owner:
            with _owner_scope(home):
                link_account("openai-codex", owner, credential_id)


def link_account(provider, owner_profile, credential_id):
    from agent.credential_pool import PooledCredential
    target_home = get_hermes_home()
    owner_home = _owner_home(owner_profile)
    if owner_home.resolve() == target_home.resolve():
        raise ValueError("Account already belongs to this profile")
    _prepare_inherited_assignment(provider)
    with _owner_scope(owner_home), auth._auth_store_lock():
        _materialize_singleton(provider)
        owner = dict(_owned_row(provider, credential_id))
    with auth._auth_store_lock():
        _local_assignments(provider)
        entries = _raw_rows(provider)
        existing = next((r for r in entries if r.get("source") == "shared" and
                         r.get("owner_profile") == owner_profile and r.get("owner_credential_id") == credential_id), None)
        if existing is not None:
            return hydrate_reference(PooledCredential.from_dict(provider, existing))
        reference = PooledCredential.from_dict(provider, {
            "id": uuid.uuid4().hex[:12], "label": owner.get("label", credential_id),
            "priority": len(entries), "auth_type": owner.get("auth_type", "api_key"),
            "source": "shared", "owner_profile": owner_profile, "owner_credential_id": credential_id,
        })
        auth.write_credential_pool(provider, [*entries, reference.to_dict()])
        _select_explicitly(provider)
    return hydrate_reference(reference)


def update_account(provider, credential_id, *, label=None, enabled=None, priority=None):
    _prepare_inherited_assignment(provider)
    with auth._auth_store_lock():
        pool = _local_assignments(provider)
        entry = pool._find(lambda e: e.id == credential_id)
        if entry is None:
            raise ValueError("Account not found")
        if label is not None and (not isinstance(label, str) or not label.strip()):
            raise ValueError("Account label must not be empty")
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError("Enabled must be a boolean")
        updates = {}
        if label is not None:
            updates["label"] = label.strip()
        if enabled is not None:
            updates["enabled"] = enabled
        if updates:
            pool._replace_entry(entry, replace(entry, **updates))
            pool._persist(control_updated_ids=[entry.id])
        if priority is not None:
            pool.move_entry(credential_id, priority)
        _select_explicitly(provider)
        return pool._find(lambda e: e.id == credential_id)


def remove_account(provider, credential_id):
    _prepare_inherited_assignment(provider)
    with auth._auth_store_lock():
        pool = _local_assignments(provider)
        index = next((i for i, e in enumerate(pool.entries(), 1) if e.id == credential_id), None)
        if index is None:
            raise ValueError("Account not found")
        removed = pool.remove_index(index)
        if removed is None:
            raise ValueError("Account not found")
        _select_explicitly(provider)
        if removed.source == "shared":
            return
        from agent.credential_sources import find_removal_step
        step = find_removal_step(provider, removed.source)
        if step is not None:
            result = step.remove_fn(provider, removed)
            if result.suppress:
                auth.suppress_credential_source(provider, removed.source)


def persist_reference_status(provider, entries, *, status_cleared_ids=()):
    """Apply runtime observations to the physical owner; never copy reference tokens."""
    for entry in entries:
        if entry.source != "shared" or entry.extra.get("shared_unavailable"):
            continue
        try:
            home = _owner_home(entry.extra.get("owner_profile"))
            with _owner_scope(home), auth._auth_store_lock():
                owner = dict(_owned_row(provider, entry.extra.get("owner_credential_id")))
                observed = dict(owner)
                for key in (*auth._POOL_STATUS_FIELDS, "model_cooldowns", "request_count"):
                    observed[key] = getattr(entry, key)
                if entry.extra.get("failure_reason"):
                    observed["failure_reason"] = entry.extra["failure_reason"]
                auth.write_credential_pool(provider, [observed], token_bases=auth._token_pairs_by_id([owner]),
                    status_cleared_ids=[owner["id"]] if entry.id in status_cleared_ids else [])
        except (ValueError, OSError):
            continue


def refresh_reference(pool, entry, *, force=False, probe=False):
    """Lock BEFORE reading the owner row and hold through refresh/probe persistence."""
    from agent.credential_pool import CredentialPool, PooledCredential
    if not entry.enabled:
        return False if probe else None
    try:
        home = _owner_home(entry.extra.get("owner_profile"))
        with _owner_scope(home), auth._auth_store_lock(timeout_seconds=pool._single_use_refresh_lock_timeout()):
            owner = _owned_row(pool.provider, entry.extra.get("owner_credential_id"))
            canonical = PooledCredential.from_dict(pool.provider, owner)
            if not canonical.enabled:
                return False if probe else None
            owner_pool = CredentialPool(pool.provider, [canonical])
            owner_pool._persisted_token_pairs = auth._token_pairs_by_id([owner])
            if probe:
                result = owner_pool._codex_quota_restored_upstream(canonical)
                updated = owner_pool.entries()[0]
            else:
                # A peer refreshed while we waited: its pair is authoritative even for force.
                if (canonical.access_token, canonical.refresh_token) != (entry.access_token, entry.refresh_token):
                    updated = canonical
                else:
                    updated = owner_pool._refresh_entry(canonical, force=force)
                result = updated
            hydrated = _reference(entry, updated.to_dict()) if updated is not None else None
        # Never acquire the target pool's lock while holding another profile's auth lock.
        if hydrated is not None:
            pool._replace_entry(entry, hydrated)
        return result if probe else hydrated
    except (ValueError, OSError):
        pool._replace_entry(entry, hydrate_reference(entry))
        return False if probe else None
