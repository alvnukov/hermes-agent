"""Resolve Guardian's exact subscription identity without selection or failover.

JWT claims compare credentials already held by Hermes; they do not authenticate
tokens. Authentication remains the provider's job. Approval keys contain no secret.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import uuid

from agent.credential_pool import CredentialPool, PooledCredential, _codex_principal_identity
from hermes_cli import auth
from hermes_constants import get_hermes_home


@dataclass(frozen=True)
class CredentialIdentity:
    provider: str
    entry_id: str
    owner_entry_id: str
    owner_subject: str
    account_id: str
    identity_generation: str
    profile_scope: str
    assignment_store_scope: str
    auth_store_scope: str
    source: str
    base_url: str
    residency: str
    guardian_enabled: bool = False
    guardian_generation: str = ""
    owner_guardian_generation: str = ""


class PinnedCredentialError(RuntimeError):
    """An unavailable binding, with a safe reason and no provider error payload."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Guardian subscription credential is unavailable ({code}).")


def _path(path: Path) -> str:
    return str(path.resolve())


def _row(store: dict, provider: str, entry_id: str) -> dict:
    rows = store.get("credential_pool", {}).get(provider, [])
    matches = [r for r in rows if isinstance(r, dict) and r.get("id") == entry_id]
    if len(matches) != 1:
        raise PinnedCredentialError("entry_missing")
    return matches[0]


def _assignment(provider: str, entry_id: str) -> tuple[Path, dict]:
    """Read the active assignment, including root fallback, without auth caches."""
    local = auth._auth_file_path()
    with auth._auth_store_lock(target_path=local):
        store = auth._load_auth_store(local)
        rows = store.get("credential_pool", {}).get(provider, [])
        selected = store.get("credential_pool_selections", {}).get(provider)
        if rows or selected:
            return local, dict(_row(store, provider, entry_id))
    root = auth._global_auth_file_path()
    if root is None:
        raise PinnedCredentialError("entry_missing")
    with auth._auth_store_lock(target_path=root):
        return root, dict(_row(auth._load_auth_store(root), provider, entry_id))


def _owner(assignment_path: Path, row: dict) -> tuple[Path, str]:
    if row.get("source") != "shared":
        return assignment_path, row["id"]
    from agent.credential_accounts import _owner_home

    try:
        home = _owner_home(row.get("owner_profile"))
    except ValueError:
        raise PinnedCredentialError("owner_missing") from None
    owner_id = row.get("owner_credential_id")
    if not isinstance(owner_id, str) or not owner_id:
        raise PinnedCredentialError("owner_missing")
    return home / "auth.json", owner_id


def _enabled(entry: PooledCredential) -> None:
    if not entry.enabled or entry.last_status in {"dead", "exhausted"} or not entry.runtime_api_key:
        raise PinnedCredentialError("entry_unavailable")
    if entry.provider != "openai-codex" or entry.auth_type != "oauth":
        raise PinnedCredentialError("subscription_identity_missing")


def _claims(entry: PooledCredential) -> tuple[str, str, str]:
    principal = _codex_principal_identity(entry.access_token)
    if principal is None:
        raise PinnedCredentialError("subscription_identity_missing")
    payload = auth._decode_jwt_claims(entry.access_token)
    claims = payload.get("https://api.openai.com/auth", {})
    residency = claims.get("chatgpt_data_residency") or claims.get("chatgpt_compute_residency") or ""
    if not isinstance(residency, str):
        raise PinnedCredentialError("subscription_identity_missing")
    return principal[0], principal[1], residency.strip()


def _route(entry: PooledCredential) -> str:
    from hermes_cli.auth_codex import _codex_pool_route_base_url

    return _codex_pool_route_base_url(entry.base_url).rstrip("/")


