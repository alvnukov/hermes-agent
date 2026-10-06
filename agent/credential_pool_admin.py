"""Locked credential-pool administration and target resolution."""
from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.credential_pool import PooledCredential


def _cleared_status_copy(entry: PooledCredential) -> PooledCredential:
    from agent.credential_pool import _CLEAR_STATUS

    # The reset marker lets a live pool in another process tell "reset after my cooldown" from
    # "never had a status" — both read as bare None on disk (#89415).
    return replace(entry, **_CLEAR_STATUS, model_cooldowns=None, status_cleared_at=time.time(),
                   extra={k: v for k, v in entry.extra.items() if k != "failure_reason"})


def sync_entry_controls(entry: PooledCredential, disk: dict) -> PooledCredential:
    """Adopt peer assignment settings while leaving a live token pair untouched."""
    updates = {"enabled": disk.get("enabled", True), "label": disk.get("label", entry.label),
               "priority": disk.get("priority", entry.priority)}
    extra = dict(entry.extra)
    for key, default in (("guardian_enabled", False), ("guardian_generation", "")):
        value = disk.get(key, default)
        if extra.get(key, default) != value:
            extra[key] = value
    if extra != entry.extra:
        updates["extra"] = extra
    if any(getattr(entry, key) != value for key, value in updates.items()):
        return replace(entry, **updates)
    return entry


def sync_shared_accounts(pool, entries, rows):
    """Let a running profile use newly published assignments on its next selection."""
    from agent.credential_accounts import hydrate_reference
    from agent.credential_pool import PooledCredential
    from hermes_cli.auth import _credential_token_pair

    known = {entry.id for entry in entries}
    pinned = getattr(pool, "account_id", None)
    for credential_id, row in rows.items():
        if pinned is not None and credential_id != pinned:
            continue
        if credential_id not in known and row.get("source") == "shared":
            entries.append(hydrate_reference(PooledCredential.from_dict(pool.provider, row)))
            pool._persisted_token_pairs[credential_id] = _credential_token_pair(row)
    return sorted(entries, key=lambda entry: entry.priority)


