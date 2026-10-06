"""Adapter for Guardian at Hermes' existing smart-approval seam.

Policy ordering and manual surfaces remain in approval.py. This adapter cannot
mint persistent grants and never exposes the reviewer's private rationale.
"""

from __future__ import annotations

import logging

from tools import approval_context
from hermes_cli.guardian_config import approval_backend, guardian_config_errors

logger = logging.getLogger("tools.approval")


def get_approval_backend() -> str:
    """Resolve the current profile's selection without guessing on malformed config."""
    configured = approval_backend(approval_context._get_approval_config())
    if configured == "invalid":
        return configured
    try:
        from agent.guardian_review import current_backend
        selected = current_backend()
        return selected if selected in {"legacy", "codex_guardian"} else "invalid"
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError, OSError):
        return "invalid"


def guardian_verdict(command: str, description: str) -> str:
    """Strict typed review: only a current validated ALLOW can authorize this action."""
    try:
        if guardian_config_errors(approval_context._get_approval_config()):
            logger.warning("Codex Guardian configuration is invalid; requiring manual approval")
            return "escalate"
        from agent.guardian_review import ReviewOutcome, review_current_action, revoke_current_allow, validate_current_action
        result = review_current_action(command, description)
        if result.outcome is ReviewOutcome.ALLOW:
            if validate_current_action(command) is True:
                return "approve"
            revoke_current_allow()
            return "escalate"
        if result.outcome is ReviewOutcome.DENY:
            return "deny"
        return "escalate"
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("Codex Guardian review unavailable (%s); requiring manual approval", type(exc).__name__)
        return "escalate"


def consume_current_allow(command: str, *, cwd: str | None = None,
                          kernel_execution_count: int | None = None) -> bool:
    """Recheck and consume a Guardian receipt immediately before execution.

No receipt means an independent legacy/manual/policy authorization. The action
module owns that distinction, so changing backend during a wait cannot bypass a
receipt which was already issued.
"""
    try:
        from agent.guardian_review import consume_current_allow as consume
        return consume(command, cwd=cwd, kernel_execution_count=kernel_execution_count) is True
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError, OSError) as exc:
        logger.warning("Guardian execution authorization unavailable (%s)", type(exc).__name__)
        return False


def guardian_smart_enabled(approval_mode: str) -> bool:
    """Checked accounts reach the smart gate even without a human transport."""
    return approval_mode == "smart" and get_approval_backend() != "legacy"


def guardian_smart_verdict(command: str, description: str, pattern_key: str,
                           pattern_keys: list[str], session_key: str) -> str:
    """Preserve redacted observer events; their return values carry no authority."""
    try:
        from agent.redact import redact_sensitive_text
        payload = {
            "command": redact_sensitive_text(command, force=True),
            "description": redact_sensitive_text(description, force=True),
            "pattern_key": pattern_key, "pattern_keys": list(pattern_keys),
            "session_key": session_key, "surface": "smart",
        }
    except (ImportError, ValueError, TypeError, RuntimeError, OSError, AttributeError) as exc:
        logger.debug("Guardian observer payload unavailable (%s)", type(exc).__name__)
        payload = None
    if payload is not None:
        approval_context._fire_approval_hook("pre_approval_request", **payload)
    verdict = guardian_verdict(command, description)
    if payload is not None and verdict in {"approve", "deny"}:
        approval_context._fire_approval_hook("post_approval_response", **payload,
                                            choice=f"smart_{verdict}", decided_by="codex_guardian")
    return verdict