def _identity(entry: PooledCredential, *, entry_id: str, assignment_path: Path,
              owner_path: Path, profile_scope: str, base_url: str,
              guardian_enabled: bool, guardian_generation: str = "") -> CredentialIdentity:
    _enabled(entry)
    account_id, subject, residency = _claims(entry)
    generation = entry.extra.get("identity_generation")
    if guardian_enabled and (not isinstance(generation, str) or not generation):
        raise PinnedCredentialError("identity_generation_missing")
    generation = generation if isinstance(generation, str) else ""
    return CredentialIdentity(
        entry.provider, entry_id, entry.id, subject, account_id, generation,
        profile_scope, _path(assignment_path), _path(owner_path), entry.source, base_url, residency,
        guardian_enabled, guardian_generation, entry.extra.get("guardian_generation", ""))


def _canonical(store: dict, provider: str, owner_id: str) -> PooledCredential:
    entry = PooledCredential.from_dict(provider, _row(store, provider, owner_id))
    if entry.source == "shared":
        raise PinnedCredentialError("owner_missing")
    # A singleton logout must remain binding even if an old pool row survived it.
    if entry.source == "device_code":
        state = store.get("providers", {}).get(provider, {})
        tokens = state.get("tokens", {})
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise PinnedCredentialError("entry_unavailable")
    _enabled(entry)
    return entry


def _migrate_generation(store: dict, entry: PooledCredential, owner_path: Path) -> PooledCredential:
    generation = entry.extra.get("identity_generation")
    if generation:
        return entry
    generation = uuid.uuid4().hex
    _row(store, entry.provider, entry.id)["identity_generation"] = generation
    if entry.source == "device_code":
        store["providers"][entry.provider]["identity_generation"] = generation
    auth._save_auth_store(store, target_path=owner_path)
    return replace(entry, extra={**entry.extra, "identity_generation": generation})


def guardian_requested_for_token(pool: CredentialPool | None, access_token: str) -> bool:
    """Preserve persisted opt-in even if the account can no longer prove its identity."""
    if pool is None or pool.provider != "openai-codex" or not access_token:
        return False
    requested = False
    for entry in pool.entries():
        if entry.runtime_api_key != access_token:
            continue
        requested = requested or entry.extra.get("guardian_enabled") is True
        try:
            _, assignment = _assignment(pool.provider, entry.id)
            requested = requested or assignment.get("guardian_enabled") is True
        except (PinnedCredentialError, OSError, TimeoutError):
            pass  # No identity proof can be minted; retain the last known opt-in only.
    return requested


def capture_identity(pool: CredentialPool | None, access_token: str,
                     base_url: str) -> CredentialIdentity | None:
    """Capture an unambiguous actual-client token; never consult the pool cursor.

    Legacy rows gain a durable random login generation while their store is locked.
    Missing persisted ownership, opaque tokens and API keys fail closed.
    """
    if pool is None or pool.provider != "openai-codex" or not access_token:
        return None
    matches = [entry for entry in pool.entries() if entry.runtime_api_key == access_token]
    if len(matches) != 1:
        return None
    observed = matches[0]
    try:
        _enabled(observed)
        principal = _claims(observed)
        assignment_path, assignment = _assignment(pool.provider, observed.id)
        if not assignment.get("enabled", True):
            return None
        owner_path, owner_id = _owner(assignment_path, assignment)
        route = _route(observed)
        if route != str(base_url).rstrip("/"):
            return None
        with auth._auth_store_lock(target_path=owner_path):
            store = auth._load_auth_store(owner_path)
            entry = _canonical(store, pool.provider, owner_id)
            if _claims(entry) != principal or _route(entry) != route:
                return None
            observed_generation = observed.extra.get("identity_generation")
            if observed_generation and observed_generation != entry.extra.get("identity_generation"):
                return None
            if not observed_generation and entry.access_token != access_token:
                return None
            guardian_enabled = assignment.get("guardian_enabled") is True
            if guardian_enabled:
                entry = _migrate_generation(store, entry, owner_path)
            return _identity(entry, entry_id=observed.id, assignment_path=assignment_path,
                             owner_path=owner_path, profile_scope=_path(get_hermes_home()), base_url=route,
                             guardian_enabled=guardian_enabled,
                             guardian_generation=assignment.get("guardian_generation", ""))
    except (PinnedCredentialError, OSError, TimeoutError):
        return None


