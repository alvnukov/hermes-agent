"""Profile-create route isolation (HERMES_PROFILE_CREATE_ISOLATED=1).

The Default board's bootstrap gate requires ``hermes profile create`` to be able
to run without touching ANYTHING outside the three new profile trees:
(a) no contact with the default root's gateway/multiplexer (pid/lock/runtime
    records read, control-socket rescan) — ``create_profile``'s publish-time
    ``_notify_multiplexer`` and the CLI's served-profiles hint block;
(b) no unlink inside the shared ``profiles/.deleted`` tombstone registry
    (``clear_named_profile_deleted``) — ``create`` must not modify the shared
    registry for a fresh name whose tombstone cannot exist.
"""

import argparse
from pathlib import Path

import pytest

from hermes_cli import profiles


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Same isolation fixture as test_profiles.py: Path.home() -> tmp_path,
    HERMES_HOME -> tmp_path/.hermes."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def _isolated_env(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE_CREATE_ISOLATED", "1")


class TestCreateIsolationEnvGate:
    """``create_profile`` must be steerable into a no-external-route mode."""

    def test_isolated_create_skips_multiplexer_notify(self, profile_env, monkeypatch):
        import hermes_cli.gateway_multiplex_served as gms
        calls = []
        monkeypatch.setattr(gms, "live_default_gateway_pid",
                            lambda: 12345, raising=False)
        monkeypatch.setattr(
            gms, "rescan_gateway_profiles",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("rescan reached")),
            raising=False)
        monkeypatch.setattr(profiles, "_notify_multiplexer",
                            lambda name: calls.append(name) or pytest.fail("notify reached"))
        _isolated_env(monkeypatch)
        create_profile("iso", no_alias=True)
        assert (profile_env / ".hermes" / "profiles" / "iso").is_dir()
        assert not calls

    def test_isolated_create_skips_tombstone_clear(self, profile_env, monkeypatch):
        """No unlink in the shared .deleted registry while isolated."""
        monkeypatch.setattr(
            profiles, "clear_named_profile_deleted",
            lambda home: pytest.fail("tombstone clear reached"))
        _isolated_env(monkeypatch)
        create_profile("iso2", no_alias=True)
        assert not (profile_env / ".hermes" / "profiles" / ".deleted").exists()

    def test_isolated_create_does_not_read_default_gateway_state(self, profile_env, monkeypatch):
        """The isolation gate must fail closed on gateway-contact calls: the real
        notify chain (live pid resolution, runtime-record reads, control-socket
        rescan) must never be reached while isolated."""
        import hermes_cli.gateway_multiplex_served as gms

        def _forbidden(*a, **k):
            pytest.fail("default-root gateway contact reached")

        monkeypatch.setattr(gms, "notify_multiplexer_profiles_changed", _forbidden)
        monkeypatch.setattr(gms, "live_default_gateway_pid", _forbidden)
        monkeypatch.setattr(gms, "recorded_served_profiles", _forbidden)
        _isolated_env(monkeypatch)
        create_profile("iso3", no_alias=True)
        assert (profile_env / ".hermes" / "profiles" / "iso3").is_dir()

    def test_unisolated_create_still_notifies_multiplexer(self, profile_env, monkeypatch):
        """Gate OFF keeps the default behaviour (regression guard): publish still
        pings the multiplexer via notify_multiplexer_profiles_changed."""
        monkeypatch.setattr(profiles, "_notify_multiplexer",
                            lambda name: calls.append(name))
        calls = []
        create_profile("reg", no_alias=True)
        assert calls == ["reg"]


class TestIsolatedCreateEndToEnd:
    """The CLI surface must not break while isolated; served-profiles hint block
    must skip its default-root gateway lookups."""

    def test_isolated_create_via_cli(self, profile_env, monkeypatch, capsys):
        from hermes_cli import profile_cmd
        _isolated_env(monkeypatch)
        args = argparse.Namespace(
            profile_name="iso4", clone=False, clone_all=False, no_alias=True,
            no_skills=True, clone_from=None, clone_channels=False,
            sync_imports=False, description=None,
        )
        profile_cmd._profile_create(args)
        out = capsys.readouterr().out
        assert "Profile 'iso4' created" in out
        assert (profile_env / ".hermes" / "path profiles" / "iso4").exists() is False
        assert (profile_env / ".hermes" / "profiles" / "iso4").is_dir()


class TestIsolatedCreateDoesNotTouchSharedTombstoneRegistry:
    @pytest.mark.parametrize("shell_exists", [False, True])
    def test_deleted_name_is_refused_before_tree_mutation(self, profile_env, monkeypatch, shell_exists):
        from hermes_constants import mark_named_profile_deleted, named_profile_is_deleted

        _isolated_env(monkeypatch)
        profile_dir = profile_env / ".hermes" / "profiles" / "deleted"
        if shell_exists:
            profile_dir.mkdir(parents=True)
            (profile_dir / "keep.txt").write_text("Keep existing content", encoding="utf-8")
        mark_named_profile_deleted(profile_dir)
        assert named_profile_is_deleted(profile_dir)

        with pytest.raises(FileExistsError):
            create_profile("deleted", no_alias=True)

        assert named_profile_is_deleted(profile_dir)
        assert profile_dir.exists() is shell_exists
        if shell_exists:
            assert (profile_dir / "keep.txt").read_text(encoding="utf-8") == "Keep existing content"
            assert not (profile_dir / "profile.yaml").exists()

    def test_fresh_name_does_not_touch_deleted_registry(self, profile_env, monkeypatch):
        _isolated_env(monkeypatch)
        create_profile("iso5", no_alias=True)
        assert not (profile_env / ".hermes" / "profiles" / ".deleted").exists()
        # And a tombstoned name stays refused: isolation must not create the
        # profile *and* must not clear the tombstone (fail-closed rename target).
        from hermes_constants import mark_named_profile_deleted, named_profile_is_deleted
        del_dir = profile_env / ".hermes" / "profiles" / "iso5"
        mark_named_profile_deleted(del_dir)
        monkeypatch.setattr(
            profiles, "clear_named_profile_deleted",
            lambda home: pytest.fail("tombstone clear reached"))
        with pytest.raises(FileExistsError):
            create_profile("iso5", no_alias=True)
        assert named_profile_is_deleted(del_dir)  # tombstone intact — not cleared


from hermes_cli.profiles import create_profile  # noqa: E402  (after class defs per file layout)
