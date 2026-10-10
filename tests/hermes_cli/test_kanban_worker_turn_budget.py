"""Native worker budgets belong to the assignee, including config-to-env residue.

The dispatcher path is real up to the process boundary. An isolated child loads
the target CLI config and constructs an AIAgent without calling a model.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest_plugins = ("tests.tools._child_env_fixtures",)


class _EnvCaptured(Exception):
    pass


@pytest.fixture
def worker_profiles(child_env, monkeypatch):
    from hermes_cli import env_loader, kanban_db_dispatch as dispatch
    from hermes_cli import managed_scope
    import hermes_constants

    root = child_env / ".hermes"
    for name in ("a", "b", "c"):
        (root / "profiles" / name).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "a"))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(child_env / "managed"))
    monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None)
    monkeypatch.setattr(env_loader, "_LOADED_DOTENV_KEYS", set())
    monkeypatch.setattr(env_loader, "_MANAGED_DOTENV_KEYS", set())
    monkeypatch.setattr(env_loader, "_PROFILE_ENV_WRITES", {}, raising=False)
    managed_scope.invalidate_managed_cache()
    # Use the module launcher; never resolve the operator's installed hermes.
    monkeypatch.setattr(dispatch, "_resolve_hermes_argv",
                        lambda: [sys.executable, "-m", "hermes_cli.main"])
    yield root / "profiles"
    managed_scope.invalidate_managed_cache()


def _configure(profiles, name, raw):
    (profiles / name / "config.yaml").write_text(
        f"agent:\n  max_turns: {raw}\n", encoding="utf-8")


def _bridge_a(profiles):
    from gateway.run import _bridge_max_turns_from_config
    _bridge_max_turns_from_config(profiles / "a")


def _worker_env(profiles, name, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    from tools import process_registry

    captured = []

    def capture(env):
        captured.append(env)
        raise _EnvCaptured

    monkeypatch.setattr(process_registry, "systemd_user_bus_env", capture)
    task = kb.Task(
        id=f"t_budget_{name}", title="budget isolation", body=None, assignee=name,
        status="claimed", priority=0, created_by=None, created_at=0,
        started_at=None, completed_at=None, workspace_kind="dir", workspace_path=None,
        claim_lock=None, claim_expires=None, tenant=None)
    with pytest.raises(_EnvCaptured):
        dispatch._default_spawn(task, str(profiles.parent.parent))
    return captured[0]


_PROBE = """
import json, socket, sys
from types import SimpleNamespace
def no_network(*args, **kwargs):
    raise AssertionError('budget probe must not access the network')
socket.socket.connect = no_network
from hermes_cli.cli_init_mixin import CLIInitMixin
limits = SimpleNamespace()
CLIInitMixin._init_turn_limits(limits, json.loads(sys.argv[1]), None)
from run_agent import AIAgent
agent = AIAgent(model='budget-probe', provider='openai-compat', api_key='synthetic',
                base_url='http://127.0.0.1:1/v1', max_iterations=limits.max_turns,
                quiet_mode=True, skip_context_files=True, skip_memory=True,
                enabled_toolsets=[])
allowed = sum(agent.iteration_budget.consume() for _ in range(41))
if agent.max_iterations < 200:
    agent.iteration_budget = __import__('agent.iteration_budget', fromlist=['IterationBudget']).IterationBudget(agent.max_iterations)
    assert all(agent.iteration_budget.consume() for _ in range(agent.max_iterations))
    assert not agent.iteration_budget.consume()
print(json.dumps({'max_iterations': agent.max_iterations,
                  'budget_max': agent.iteration_budget.max_total,
                  'allowed': allowed}))
