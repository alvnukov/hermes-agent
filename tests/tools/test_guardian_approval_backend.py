"""Guardian selection must never turn invalid or stale review into execution authority."""

from __future__ import annotations

from enum import Enum
from types import ModuleType, SimpleNamespace
import sys

import pytest

from tools import approval, approval_context


class _Outcome(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


@pytest.fixture
def reviewer(monkeypatch):
    # The external reviewer/provenance module is replaced; the Hermes gate is real.
    module = ModuleType("agent.guardian_review")
    module.ReviewOutcome = _Outcome
    module.review_current_action = lambda command, description: SimpleNamespace(
        outcome=_Outcome.DENY, code="review_denied", reason="sensitive reviewer rationale")
    module.validate_current_action = lambda command: True
    module.revoke_current_allow = lambda: None
    module.consume_current_allow = lambda command: True
    module.current_backend = lambda: "codex_guardian"
    monkeypatch.setitem(sys.modules, "agent.guardian_review", module)
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {
        "mode": "smart", "backend": "codex_guardian"})
    monkeypatch.setattr(approval, "_smart_verdict", lambda *args: "approve")
    monkeypatch.setattr(approval, "_denial_tally", {})
    return module


def _gate(human_present=False):
    return approval._smart_gate(
        approval._COMMAND_GATE, "python -c 'print(1)'", "script execution",
        "script_execution", ["script_execution"], "guardian-test", human_present=human_present)


def test_guardian_deny_does_not_use_legacy_classifier(reviewer):
    result, denied = _gate()
    assert result["approved"] is False
    assert denied is True
    assert "sensitive reviewer rationale" not in result["message"]


def test_guardian_allow_is_one_operation_without_pattern_grant(reviewer):
    reviewer.review_current_action = lambda *args: SimpleNamespace(
        outcome=_Outcome.ALLOW, code="allowed", reason="private review")
    result, denied = _gate()
    assert result["approved"] is True
    assert result.get("guardian_approved") is True
    assert denied is False
    assert not approval.is_approved("guardian-test", "script_execution")


def test_stale_allow_escalates_instead_of_auto_approving(reviewer):
    reviewer.review_current_action = lambda *args: SimpleNamespace(
        outcome=_Outcome.ALLOW, code="allowed", reason="")
    reviewer.validate_current_action = lambda command: False
    result, denied = _gate(human_present=True)
    assert result is None
    assert denied is False


@pytest.mark.parametrize("backend", ["unknown", False, {}, ""])
def test_invalid_backend_never_falls_back_to_legacy(reviewer, monkeypatch, backend):
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"backend": backend})
    result, denied = _gate(human_present=True)
    assert result is None
    assert denied is False


def test_guardian_failure_without_human_blocks_even_if_headless_is_trusted(reviewer):
    reviewer.review_current_action = lambda *args: SimpleNamespace(
        outcome=_Outcome.ESCALATE, code="unavailable", reason="")
    result, denied = _gate()
    assert result["approved"] is False
    assert result.get("guardian_escalated") is True
    assert denied is False


def test_guardian_deny_owner_override_has_no_persisted_scope(reviewer):
    result, denied = _gate(human_present=True)
    assert result is None
    assert denied is True


def test_unchecked_account_preserves_legacy_even_with_global_guardian(reviewer):
    reviewer.current_backend = lambda: "legacy"
    result, denied = _gate()
    assert result["approved"] is True
    assert not result.get("guardian_approved")
    assert denied is False


def test_manual_only_action_does_not_invoke_guardian(reviewer, monkeypatch):
    reviewer.review_current_action = lambda *args: pytest.fail("manual-only action reached Guardian")
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "_presence", lambda callback=None: (lambda *a, **k: "deny", True, False, False))
    monkeypatch.setattr(approval, "is_approved", lambda *args: False)
    result = approval.request_tool_approval("write_file", "operator requires confirmation")
    assert result["approved"] is False


def test_backend_resolver_failure_never_falls_back_when_global_is_legacy(reviewer, monkeypatch):
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"backend": "legacy", "mode": "smart"})
    def unavailable():
        raise RuntimeError("unavailable")
    reviewer.current_backend = unavailable
    result, denied = _gate(human_present=True)
    assert result is None
    assert denied is False


def test_stale_allow_is_revoked_before_manual_recovery(reviewer):
    events = []
    reviewer.review_current_action = lambda *args: SimpleNamespace(outcome=_Outcome.ALLOW)
    reviewer.validate_current_action = lambda command: False
    reviewer.revoke_current_allow = lambda: events.append("revoked")
    assert _gate(human_present=True) == (None, False)
    assert events == ["revoked"]


def _no_human(monkeypatch):
    monkeypatch.setattr(approval, "_presence", lambda callback=None: (None, False, False, False))
    monkeypatch.setattr(approval, "_unattended_contexts", lambda: [])
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda command: False)
    monkeypatch.setattr(approval, "is_approved", lambda *args: False)
    monkeypatch.setattr(approval, "_tirith_scan", lambda command: {"action": "allow"})


@pytest.mark.parametrize("failure", [False, True])
def test_full_terminal_guard_without_ui_blocks_guardian_failure(reviewer, monkeypatch, failure):
    _no_human(monkeypatch)
    def review(*args):
        if failure:
            raise OSError("private connection detail")
        return SimpleNamespace(outcome=_Outcome.ESCALATE)
    reviewer.review_current_action = review
    result = approval.check_all_command_guards("python -c 'print(1)'", "local")
    assert result["approved"] is False
    assert result.get("guardian_escalated") is True
    assert "private" not in result["message"]


def test_full_python_guard_without_ui_blocks_guardian_escalation(reviewer, monkeypatch):
    _no_human(monkeypatch)
    reviewer.review_current_action = lambda *args: SimpleNamespace(outcome=_Outcome.ESCALATE)
    result = approval.check_execute_code_guard("print(1)", "local")
    assert result["approved"] is False
    assert result.get("guardian_escalated") is True


def test_explicit_headless_trust_preserves_approval_bypass(reviewer, monkeypatch):
    _no_human(monkeypatch)
    monkeypatch.setattr(approval, "_unattended_contexts", lambda: [SimpleNamespace(mode=lambda: "approve")])
    reviewer.review_current_action = lambda *args: pytest.fail("explicitly trusted bypass reached Guardian")
    assert approval.check_all_command_guards("python -c 'print(1)'", "local")["approved"] is True
    assert approval.check_execute_code_guard("print(1)", "local")["approved"] is True


def test_guardian_preserves_observer_hooks_without_observer_authority(reviewer, monkeypatch):
    events = []
    def observe(name, **payload):
        events.append((name, payload))
        return {"choice": "approve"}
    monkeypatch.setattr(approval_context, "_fire_approval_hook", observe)
    result, _ = _gate()
    assert result["approved"] is False
    assert [name for name, _ in events] == ["pre_approval_request", "post_approval_response"]
    assert events[1][1]["decided_by"] == "codex_guardian"
    assert events[1][1]["choice"] == "smart_deny"
    assert events[0][1]["pattern_keys"] == ["script_execution"]
    assert "sensitive reviewer rationale" not in str(events)
