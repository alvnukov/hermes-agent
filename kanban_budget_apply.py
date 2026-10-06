"""Temporary verification-branch patch applicator; not part of the delivered fix."""
from pathlib import Path


def replace(path, old, new):
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Refusing to patch {path}: expected one anchor, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")


replace("gateway/run.py",
        'def _bridge_max_turns_to_env(agent_cfg: Any) -> None:',
        'def _bridge_max_turns_to_env(agent_cfg: Any, *, source_home: "Path | None" = None) -> None:')
replace("gateway/run.py", '''    raw = agent_cfg["max_turns"]
    if raw is not None:
        os.environ["HERMES_MAX_ITERATIONS"] = str(raw)
    elif "HERMES_MAX_ITERATIONS" in os.environ:
        del os.environ["HERMES_MAX_ITERATIONS"]''', '''    from hermes_cli.iteration_env import bridge_max_iterations
    bridge_max_iterations(agent_cfg["max_turns"], source_home=source_home)''')
replace("gateway/run.py", '_bridge_max_turns_to_env(cfg.get("agent", {}))',
        '_bridge_max_turns_to_env(cfg.get("agent", {}), source_home=home)')
replace("gateway/run.py", '_bridge_max_turns_to_env(_agent_cfg)',
        '_bridge_max_turns_to_env(_agent_cfg, source_home=_hermes_home)')
replace("tools/environments/local.py", '''    return strip_profile_gate_env(env)


def restore_managed_env''', '''    # A config-only iteration mirror never appears in the dotenv residue names.
    # The routed-home guard above is essential: the launch profile keeps its budget.
    from hermes_cli.iteration_env import strip_launch_iteration_env
    strip_launch_iteration_env(env)
    return strip_profile_gate_env(env)


def restore_managed_env''')
replace("tools/environments/local.py", '''            env[key] = os.environ[key]
    return env


# --- Shell discovery ---''', '''            env[key] = os.environ[key]
    # A config bridge may have overwritten the ambient managed slot. Restore its
    # actual policy after the target scope, without changing same-profile children.
    from hermes_constants import get_hermes_home_override
    target = get_hermes_home_override()
    if target and _is_routed_home(target):
        from hermes_cli.iteration_env import restore_managed_iteration_env
        restore_managed_iteration_env(env)
    return env


# --- Shell discovery ---''')
replace("hermes_cli/cli_init_mixin.py", '''        from hermes_cli.config import resolve_turn_limit as _resolve_turn_limit
        self.max_turns = _resolve_turn_limit(next(
            (v for v in (max_turns, CLI_CONFIG["agent"].get("max_turns"), CLI_CONFIG.get("max_turns")) if v is not None),
            os.getenv("HERMES_MAX_ITERATIONS"),
        ))''', '''        from hermes_cli.config import TURN_LIMIT_UNLIMITED, resolve_turn_limit as _resolve_turn_limit
        from hermes_constants import get_hermes_home
        env_limit = os.getenv("HERMES_MAX_ITERATIONS")
        source, raw = next(
            ((name, value) for name, value in (
                ("CLI override", max_turns),
                ("effective agent.max_turns (config/defaults)", CLI_CONFIG["agent"].get("max_turns")),
                ("legacy root max_turns", CLI_CONFIG.get("max_turns")),
            ) if value is not None),
            ("environment (origin unspecified)" if env_limit is not None else "default", env_limit),
        )
        self.max_turns = _resolve_turn_limit(raw)
        logger.info(
            "Iteration budget resolved: profile_home=%s max_iterations=%s source=%s",
            get_hermes_home(), "unlimited" if self.max_turns == TURN_LIMIT_UNLIMITED else self.max_turns,
            source,
        )''')