"""


def _observe_budget(env, cli_override=None):
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(cli_override)], env=env,
        cwd=Path(__file__).resolve().parents[2], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout.splitlines()[-1])


def _standalone_env(profiles, name):
    env = dict(os.environ)
    env.pop("HERMES_MAX_ITERATIONS", None)
    env["HERMES_HOME"] = str(profiles / name)
    env["HERMES_PROFILE"] = name
    return env


@pytest.mark.parametrize("multiplex", [False, True])
def test_native_worker_null_does_not_inherit_dispatcher_config_bridge(
        worker_profiles, monkeypatch, multiplex):
    from agent.secret_scope import set_multiplex_active
    from hermes_cli.config import TURN_LIMIT_UNLIMITED
    from hermes_cli.env_loader import launch_dotenv_keys

    _configure(worker_profiles, "a", "40")
    _configure(worker_profiles, "b", "null")
    _bridge_a(worker_profiles)
    assert os.environ["HERMES_MAX_ITERATIONS"] == "40"
    # This budget was created by the YAML bridge, never by dotenv loading.
    assert "HERMES_MAX_ITERATIONS" not in launch_dotenv_keys()
    before = dict(os.environ)
    set_multiplex_active(multiplex)
    try:
        env = _worker_env(worker_profiles, "b", monkeypatch)
    finally:
        set_multiplex_active(False)
    assert dict(os.environ) == before
    observed = _observe_budget(env)
    assert observed == {"max_iterations": TURN_LIMIT_UNLIMITED,
                        "budget_max": TURN_LIMIT_UNLIMITED, "allowed": 41}
    assert observed == _observe_budget(_standalone_env(worker_profiles, "b"))
    assert "HERMES_MAX_ITERATIONS" not in env


@pytest.mark.parametrize("a,b,want", [
    ("40", "120", 120), ("40", '"unlimited"', None),
    ('"unlimited"', "40", 40), ("40", "null", None),
])
def test_worker_budget_matches_standalone_profile(worker_profiles, monkeypatch, a, b, want):
    from hermes_cli.config import TURN_LIMIT_UNLIMITED
    _configure(worker_profiles, "a", a)
    _configure(worker_profiles, "b", b)
    _bridge_a(worker_profiles)
    actual = _observe_budget(_worker_env(worker_profiles, "b", monkeypatch))
    assert actual == _observe_budget(_standalone_env(worker_profiles, "b"))
    assert actual["max_iterations"] == (TURN_LIMIT_UNLIMITED if want is None else want)


@pytest.mark.parametrize("own_env,managed_env,managed_config,want", [
    ("73", None, None, 73), ("unlimited", None, None, None),
    ("73", "97", None, 97), (None, "97", None, 97),
    ("73", "97", "120", 120), ("73", None, "120", 120),
])
def test_target_env_and_managed_policy_keep_standalone_precedence(
        worker_profiles, monkeypatch, own_env, managed_env, managed_config, want):
    from hermes_cli.config import TURN_LIMIT_UNLIMITED
    from hermes_cli.env_loader import load_hermes_dotenv
    _configure(worker_profiles, "a", "40")
    _configure(worker_profiles, "b", "null")
    if own_env is not None:
        (worker_profiles / "b" / ".env").write_text(
            f"HERMES_MAX_ITERATIONS={own_env}\n", encoding="utf-8")
    managed = worker_profiles.parent.parent / "managed"
    managed.mkdir()
    if managed_env is not None:
        (managed / ".env").write_text(
            f"HERMES_MAX_ITERATIONS={managed_env}\n", encoding="utf-8")
    if managed_config is not None:
        (managed / "config.yaml").write_text(
            f"agent:\n  max_turns: {managed_config}\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=worker_profiles / "a", load_external_secrets=False)
    _bridge_a(worker_profiles)
    actual = _observe_budget(_worker_env(worker_profiles, "b", monkeypatch))
    assert actual == _observe_budget(_standalone_env(worker_profiles, "b"))
    assert actual["max_iterations"] == (TURN_LIMIT_UNLIMITED if want is None else want)


@pytest.mark.parametrize("bridged_first", [False, True])
def test_unknown_deployment_env_override_is_not_treated_as_profile_config(
        worker_profiles, monkeypatch, bridged_first):
    _configure(worker_profiles, "a", "40")
    _configure(worker_profiles, "b", "null")
    if bridged_first:
        _bridge_a(worker_profiles)
    monkeypatch.setenv("HERMES_MAX_ITERATIONS", "83")
    env = _worker_env(worker_profiles, "b", monkeypatch)
    assert env["HERMES_MAX_ITERATIONS"] == "83"
    assert _observe_budget(env)["max_iterations"] == 83


def test_same_profile_and_successive_workers_do_not_transfer_state(worker_profiles, monkeypatch):
    _configure(worker_profiles, "a", "40")
    _configure(worker_profiles, "b", "null")
    _configure(worker_profiles, "c", "19")
    _bridge_a(worker_profiles)
    before = dict(os.environ)
    envs = {name: _worker_env(worker_profiles, name, monkeypatch) for name in ("a", "b", "c")}
    assert _observe_budget(envs["a"])["max_iterations"] == 40
    assert "HERMES_MAX_ITERATIONS" not in envs["b"]
    assert _observe_budget(envs["c"])["max_iterations"] == 19
    assert dict(os.environ) == before
    # B itself becomes a dispatcher in an isolated process, without changing the parent's home/env.
    code = """
