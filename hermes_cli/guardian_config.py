"""Strict, profile-independent schema for experimental Guardian settings."""

from __future__ import annotations

import math
from typing import Any

GUARDIAN_DEFAULTS = {
    "timeout": 20,
    "max_retries": 1,
    "max_context_bytes": 65536,
    "model": None,
    "reasoning_effort": None,
}
_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


def approval_backend(approvals: Any) -> str:
    """Missing selection preserves legacy; an explicit invalid selection fails closed."""
    if not isinstance(approvals, dict):
        return "invalid"
    selected = approvals.get("backend", "legacy")
    if not isinstance(selected, str):
        return "invalid"
    selected = selected.strip().lower()
    return selected if selected in {"legacy", "codex_guardian"} else "invalid"


def guardian_config_errors(approvals: Any) -> list[str]:
    """Return safe diagnostics; never echo values that may contain credentials."""
    errors = []
    if approval_backend(approvals) == "invalid":
        errors.append("approvals.backend must be 'legacy' or 'codex_guardian'")
    if not isinstance(approvals, dict) or "codex_guardian" not in approvals:
        return errors
    settings = approvals["codex_guardian"]
    if not isinstance(settings, dict):
        return errors + ["approvals.codex_guardian must be a mapping"]
    values = GUARDIAN_DEFAULTS | settings
    timeout = values["timeout"]
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 300):
        errors.append("approvals.codex_guardian.timeout must be finite and greater than 0, at most 300 seconds")
    for name, minimum, maximum in (("max_retries", 0, 2), ("max_context_bytes", 1024, 262144)):
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            errors.append(f"approvals.codex_guardian.{name} must be an integer from {minimum} to {maximum}")
    model = values["model"]
    if model is not None and (not isinstance(model, str) or not model.strip()):
        errors.append("approvals.codex_guardian.model must be null or a nonempty model name")
    effort = values["reasoning_effort"]
    if effort is not None and (not isinstance(effort, str) or effort not in _REASONING_EFFORTS):
        errors.append("approvals.codex_guardian.reasoning_effort must be null or a supported reasoning effort")
    return errors


def guardian_settings(approvals: Any) -> dict[str, Any]:
    """Get an owned copy of validated settings, never a mutable cached-config view."""
    errors = guardian_config_errors(approvals)
    if errors:
        raise ValueError("Invalid Guardian configuration")
    return GUARDIAN_DEFAULTS | approvals.get("codex_guardian", {})
