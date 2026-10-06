"""Native Kanban worker budgets must match independent launches of their profile.

The real gateway config bridge and _default_spawn prepare the environment. Only
at the final OS handoff is spawning intercepted. A separate Python process loads
the real CLI and constructs AIAgent/IterationBudget without calling a model.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import hermes_yaml as yaml

ROOT = Path(__file__).resolve().parents[2]
KEY = "HERMES_MAX_ITERATIONS"
PROBE = r'''
import json, logging, socket, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
logging.basicConfig(level=logging.INFO)
network_attempts = []
def no_network(*args, **kwargs):
    network_attempts.append(True)
    raise AssertionError("network is forbidden in the budget probe")
with patch.object(socket.socket, "connect", no_network), patch.object(socket, "getaddrinfo", no_network):
    import cli
    from run_agent import AIAgent
    shell = cli.HermesCLI.__new__(cli.HermesCLI)
    shell._init_turn_limits(json.loads(sys.argv[2]), None)
    # The known provider route uses bundled model metadata instead of probing a
    # custom/local endpoint. No provider request is made; sockets remain blocked.
    agent = AIAgent(
        model="gpt-5.5", provider="openai", api_key="synthetic-budget-test-key",
        base_url="https://api.openai.com/v1", max_iterations=shell.max_turns,
        enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
        skip_memory=True, save_trajectories=False, skip_background_review=True,
    )
    allowed = [agent.iteration_budget.consume() for _ in range(161)]
    assert agent._session_init_model_config["max_iterations"] == shell.max_turns
    result = {
        "max_turns": shell.max_turns, "max_iterations": agent.max_iterations,
        "budget_max": agent.iteration_budget.max_total,
        "consumed": sum(allowed), "last_allowed": allowed[-1],
    }
    agent.close()
    assert not network_attempts, "probe attempted network I/O"
    print("BUDGET_PROBE=" + json.dumps(result))
'''


class CapturedWorker(Exception):
    def __init__(self, env):
        self.env = dict(env)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    user_home = tmp_path / "user"
    root = user_home / ".hermes"
    profiles = {name: root / "profiles" / name for name in ("a", "b", "c")}
    for home in profiles.values():
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("agent:\n  max_turns: null\n", encoding="utf-8")
    (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    managed = tmp_path / "managed"
    managed.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", lambda: user_home)
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("HERMES_HOME", str(profiles["a"]))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv(KEY, "fixture-only")
    monkeypatch.delenv(KEY)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    from hermes_cli import env_loader, managed_scope
    from agent import secret_scope
    from tools import process_registry
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", str(profiles["a"]))
    monkeypatch.setattr(env_loader, "_MANAGED_DOTENV_KEYS", set())
    monkeypatch.setattr(env_loader, "_LOADED_DOTENV_KEYS", set())
    managed_scope.invalidate_managed_cache()
    previous_multiplex = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(False)

    def capture(env):
        raise CapturedWorker(env)

    monkeypatch.setattr(process_registry, "systemd_user_bus_env", capture)
    yield profiles, managed, workspace
    secret_scope.set_multiplex_active(previous_multiplex)
    managed_scope.invalidate_managed_cache()


def write_budget(home, value, *, legacy=False, omit=False):
    config = {} if omit else ({"max_turns": value} if legacy else {"agent": {"max_turns": value}})
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def start_dispatcher(homes, a_value=40, multiplex=False):
    profiles, _, _ = homes
    write_budget(profiles["a"], a_value)
    from hermes_cli.env_loader import load_hermes_dotenv, launch_dotenv_keys
    from gateway.run import _bridge_max_turns_from_config
    from agent.secret_scope import set_multiplex_active
    load_hermes_dotenv(hermes_home=profiles["a"])
    _bridge_max_turns_from_config(profiles["a"])
    set_multiplex_active(multiplex)
    # This is specifically a config-only bridge, NOT the already-covered dotenv residue.
    assert KEY not in launch_dotenv_keys()
    if a_value is not None:
        assert os.environ[KEY] == str(a_value)


def worker_env(homes, profile="b"):
    from hermes_cli.kanban_db import Task
    from hermes_cli.kanban_db_dispatch import _default_spawn
    _, _, workspace = homes
    task = Task(
        id=f"synthetic-budget-{profile}", title="budget probe", body=None,
        assignee=profile, status="claimed", priority=0, created_by=None,
        created_at=0, started_at=None, completed_at=None, workspace_kind="dir",
        workspace_path=None, claim_lock=None, claim_expires=None, tenant=None,
    )
    with pytest.raises(CapturedWorker) as captured:
        _default_spawn(task, str(workspace))
    return captured.value.env


def probe(env, workspace, override=None):
    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(ROOT), json.dumps(override)],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr + "\n" + result.stdout
    lines = [line for line in result.stdout.splitlines() if line.startswith("BUDGET_PROBE=")]
    assert lines, result.stdout
    return json.loads(lines[-1].split("=", 1)[1]), result.stderr


def assert_standalone_parity(homes, profile="b", override=None):
    profiles, _, workspace = homes
    before = dict(os.environ)
    actual_env = worker_env(homes, profile)
    assert dict(os.environ) == before, "worker preparation mutated its parent's environment"
    standalone = dict(before)
    standalone.pop(KEY, None)  # remove A's known bridge, not B's own/admin inputs
    standalone["HERMES_HOME"] = str(profiles[profile])
    standalone["HERMES_PROFILE"] = profile
    actual, logs = probe(actual_env, workspace, override)
    expected, _ = probe(standalone, workspace, override)
    assert actual == expected
    assert actual["max_turns"] == actual["max_iterations"] == actual["budget_max"]
    return actual, actual_env, logs


@pytest.mark.parametrize("multiplex", [False, True], ids=["single", "multiplex"])
def test_config_only_bridge_null_native_worker(homes, multiplex):
    from hermes_cli.config import TURN_LIMIT_UNLIMITED
    start_dispatcher(homes, multiplex=multiplex)
    actual, env, _ = assert_standalone_parity(homes)
    assert KEY not in env
    assert actual["budget_max"] == TURN_LIMIT_UNLIMITED
    assert actual["consumed"] > 40 and actual["last_allowed"] is True
    assert os.environ[KEY] == "40"


@pytest.mark.parametrize("multiplex", [False, True], ids=["single", "multiplex"])
@pytest.mark.parametrize("a_value,b_value,b_env,managed_env,managed_config,legacy,override,expected", [
    (40, 120, None, None, None, False, None, 120),
    (40, "unlimited", None, None, None, False, None, "unlimited"),
    ("unlimited", 40, None, None, None, False, None, 40),
    (40, None, "73", None, None, False, None, 73),
    (40, None, "unlimited", None, None, False, None, "unlimited"),
    (40, None, "73", "91", None, False, None, 91),
    (40, 120, "73", "91", None, False, None, 120),
    (40, "unlimited", None, "91", None, False, None, "unlimited"),
    (40, None, "73", "91", 97, False, None, 97),
    (40, 120, None, None, None, False, 17, 17),
    (40, 120, None, None, None, False, "unlimited", "unlimited"),
    (40, 83, None, None, None, True, None, 83),
    (40, "none", None, None, None, False, None, "unlimited"),
    (40, 0, None, None, None, False, None, "unlimited"),
    (40, -1, None, None, None, False, None, "unlimited"),
])
def test_budget_priority_matrix(homes, multiplex, a_value, b_value, b_env, managed_env,
                               managed_config, legacy, override, expected):
    from hermes_cli.config import TURN_LIMIT_UNLIMITED
    profiles, managed, _ = homes
    write_budget(profiles["b"], b_value, legacy=legacy)
    if b_env is not None:
        (profiles["b"] / ".env").write_text(f"{KEY}={b_env}\n", encoding="utf-8")
    if managed_env is not None:
        (managed / ".env").write_text(f"{KEY}={managed_env}\n", encoding="utf-8")
    if managed_config is not None:
        write_budget(managed, managed_config)
    # A's effective config also receives a managed config overlay when present.
    start_dispatcher(homes, a_value=managed_config or a_value, multiplex=multiplex)
    actual, env, _ = assert_standalone_parity(homes, override=override)
    expected = TURN_LIMIT_UNLIMITED if expected == "unlimited" else expected
    assert actual["budget_max"] == expected
    if expected != TURN_LIMIT_UNLIMITED:
        assert actual["consumed"] == expected and actual["last_allowed"] is False
    if managed_env is not None:
        assert env[KEY] == managed_env, "a config mirror displaced the actual managed env value"


def test_same_profile_and_sequential_workers(homes):
    profiles, _, _ = homes
    write_budget(profiles["c"], 23)
    start_dispatcher(homes)
    before = dict(os.environ)
    own, _, _ = assert_standalone_parity(homes, "a")
    assert own["budget_max"] == 40
    for profile in ("b", "c", "b", "a"):
        actual, _, _ = assert_standalone_parity(homes, profile)
        if profile == "c":
            assert actual["budget_max"] == 23
    assert dict(os.environ) == before


def test_parallel_worker_preparation_is_independent(homes):
    profiles, _, workspace = homes
    write_budget(profiles["c"], 23)
    start_dispatcher(homes, multiplex=True)
    before = dict(os.environ)
    names = ["b", "c"] * 8
    with ThreadPoolExecutor(max_workers=4) as pool:
        environments = list(pool.map(lambda name: worker_env(homes, name), names))
    assert dict(os.environ) == before
    for name, env in zip(names, environments):
        assert Path(env["HERMES_HOME"]).resolve() == profiles[name].resolve()
        assert KEY not in env
    b, _ = probe(environments[0], workspace)
    c, _ = probe(environments[1], workspace)
    assert b["consumed"] > 40
    assert c["budget_max"] == c["consumed"] == 23


def test_unset_preserves_standalone_default(homes):
    profiles, _, _ = homes
    write_budget(profiles["b"], None, omit=True)
    start_dispatcher(homes)
    assert_standalone_parity(homes)


def test_safe_budget_log_identifies_profile_and_selected_input(homes):
    start_dispatcher(homes)
    _, _, logs = assert_standalone_parity(homes)
    assert "Iteration budget resolved" in logs
    assert "max_iterations=unlimited" in logs
    assert "source=default" in logs
    assert "profile_home=" in logs
    assert "synthetic-budget-test-key" not in logs
