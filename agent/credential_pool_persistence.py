"""Persist pool rows without copying borrowed single-use grants into profiles."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

def _store_owns_pool_provider(auth_store: Dict[str, Any], provider: str) -> bool:
    """True when an already-loaded *auth_store* has its own rows for *provider*."""
    pool = auth_store.get("credential_pool")
    entries = pool.get(provider) if isinstance(pool, dict) else None
    return (isinstance(entries, list) and bool(entries)) or bool(
        auth_store.get("credential_pool_selections", {}).get(provider))


def _profile_owns_pool_provider(provider: str) -> bool:
    """True when the ACTIVE auth.json has its own rows for *provider*.

    Named profiles with no local rows read the provider through the
    ``read_credential_pool`` global-root fallback ("borrowing").
    """
    from agent import credential_pool as _pool
    # Classic mode (profile == root) has no root fallback, so the answer is always "owns";
    # skip the per-call auth.json re-read on this hot load_pool path.
    if _pool.auth_mod._global_auth_file_path() is None:
        return True
    try:
        auth_store = _pool._load_auth_store()
    except Exception:
        return True  # unreadable store: assume ownership, keep legacy path
    return _pool._store_owns_pool_provider(auth_store, provider)


def _update_root_pool_rows(
    provider: str, payloads: List[Dict[str, Any]], global_path: Path,
    *, status_cleared_ids: Optional[Iterable[str]] = None,
    token_bases: Optional[Dict[str, Tuple[Any, Any]]] = None,
) -> List[Dict[str, Any]]:
    """UPDATE-ONLY merge of *payloads* into the root store's rows for *provider*.

    A borrower may refresh the root's rows (rotation, cooldown state) but
    never add or delete them — the root owns their lifecycle. In particular a
    profile's singleton-prune (it has no ``.anthropic_oauth.json`` of its own)
    must not delete the root grant, so ``removed_ids`` is ignored by callers.
    """
    from agent import credential_pool as _pool
    with _pool._auth_store_lock(target_path=global_path):
        store = _pool._load_auth_store(global_path)
        pool = store.get("credential_pool")
        if not isinstance(pool, dict):
            pool = {}
            store["credential_pool"] = pool
        existing = pool.get(provider)
        existing_list = existing if isinstance(existing, list) else []
        incoming_by_id = _pool.auth_mod._entry_ids(payloads)
        cleared = {cid for cid in (status_cleared_ids or ()) if cid}
        bases = token_bases or {}
        merged: List[Dict[str, Any]] = []
        changed = False
        for disk_entry in existing_list:
            did = disk_entry.get("id") if isinstance(disk_entry, dict) else None
            incoming = incoming_by_id.get(did) if did else None
            if incoming is None:
                merged.append(disk_entry)
                continue
            updated = _pool.auth_mod._merge_pool_row_generation(
                incoming, disk_entry, provider,
                base_pair=bases.get(did), status_cleared=did in cleared,
            )
            for key in ("enabled", "label", "priority", "source", "owner_profile", "owner_credential_id"):
                if key in disk_entry:
                    updated[key] = disk_entry[key]
                else:
                    updated.pop(key, None)
            if updated != disk_entry:
                changed = True
            merged.append(updated)
        if changed:
            pool[provider] = merged
            _pool._save_auth_store(store, target_path=global_path)
        return merged


def persist_pool_entries(
    provider: str,
    payloads: List[Dict[str, Any]],
    *,
    removed_ids: Optional[Iterable[str]] = None,
    status_cleared_ids: Optional[Iterable[str]] = None,
    token_bases: Optional[Dict[str, Tuple[Any, Any]]] = None,
    control_updated_ids: Optional[Iterable[str]] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Persist a provider's pool rows to the store that OWNS them.

    A named profile that sees a single-use-refresh provider (see
    ``SINGLE_USE_REFRESH_POOL_PROVIDERS``) only through the global-root fallback must not
    materialize a local ``credential_pool.<provider>`` copy: that copy forks
    the single-use refresh token, the first profile to rotate commits the new
    pair only to its own file, and root plus every sibling die with
    ``invalid_grant`` (#100339). Such rows are written back to the root store
    (under the root lock); everything else goes to the active store.
    """
    from agent import credential_pool as _pool
    if provider in _pool.SINGLE_USE_REFRESH_POOL_PROVIDERS and not _pool._profile_owns_pool_provider(provider):
        global_path = _pool._borrowed_single_use_pool_root()
        if global_path is not None:
            try:
                return _update_root_pool_rows(
                    provider, payloads, global_path,
                    status_cleared_ids=status_cleared_ids, token_bases=token_bases,
                )
            except Exception as exc:
                # Fail closed on the FORK, not on the save: never fall back to
                # writing a local copy (that IS the bug). The in-memory pool
                # still holds the rotated pair for this process.
                _pool.logger.warning(
                    "%s pool: write-through of borrowed root grant failed (%s); "
                    "not materializing a profile-local copy",
                    provider, exc,
                )
            return None
    return _pool.write_credential_pool(
        provider, payloads, removed_ids=removed_ids, status_cleared_ids=status_cleared_ids,
        token_bases=token_bases,
        control_updated_ids=control_updated_ids,
    )
