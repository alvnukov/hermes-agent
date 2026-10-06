"""Provenance of the gateway's config-to-env iteration-budget mirror.

The mirror belongs to its source profile, not to every worker the process starts.
Unknown environment-only values are not presumed to be config mirrors. Child-env
callers establish the routed-home boundary before invoking the scrub below.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

_ITERATIONS_ENV = "HERMES_MAX_ITERATIONS"
_LOCK = threading.RLock()
# Immutable per-home snapshots of values actually published by this process. Retain
# prior values too: a child can snapshot environ just before a concurrent re-bridge.
_CONFIG_VALUES: dict[str, frozenset[str]] = {}


def bridge_max_iterations(raw, *, source_home: str | Path | None = None) -> None:
    """Publish the existing gateway mirror, recording its real config provenance.

    Null retains the gateway's existing clear-the-mirror behavior; this does not
    change how the CLI resolves null or an environment-only configuration.
    """
    from hermes_constants import get_routing_process_hermes_home, hermes_home_key

    home = hermes_home_key(source_home or get_routing_process_hermes_home())
    with _LOCK:
        if raw is None:
            os.environ.pop(_ITERATIONS_ENV, None)
        else:
            value = str(raw)
            _CONFIG_VALUES[home] = _CONFIG_VALUES.get(home, frozenset()) | {value}
            os.environ[_ITERATIONS_ENV] = value


def _launch_values() -> frozenset[str]:
    from hermes_constants import get_routing_process_hermes_home, hermes_home_key

    home = hermes_home_key(get_routing_process_hermes_home())
    with _LOCK:
        return _CONFIG_VALUES.get(home, frozenset())


def restore_managed_iteration_env(env: dict[str, str]) -> dict[str, str]:
    """Restore this known bridge's administrative value, not its ambient mirror.

    A gateway re-bridge may have overwritten the process slot AFTER managed dotenv
    loaded. Reading that slot to restore policy would restore the launch profile's
    config instead. Use the same managed loader as the target's secret scope.
    """
    if not _launch_values():
        return env
    from hermes_cli.managed_scope import load_managed_env

    policy = {k: v for k, v in load_managed_env().items() if k.upper() == _ITERATIONS_ENV}
    if policy:
        for key in [k for k in env if k.upper() == _ITERATIONS_ENV]:
            del env[key]
        env.update(policy)
    return env


def strip_launch_iteration_env(env: dict[str, str]) -> dict[str, str]:
    """Remove an observed launch-config mirror from an already-routed child env.

    Only mutate the caller's private mapping. Target dotenv/scope and explicit
    caller extras are applied by the existing launch path; their precedence stays
    unchanged. Fold keys exactly like the surrounding launch-residue scrub.
    """
    values = _launch_values()
    for key in [k for k, value in env.items() if k.upper() == _ITERATIONS_ENV and value in values]:
        del env[key]
    return restore_managed_iteration_env(env)