def _validate_assignment(identity: CredentialIdentity) -> tuple[Path, dict]:
    path, assignment = _assignment(identity.provider, identity.entry_id)
    owner_path, owner_id = _owner(path, assignment)
    if (_path(path), _path(owner_path), owner_id) != (
            identity.assignment_store_scope, identity.auth_store_scope, identity.owner_entry_id):
        raise PinnedCredentialError("identity_changed")
    if not assignment.get("enabled", True):
        raise PinnedCredentialError("entry_unavailable")
    if assignment.get("guardian_enabled") is not True:
        raise PinnedCredentialError("guardian_disabled")
    if assignment.get("guardian_generation", "") != identity.guardian_generation:
        raise PinnedCredentialError("identity_changed")
    return owner_path, assignment


def _validate_entry(entry: PooledCredential, identity: CredentialIdentity) -> None:
    current = _identity(entry, entry_id=identity.entry_id,
                        assignment_path=Path(identity.assignment_store_scope),
                        owner_path=Path(identity.auth_store_scope), profile_scope=identity.profile_scope,
                        base_url=identity.base_url, guardian_enabled=identity.guardian_enabled,
                        guardian_generation=identity.guardian_generation)
    if current != identity:
        raise PinnedCredentialError("identity_changed")


def resolve_pinned(pool: CredentialPool, identity: CredentialIdentity, *,
                   refresh: bool = True) -> PooledCredential:
    """Resolve only the approved row, observing controls and serialized OAuth refresh.

    A fresh one-row pool avoids touching the main pool's leases/cursor, and avoids
    taking its lock while holding a cross-process auth-store lock. Assignment and
    owner locks are held separately to avoid cycles between sharing profiles.
    """
    if pool.provider != identity.provider or _path(get_hermes_home()) != identity.profile_scope:
        raise PinnedCredentialError("profile_changed")
    if not identity.guardian_enabled:
        raise PinnedCredentialError("guardian_disabled")
    try:
        owner_path, _assignment_row = _validate_assignment(identity)
        # Route settings belong to the calling profile, even for borrowed accounts.
        with auth._auth_store_lock(target_path=owner_path):
            entry = _canonical(auth._load_auth_store(owner_path), identity.provider, identity.owner_entry_id)
        if _route(entry) != identity.base_url:
            raise PinnedCredentialError("identity_changed")
        from agent.credential_accounts import _owner_scope

        with _owner_scope(owner_path.parent), auth._auth_store_lock(target_path=owner_path):
            store = auth._load_auth_store(owner_path)
            entry = _canonical(store, identity.provider, identity.owner_entry_id)
            _validate_entry(entry, identity)
            pinned_pool = CredentialPool(identity.provider, [entry])
            pinned_pool._persisted_token_pairs = auth._token_pairs_by_id([entry.to_dict()])
            # Singleton synchronization can adopt a fresh login before refresh;
            # verify that grant too, before spending its refresh token.
            entry = pinned_pool._sync_entry_from_auth_store(entry)
            _validate_entry(entry, identity)
            if refresh and pinned_pool._entry_needs_refresh(entry):
                entry = pinned_pool._refresh_entry(entry, force=False)
                if entry is None:
                    raise PinnedCredentialError("refresh_failed")
                _validate_entry(entry, identity)
            if pinned_pool._entry_needs_refresh(entry):
                raise PinnedCredentialError("token_expired")
            persisted = _canonical(auth._load_auth_store(owner_path), identity.provider, identity.owner_entry_id)
            _validate_entry(persisted, identity)
            if persisted.access_token != entry.access_token:
                raise PinnedCredentialError("refresh_not_persisted")
        _validate_assignment(identity)
        return replace(entry, id=identity.entry_id)
    except PinnedCredentialError:
        raise
    except (OSError, TimeoutError, ValueError):
        raise PinnedCredentialError("auth_store_unavailable") from None