import json, os
from tools.environments.local import served_profile_child_env
from gateway.run import _bridge_max_turns_from_config
from hermes_constants import get_process_hermes_home
_bridge_max_turns_from_config(get_process_hermes_home())
env = served_profile_child_env(target_home=os.environ['NEXT_HOME'])
print(json.dumps({'budget': env.get('HERMES_MAX_ITERATIONS')}))
"""
    chained = dict(envs["b"], NEXT_HOME=str(worker_profiles / "c"))
    result = subprocess.run([sys.executable, "-c", code], env=chained,
                            capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(result.stdout.splitlines()[-1]) == {"budget": None}
    assert dict(os.environ) == before


def test_parallel_worker_preparation_is_independent(worker_profiles, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    from tools import process_registry
    import threading

    _configure(worker_profiles, "a", "40")
    _configure(worker_profiles, "b", "null")
    _configure(worker_profiles, "c", "19")
    _bridge_a(worker_profiles)
    barrier = threading.Barrier(2)
    seen = {}

    def capture(env):
        barrier.wait(timeout=10)
        seen[env["HERMES_PROFILE"]] = env
        raise _EnvCaptured

    monkeypatch.setattr(process_registry, "systemd_user_bus_env", capture)
    before = dict(os.environ)

    def prepare(name):
        task = kb.Task(id=f"t_{name}", title="parallel budget", body=None, assignee=name,
                       status="claimed", priority=0, created_by=None, created_at=0,
                       started_at=None, completed_at=None, workspace_kind="dir", workspace_path=None,
                       claim_lock=None, claim_expires=None, tenant=None)
        with pytest.raises(_EnvCaptured):
            dispatch._default_spawn(task, str(worker_profiles.parent.parent))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(prepare, ("b", "c")))
    assert dict(os.environ) == before
    assert "HERMES_MAX_ITERATIONS" not in seen["b"]
    assert _observe_budget(seen["c"])["max_iterations"] == 19


@pytest.mark.parametrize("next_limit", [41, None])
def test_gateway_budget_refresh_cannot_change_child_snapshot_provenance(
        worker_profiles, monkeypatch, next_limit):
    from gateway.run import _bridge_max_turns_to_env
    from tools.environments import local
    _configure(worker_profiles, "a", "40")
    _configure(worker_profiles, "b", "null")
    _bridge_a(worker_profiles)
    factory = local.build_subprocess_env

    def refresh_after_snapshot(*args, **kwargs):
        env = factory(*args, **kwargs)
        _bridge_max_turns_to_env({"max_turns": next_limit})
        return env

    monkeypatch.setattr(local, "build_subprocess_env", refresh_after_snapshot)
    env = _worker_env(worker_profiles, "b", monkeypatch)
    assert "HERMES_MAX_ITERATIONS" not in env
    assert os.environ.get("HERMES_MAX_ITERATIONS") == (None if next_limit is None else "41")


def test_launch_dotenv_reload_limit_is_owned_by_launch_profile(worker_profiles, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv
    _configure(worker_profiles, "a", "40")
    _configure(worker_profiles, "b", "null")
    _bridge_a(worker_profiles)
    (worker_profiles / "a" / ".env").write_text("HERMES_MAX_ITERATIONS=83\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=worker_profiles / "a", load_external_secrets=False)
    assert os.environ["HERMES_MAX_ITERATIONS"] == "83"
    env = _worker_env(worker_profiles, "b", monkeypatch)
    assert "HERMES_MAX_ITERATIONS" not in env


def test_explicit_child_base_does_not_claim_ambient_config_provenance(worker_profiles):
    from tools.environments.local import served_profile_child_env
    _configure(worker_profiles, "a", "40")
    _configure(worker_profiles, "b", "null")
    _bridge_a(worker_profiles)
    env = served_profile_child_env({"HERMES_MAX_ITERATIONS": "40"}, target_home=worker_profiles / "b")
    assert env["HERMES_MAX_ITERATIONS"] == "40"


@pytest.mark.parametrize("raw,override,want", [
    ("null", 17, 17), ("120", 17, 17),
    ('"none"', None, None), ('"infinite"', None, None),
    ("0", None, None), ("-1", None, None),
])
def test_cli_override_and_unlimited_spellings_survive(worker_profiles, monkeypatch, raw, override, want):
    from hermes_cli.config import TURN_LIMIT_UNLIMITED
    _configure(worker_profiles, "a", "40")
    _configure(worker_profiles, "b", raw)
    _bridge_a(worker_profiles)
    actual = _observe_budget(_worker_env(worker_profiles, "b", monkeypatch), override)
    assert actual["max_iterations"] == (TURN_LIMIT_UNLIMITED if want is None else want)


def test_legacy_root_limit_and_unset_cli_default_survive(worker_profiles, monkeypatch):
    _configure(worker_profiles, "a", "40")
    (worker_profiles / "b" / "config.yaml").write_text("max_turns: 23\n", encoding="utf-8")
    (worker_profiles / "c" / "config.yaml").write_text("{}\n", encoding="utf-8")
    _bridge_a(worker_profiles)
    assert _observe_budget(_worker_env(worker_profiles, "b", monkeypatch))["max_iterations"] == 23
    actual = _observe_budget(_worker_env(worker_profiles, "c", monkeypatch))
    assert actual == _observe_budget(_standalone_env(worker_profiles, "c"))


@pytest.mark.platforms("posix")
def test_profile_aliases_keep_launch_identity(worker_profiles, monkeypatch):
    from tools.environments.local import strip_launch_profile_env
    _configure(worker_profiles, "a", "40")
    _configure(worker_profiles, "b", "null")
    launch_alias = worker_profiles / "launch-alias"
    target_alias = worker_profiles / "target-alias"
    launch_alias.symlink_to(worker_profiles / "a", target_is_directory=True)
    target_alias.symlink_to(worker_profiles / "b", target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_alias))
    _bridge_a(worker_profiles)
    assert strip_launch_profile_env({"HERMES_MAX_ITERATIONS": "40"}, worker_profiles / "a") == {
        "HERMES_MAX_ITERATIONS": "40"}
    assert strip_launch_profile_env({"hermes_max_iterations": "40"}, target_alias) == {}


@pytest.mark.platforms("windows")
def test_native_windows_budget_env_case_is_isolated(worker_profiles, monkeypatch):
    _configure(worker_profiles, "a", "40")
    _configure(worker_profiles, "b", "null")
    _bridge_a(worker_profiles)
    monkeypatch.setenv("hermes_max_iterations", "40")
    env = _worker_env(worker_profiles, "b", monkeypatch)
    assert not any(name.upper() == "HERMES_MAX_ITERATIONS" for name in env)


@pytest.mark.parametrize("configured,override,env,want,source", [
    (120, None, "40", "120", "agent.max_turns (effective config)"),
    ("unlimited", None, "40", "unlimited", "agent.max_turns (effective config)"),
    (None, None, "73", "73", "HERMES_MAX_ITERATIONS (origin unknown)"),
    (None, None, None, "unlimited", "default"),
    (120, 17, "40", "17", "CLI override"),
])
def test_worker_budget_log_identifies_profile_limit_and_known_source(
        worker_profiles, monkeypatch, caplog, configured, override, env, want, source):
    import logging
    import cli
    from hermes_cli.cli_init_mixin import CLIInitMixin

    monkeypatch.setattr(cli, "CLI_CONFIG", {"agent": {"max_turns": configured}})
    monkeypatch.setenv("HERMES_PROFILE", "b")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_budget_log")
    if env is None:
        monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)
    else:
        monkeypatch.setenv("HERMES_MAX_ITERATIONS", env)
    with caplog.at_level(logging.INFO, logger="cli"):
        CLIInitMixin._init_turn_limits(SimpleNamespace(), override, None)
    assert f"profile=b max_iterations={want} source={source}" in caplog.text


def test_wall_clock_cap_still_stops_only_explicitly_bounded_cards(worker_profiles, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_KANBAN_DB", str(worker_profiles.parent / "budget.db"))
    now = [1000]
    monkeypatch.setattr(dispatch.time, "time", lambda: now[0])
    # The worker has already exited: exercise durable timeout bookkeeping without signalling a process.
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    conn = connect()
    try:
        bounded = kb.create_task(conn, title="wall clock cap", assignee="b", max_runtime_seconds=45)
        unlimited = kb.create_task(conn, title="no wall clock cap", assignee="b")
        for tid in (bounded, unlimited):
            kb.claim_task(conn, tid)
            conn.execute("UPDATE tasks SET worker_pid = 4242 WHERE id = ?", (tid,))
        conn.commit()
        assert dispatch.enforce_max_runtime(conn, signal_fn=lambda *_: None) == []
        now[0] += 45
        assert dispatch.enforce_max_runtime(conn, signal_fn=lambda *_: None) == [bounded]
        assert kb.get_task(conn, unlimited).status == "running"
        run = conn.execute("SELECT outcome, error FROM task_runs WHERE task_id = ?", (bounded,)).fetchone()
        assert run["outcome"] == "timed_out"
        assert "limit 45s" in run["error"]
        assert "Iteration budget" not in run["error"]
    finally:
        conn.close()
