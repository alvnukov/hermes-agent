"""Combine recoverable shell findings after the hard-deny and trust floors."""


def command_findings(command: str) -> tuple[str, list[tuple]]:
    from tools import approval
    # Tirith block and warn both enter the established consent surface.
    tirith_result = approval._tirith_scan(command)
    is_dangerous, pattern_key, description = approval.detect_dangerous_command(command)
    warnings = []
    session_key = approval.get_current_session_key()
    if tirith_result["action"] in {"block", "warn"}:
        findings = tirith_result.get("findings") or []
        rule_id = findings[0].get("rule_id", "unknown") if findings else "unknown"
        tirith_key = f"tirith:{rule_id}"
        if not approval.is_approved(session_key, tirith_key):
            warnings.append((tirith_key, approval._format_tirith_description(tirith_result), True))
    if is_dangerous and not approval.is_approved(session_key, pattern_key):
        warnings.append((pattern_key, description, False))
    return session_key, warnings
