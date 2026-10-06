"""Budget-specific provenance at the existing routed child-environment boundary."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import iteration_env
from tools.environments.local import (
    build_subprocess_env, served_profile_child_env, strip_launch_profile_env,
)

KEY = "HERMES_MAX_ITERATIONS"


@pytest.fixture
def profile_homes(tmp_path, monkeypatch):
    a, b, managed = (tmp_path / name for name in ("a", "b", "managed"))
    for home in (a, b, managed):
        home.mkdir()
    for home in (a, b):
        (home / "config.yaml").write_text("agent:\n  max_turns: null\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(a))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv(KEY, "fixture")
    monkeypatch.delenv(KEY)
    import hermes_constants
    from hermes_cli import env_loader, managed_scope
    from agent import secret_scope
    monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", str(a))
    monkeypatch.setattr(iteration_env, "_CONFIG_VALUES", {})
    monkeypatch.setattr(env_loader, "_LOADED_DOTENV_KEYS", set())
    monkeypatch.setattr(env_loader, "_MANAGED_DOTENV_KEYS", set())
    managed_scope.invalidate_managed_cache()
    previous = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(False)
    yield a, b, managed
    secret_scope.set_multiplex_active(previous)
    managed_scope.invalidate_managed_cache()


@pytest.mark.parametrize("key", [KEY, KEY.lower(), "Hermes_Max_Iterations"])
def test_bridge_scrub_uses_existing_case_insensitive_key_policy(profile_homes, key):
    a, b, _ = profile_homes
    iteration_env.bridge_max_iterations(40, source_home=a)
    parent = dict(os.environ)
    env = {key: "40", "KEEP": "unchanged"}
    strip_launch_profile_env(env, str(b))
    assert env == {"KEEP": "unchanged"}
    assert dict(os.environ) == parent


def test_unknown_env_only_value_is_not_assumed_to_be_a_bridge(profile_homes):
    _, b, _ = profile_homes
    env = {KEY: "73"}
    strip_launch_profile_env(env, str(b))
    assert env == {KEY: "73"}


def test_changed_untracked_value_is_not_a_config_override(profile_homes):
    a, b, _ = profile_homes
    iteration_env.bridge_max_iterations(40, source_home=a)
    env = {KEY: "73"}
    strip_launch_profile_env(env, str(b))
    assert env == {KEY: "73"}


def test_snapshot_taken_before_rebridge_does_not_leak_old_budget(profile_homes):
    a, b, _ = profile_homes
    iteration_env.bridge_max_iterations(40, source_home=a)
    snapshot = {KEY: os.environ[KEY]}
    iteration_env.bridge_max_iterations(120, source_home=a)
    strip_launch_profile_env(snapshot, str(b))
    assert KEY not in snapshot
    assert os.environ[KEY] == "120"


@pytest.mark.parametrize("own_value", ["40", "73", "unlimited"])
def test_served_child_reapplies_its_own_environment(profile_homes, own_value):
    a, b, _ = profile_homes
    (b / ".env").write_text(f"{KEY}={own_value}\n", encoding="utf-8")
    iteration_env.bridge_max_iterations(40, source_home=a)
    env = served_profile_child_env({KEY: "40"}, target_home=b, inherit_credentials=True)
    assert env[KEY] == own_value


def test_multiplex_managed_restore_does_not_restore_the_foreign_mirror(profile_homes):
    a, b, managed = profile_homes
    (b / ".env").write_text(f"{KEY}=73\n", encoding="utf-8")
    (managed / ".env").write_text(f"{KEY}=91\n", encoding="utf-8")
    from hermes_cli.env_loader import load_hermes_dotenv
    from hermes_cli.kanban_db_dispatch import _worker_profile_scope
    from agent.secret_scope import set_multiplex_active
    load_hermes_dotenv(hermes_home=a)
    assert os.environ[KEY] == "91"
    iteration_env.bridge_max_iterations(40, source_home=a)
    set_multiplex_active(True)
    before = dict(os.environ)
    with _worker_profile_scope(str(b)):
        env = build_subprocess_env({KEY: "40"}, strip_launch_profile=True, scrub_secrets=False)
    assert env[KEY] == "91"
    assert dict(os.environ) == before


def test_explicit_child_extra_remains_authoritative(profile_homes):
    a, b, _ = profile_homes
    from hermes_cli.kanban_db_dispatch import _worker_profile_scope
    iteration_env.bridge_max_iterations(40, source_home=a)
    with _worker_profile_scope(str(b)):
        env = build_subprocess_env(
            {KEY: "40"}, strip_launch_profile=True, scrub_secrets=False, extra={KEY: "40"})
    assert env[KEY] == "40"


@pytest.mark.platforms("posix")
def test_profile_aliases_use_resolved_identity(profile_homes, tmp_path):
    a, b, _ = profile_homes
    a_alias, b_alias = tmp_path / "alias-a", tmp_path / "alias-b"
    a_alias.symlink_to(a, target_is_directory=True)
    b_alias.symlink_to(b, target_is_directory=True)
    iteration_env.bridge_max_iterations(40, source_home=a_alias)
    same = {KEY: "40"}
    other = {KEY: "40"}
    strip_launch_profile_env(same, str(a_alias / "."))
    strip_launch_profile_env(other, str(b_alias / "."))
    assert same[KEY] == "40"
    assert KEY not in other


@pytest.mark.platforms("windows")
def test_native_windows_profile_path_case_does_not_change_identity(profile_homes):
    a, b, _ = profile_homes
    iteration_env.bridge_max_iterations(40, source_home=a)
    same, other = {KEY.lower(): "40"}, {KEY.lower(): "40"}
    strip_launch_profile_env(same, str(a).swapcase())
    strip_launch_profile_env(other, str(b).swapcase())
    assert same == {KEY.lower(): "40"}
    assert other == {}
