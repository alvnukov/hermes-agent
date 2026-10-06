"""Credential pool writes preserve peer lifecycle changes and token generations."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple
import uuid


def _pool_identity_generation(entry, disk, *, explicit):
    """Keep a login identity across refresh, replace it on an explicit new grant."""
    if (not isinstance(entry, dict) or entry.get("auth_type") != "oauth"
            or not (entry.get("access_token") or entry.get("refresh_token"))):
        return entry
    entry = dict(entry)
    old = disk.get("identity_generation") if isinstance(disk, dict) else None
    changed = explicit and disk is not None and (
        entry.get("access_token"), entry.get("refresh_token")) != (
        disk.get("access_token"), disk.get("refresh_token"))
    generation = uuid.uuid4().hex if changed else (old or entry.get("identity_generation"))
    if generation or explicit:
        entry["identity_generation"] = generation or uuid.uuid4().hex
    return entry


def _stamp_codex_generations(provider_id, entries, existing_by_id, *, explicit):
    if provider_id != "openai-codex":
        return entries
    return [_pool_identity_generation(e, existing_by_id.get(e.get("id")), explicit=explicit)
            if isinstance(e, dict) else e for e in entries]

def write_credential_pool(
    provider_id: str, entries: List[Dict[str, Any]], *,
    removed_ids: Optional[Iterable[str]] = None,
    status_cleared_ids: Optional[Iterable[str]] = None,
    token_bases: Optional[Dict[str, Tuple[Any, Any]]] = None,
    control_updated_ids: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Persist one provider's credential pool under auth.json.

    Final disk-boundary sanitizer for borrowed credentials (callers may pass raw dicts). Entries on
    disk but missing from *entries* (added concurrently) are merged back unless in *removed_ids*,
    so a rotation/exhaustion rewrite never drops a concurrent credential. Entries in
    *status_cleared_ids* were cleared deliberately (``hermes auth reset``) and skip the
    recency merge, which would otherwise read their cleared ``last_status_at`` (None ->
    epoch 0) as a stale snapshot and copy a still-binding cooldown back."""
    from hermes_cli import auth as _auth
    removed = {rid for rid in (removed_ids or ()) if rid}
    bases = token_bases or {}
    with _auth._auth_store_lock():
        auth_store = _auth._load_auth_store()
        pool = _auth._store_section(auth_store, "credential_pool")
        sanitized = [
            _auth.sanitize_borrowed_credential_payload(e, provider_id) if isinstance(e, dict) else e
            for e in entries]
        existing_list = pool.get(provider_id)
        existing_list = existing_list if isinstance(existing_list, list) else []
        existing_by_id = _auth._entry_ids(existing_list)
        sanitized = _stamp_codex_generations(provider_id, sanitized, existing_by_id,
                                             explicit=token_bases is None)
        tombstones = _auth._store_section(auth_store, "credential_pool_removed")
        deleted = set(tombstones.get(provider_id, [])) | removed
        controls = set(control_updated_ids or ())
        # Runtime snapshots carry known token bases. Keep lifecycle changes made by a peer.
        if token_bases is not None:
            safe = []
            for entry in sanitized:
                if not isinstance(entry, dict):
                    continue
                cid = entry.get("id")
                if cid in deleted:
                    continue
                entry = dict(entry)
                disk = existing_by_id.get(cid)
                if disk is not None and cid not in controls:
                    for key in ("enabled", "label", "priority", "source", "owner_profile", "owner_credential_id",
                                "guardian_enabled", "guardian_generation"):
                        if key in disk:
                            entry[key] = disk[key]
                        else:
                            entry.pop(key, None)
                safe.append(_auth.sanitize_borrowed_credential_payload(entry, provider_id))
            sanitized = safe
        else:
            # An explicit add may deliberately reuse a stable singleton identity.
            deleted.difference_update(_auth._entry_ids(sanitized))
        new_ids = set(_auth._entry_ids(sanitized))
        status_cleared = {cid for cid in (status_cleared_ids or ()) if cid}
        merged: List[Dict[str, Any]] = [
            _auth._merge_pool_row_generation(
                e, existing_by_id.get(e.get("id")), provider_id,
                base_pair=bases.get(e.get("id")),
                status_cleared=e.get("id") in status_cleared,
            )
            if isinstance(e, dict) else e
            for e in sanitized]
        for disk_entry in existing_list:
            disk_id = disk_entry.get("id") if isinstance(disk_entry, dict) else None
            if disk_id and disk_id not in new_ids and disk_id not in removed:
                merged.append(_auth.sanitize_borrowed_credential_payload(disk_entry, provider_id))
        pool[provider_id] = merged
        if deleted:
            tombstones[provider_id] = sorted(deleted)
        else:
            tombstones.pop(provider_id, None)
        _auth._save_auth_store(auth_store)
        return merged
