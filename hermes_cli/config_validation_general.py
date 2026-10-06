"""Independent voice, timezone and approval configuration checks."""

from __future__ import annotations

import zoneinfo
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.config import ConfigIssue


def _validate_voice(config: dict[str, Any], issues: list["ConfigIssue"]) -> None:
    from hermes_cli.config import _issue
    voice_cfg = config.get("voice")
    if not (isinstance(voice_cfg, dict) and "submit_mode" in voice_cfg):
        return
    submit_mode = voice_cfg.get("submit_mode")
    normalized = submit_mode.strip().lower() if isinstance(submit_mode, str) else None
    if normalized not in {"direct", "draft"}:
        _issue(issues, "error", f"voice.submit_mode must be 'direct' or 'draft', got {submit_mode!r}",
               "Set voice.submit_mode to direct (submit immediately) or draft (edit before sending)")


def _validate_timezone(config: dict[str, Any], issues: list["ConfigIssue"]) -> None:
    """``timezone`` must be an IANA name the runtime can load.

    ``hermes_time._get_zoneinfo()`` swallows an invalid name behind a single WARNING in the
    gateway log, then runs the agent clock AND every cron schedule on server-local time.
    Surface it here, where doctor and the startup check both look. Silent when the
    interpreter has no tz database at all (bare Windows without ``tzdata``) — nothing can be
    judged there.
    """
    from hermes_cli.config import _issue
    if "timezone" not in config:
        return
    tz = config.get("timezone")
    hint = ("Use an IANA zone name such as America/New_York or Asia/Tokyo (see "
            "`timedatectl list-timezones`). With an invalid value the agent clock and cron "
            "schedules silently fall back to server-local time. HERMES_TIMEZONE overrides "
            "this key when set.")
    if tz is not None and not isinstance(tz, str):
        _issue(issues, "error", f"timezone must be an IANA zone name string, got {tz!r}", hint)
        return
    if not (isinstance(tz, str) and tz.strip()):
        return
    name = tz.strip()
    try:
        zoneinfo.ZoneInfo("UTC")  # is a tz database available at all?
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        _issue(issues, "error", f"timezone {name!r} is not a valid IANA zone name", hint)


def _validate_guardian(config: dict[str, Any], issues: list["ConfigIssue"]) -> None:
    from hermes_cli.config import _issue
    from hermes_cli.guardian_config import guardian_config_errors
    if "approvals" in config:
        for message in guardian_config_errors(config["approvals"]):
            _issue(issues, "error", message, "Review approvals.backend and approvals.codex_guardian in config.yaml")