class CredentialPoolAdminMixin:
    def set_guardian_enabled(self, credential_id: str, enabled: bool) -> Optional[PooledCredential]:
        """Change the approval backend for this assignment without copying its tokens."""
        if self.provider != "openai-codex":
            raise ValueError("Guardian requires a Codex subscription account")
        if not isinstance(enabled, bool):
            raise ValueError("Guardian enabled must be a boolean")
        with self._lock:
            self._sync_live_controls()
            entry = self._find(lambda e: e.id == credential_id)
            if entry is None:
                return None
            extra = {**entry.extra, "guardian_enabled": enabled}
            if entry.extra.get("guardian_enabled", False) != enabled or not extra.get("guardian_generation"):
                extra["guardian_generation"] = uuid.uuid4().hex
            updated = replace(entry, extra=extra)
            self._replace_entry(entry, updated)
            self._persist(control_updated_ids=[credential_id])
            return self._find(lambda e: e.id == credential_id)

    def reset_status(self, credential_id: str) -> Optional[PooledCredential]:
        """Clear only the target's local error state, preserving sibling cooldowns."""
        with self._lock:
            entry = self._find(lambda e: e.id == credential_id)
            if entry is None:
                return None
            cleared = _cleared_status_copy(entry)
            self._replace_entry(entry, cleared)
            self._persist(status_cleared_ids=[cleared.id])
            return cleared
    def reset_statuses(self) -> int:
        """Clear exhaustion state on every entry. Returns how many were cleared.

        ``failure_reason`` lives in ``extra``, not a dataclass field, so it is
        stripped explicitly. The persist declares the cleared ids because the
        disk-recency merge reads a cleared ``last_status_at`` (None -> epoch 0)
        as a stale snapshot and would copy a still-binding cooldown back.
        """
        from agent.credential_pool import _CLEAR_STATUS

        with self._lock:
            stale = [
                e for e in self._entries
                if e.last_status or e.last_status_at or e.last_error_code or e.failure_reason or e.model_cooldowns
            ]
            if stale:
                stale_ids = {e.id for e in stale}
                self._entries = [
                    _cleared_status_copy(e) if e.id in stale_ids else e
                    for e in self._entries
                ]
                self._persist(status_cleared_ids=list(stale_ids))
            return len(stale)

    def remove_index(self, index: int) -> Optional[PooledCredential]:
        with self._lock:
            if index < 1 or index > len(self._entries):
                return None
            removed = self._entries.pop(index - 1)
            self._entries = [replace(e, priority=p) for p, e in enumerate(self._entries)]
            self._persist(removed_ids=[removed.id], control_updated_ids=[e.id for e in self._entries])
            if self._current_id == removed.id:
                self._current_id = None
            return removed

    def move_entry(self, credential_id: str, priority: int) -> Optional[PooledCredential]:
        """Place an entry at a clamped zero-based position and persist contiguous priorities."""
        from agent.credential_pool import _normalize_pool_priorities

        with self._lock:
            entry = self._find(lambda e: e.id == credential_id)
            if entry is None:
                return None
            others = [e for e in self._entries if e.id != credential_id]
            others.insert(max(0, min(int(priority), len(others))), entry)
            entries = [replace(e, priority=p) for p, e in enumerate(others)]
            # Apply load-time ordering now so the reported position survives reload.
            _normalize_pool_priorities(self.provider, entries)
            self._entries = sorted(entries, key=lambda e: e.priority)
            self._persist(control_updated_ids=[e.id for e in self._entries])
            return self._find(lambda e: e.id == credential_id)

    def resolve_target(self, target: Any) -> Tuple[Optional[int], Optional[PooledCredential], Optional[str]]:
        raw = str(target or "").strip()
        if not raw:
            return None, None, "No credential target provided."

        with self._lock:
            for idx, entry in enumerate(self._entries, start=1):
                if entry.id == raw:
                    return idx, entry, None

            label_matches = [
                (idx, entry)
                for idx, entry in enumerate(self._entries, start=1)
                if entry.label.strip().lower() == raw.lower()
            ]
            if len(label_matches) == 1:
                return label_matches[0][0], label_matches[0][1], None
            if len(label_matches) > 1:
                return None, None, f'Ambiguous credential label "{raw}". Use the numeric index or entry id instead.'
            if raw.isdigit():
                index = int(raw)
                if 1 <= index <= len(self._entries):
                    return index, self._entries[index - 1], None
                return None, None, f"No credential #{index}."
            return None, None, f'No credential matching "{raw}".'

    def add_entry(self, entry: PooledCredential) -> PooledCredential:
        from agent.credential_pool import _next_priority, _profile_owns_pool_provider, write_credential_pool
        from hermes_cli import auth as auth_mod

        with self._lock:
            entry = replace(entry, priority=_next_priority(self._entries))
            self._entries.append(entry)
            borrowed_ids = getattr(self, "_borrowed_root_ids", None)
            if borrowed_ids or not _profile_owns_pool_provider(self.provider):
                # ``hermes -p <profile> auth add <single-use provider>``: the
                # profile claims its OWN credential. Persist only profile-owned
                # rows — copying the borrowed root grant alongside would fork
                # its single-use refresh token (#100339). Once the profile owns
                # rows, the root fallback for this provider is shadowed.
                # A first explicit add also claims ownership when there are no
                # borrowed rows; the ordinary persist path is update-only at root.
                self._entries = [e for e in self._entries if e.id not in (borrowed_ids or set())]
                written = write_credential_pool(
                    self.provider, [e.to_dict() for e in self._entries],
                    token_bases=self._persisted_token_pairs,
                )
                self._persisted_token_pairs = auth_mod._token_pairs_by_id(written)
                self._borrowed_root_ids = set()
            else:
                self._persist(control_updated_ids=[entry.id])
            return entry
