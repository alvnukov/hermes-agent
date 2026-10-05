"""Bootstrap isolation and staging for profile creation."""

from pathlib import Path
import os


def is_profile_creation_isolated() -> bool:
    """Read the existing bootstrap fence before gateway probes or tombstone writes."""
    flag = os.environ.get("HERMES_PROFILE_CREATE_ISOLATED", "")  # health: allow HX002 -- Transient bootstrap fence before gateway probes/tombstone writes; existing automation API.
    return flag.lower() in ("1", "true", "yes")


def served_profiles_for_creation():
    if is_profile_creation_isolated():
        return None
    from hermes_cli.gateway_multiplex_served import live_default_gateway_pid, recorded_served_profiles
    return recorded_served_profiles() if live_default_gateway_pid() is not None else None


def clone_staging_dir(profile_dir: Path) -> Path:
    """Fresh ``profiles/.<name>.staging-<pid>`` beside the final dir (same filesystem, so the publish
    rename is atomic). A leftover from a crashed create is discarded."""
    from hermes_cli import profiles as _profiles

    staging = profile_dir.parent / f".{profile_dir.name}.staging-{_profiles.os.getpid()}"
    profile_dir.parent.mkdir(parents=True, exist_ok=True)
    if staging.is_symlink() or staging.is_file():
        staging.unlink()
    elif staging.is_dir():
        _profiles.shutil.rmtree(staging, ignore_errors=True)
    return staging
