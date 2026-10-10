"""Restoring auth must keep re-added live accounts writable after token rotation."""

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("provider", ["openai-codex", "anthropic", "xai-oauth", "nous"])
def test_snapshot_restore_keeps_readded_live_account_token_updates(
    tmp_path, monkeypatch, provider
):
    from hermes_cli.auth import read_credential_pool, write_credential_pool
    from hermes_cli.backup import restore_quick_snapshot

    home = tmp_path / "scratch-profile"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    live = {
        "id": "readded-account",
        "auth_type": "oauth",
        "source": "manual",
        "access_token": "access-live",
        "refresh_token": "refresh-live",
    }
    write_credential_pool(provider, [live])
    write_credential_pool(
        provider, [], removed_ids=[live["id"], "still-deleted-account"]
    )
    snap_dir = home / "state-snapshots" / "before-readd"
    snap_dir.mkdir(parents=True)
    snapshot_auth = (home / "auth.json").read_text(encoding="utf-8")
    (snap_dir / "auth.json").write_text(snapshot_auth, encoding="utf-8")
    (snap_dir / "manifest.json").write_text(
        json.dumps({"files": {"auth.json": len(snapshot_auth.encode())}}),
        encoding="utf-8",
    )
    write_credential_pool(provider, [live])

    assert restore_quick_snapshot("before-readd", hermes_home=home) is True
    assert read_credential_pool(provider)[0]["id"] == live["id"]
    rotated = {
        **live,
        "access_token": "access-rotated",
        "refresh_token": "refresh-rotated",
    }
    write_credential_pool(
        provider,
        [rotated],
        token_bases={live["id"]: (live["access_token"], live["refresh_token"])},
    )

    row = read_credential_pool(provider)[0]
    assert (row["access_token"], row["refresh_token"]) == (
        rotated["access_token"], rotated["refresh_token"]
    )
    restored = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    assert restored["credential_pool_removed"][provider] == ["still-deleted-account"]
