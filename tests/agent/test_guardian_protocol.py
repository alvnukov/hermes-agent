"""Codex Guardian's wire contract and assessment parsing, without live inference."""

import pytest

from agent.guardian_protocol import (
    GuardianProtocolError,
    build_review_request,
    parse_assessment,
    select_review_model,
)


def _catalog(**changes):
    return [{
        "slug": "codex-auto-review",
        "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}],
        "default_reasoning_level": "medium",
        "use_responses_lite": True,
        **changes,
    }]


@pytest.mark.parametrize("changes", [{"requires_sandboxed_review": True},
    {"model_messages": {"auto_review": {"policy": "different server policy"}}},
    {"model_messages": {"auto_review": {"policy_template": "different server template"}}}])
def test_changed_review_environment_or_policy_requires_manual(changes):
    with pytest.raises(GuardianProtocolError):
        select_review_model(_catalog(**changes), "main", None, None)


def test_pinned_catalog_template_without_extra_policy_slot_is_supported():
    from agent import guardian_protocol
    template = (guardian_protocol._RESOURCES / "policy_template.md").read_text(encoding="utf-8-sig")
    catalog = _catalog(model_messages={"auto_review": {"policy_template": template.replace("{{ extra_policy }}\n\n", "")}})
    assert select_review_model(catalog, "main", None, None) == ("codex-auto-review", "low", True)


def test_review_wire_marks_real_parent_and_uses_lite_contract():
    action = {"tool": "terminal", "command": "pwd", "cwd": "/workspace"}
    request = build_review_request(
        parent_response_id="resp-parent", model="codex-auto-review", reasoning_effort="low",
        transcript="user: inspect the working directory", action=action,
        policy="Only inspect local files.", use_responses_lite=True,
    )
    assert request["model"] == "codex-auto-review"
    assert request["extra_headers"]["x-codex-guardian"] == "reviewer"
    assert request["extra_headers"]["x-openai-subagent"] == "guardian"
    assert request["extra_headers"]["x-openai-internal-codex-responses-lite"] == "true"
    metadata = request["extra_body"]["client_metadata"]
    assert metadata == {"parent_response_id": "resp-parent", "x-openai-subagent": "guardian"}
    assert request["reasoning"] == {"effort": "low", "context": "all_turns"}
    assert request["extra_body"]["text"]["format"]["strict"] is False
    assert request["extra_body"]["text"]["format"]["schema"]["required"] == ["outcome"]
    assert request["store"] is False and request["stream"] is True
    assert request["parallel_tool_calls"] is False
    assert "tools" not in request and "service_tier" not in request
    assert "resp-parent" not in str(request["input"])
    assert "Only inspect local files." in request["input"][0]["content"][0]["text"]
    assert '"command": "pwd"' in request["input"][1]["content"][-2]["text"]
    assert action == {"tool": "terminal", "command": "pwd", "cwd": "/workspace"}


def test_review_non_lite_request_omits_lite_marker():
    request = build_review_request(
        parent_response_id="resp-parent", model="codex-auto-review", reasoning_effort="medium",
        transcript="user: inspect files", action={"tool": "read_file"}, policy="",
        use_responses_lite=False,
    )
    assert "x-openai-internal-codex-responses-lite" not in request["extra_headers"]
    assert request["reasoning"] == {"effort": "medium"}
    assert request["tools"] == []


def test_model_choice_is_advertised_subscription_reviewer_only():
    assert select_review_model(_catalog(), "main-model", None, None) == (
        "codex-auto-review", "low", True,
    )
    assert select_review_model(_catalog(supported_reasoning_levels=[{"effort": "medium"}]),
                               "main-model", None, None) == (
        "codex-auto-review", "medium", True,
    )
    for entries, override, expected in [
        ([], None, "capability_unavailable"),
        ([{"slug": "main-model"}], None, "capability_unavailable"),
        (_catalog(), "ordinary-model", "unsupported_guardian_model"),
        (_catalog(supported_reasoning_levels=[]), None, "capability_unavailable"),
        (_catalog() + [{"slug": "main-model", "auto_review_model_override": "ordinary-model"}],
         None, "unsupported_guardian_model"),
    ]:
        with pytest.raises(GuardianProtocolError) as error:
            select_review_model(entries, "main-model", override, None)
        assert error.value.code == expected


def test_requested_effort_requires_live_support():
    assert select_review_model(_catalog(), "main-model", None, "medium") == (
        "codex-auto-review", "medium", True,
    )
    with pytest.raises(GuardianProtocolError) as error:
        select_review_model(_catalog(), "main-model", None, "high")
    assert error.value.code == "capability_unavailable"


@pytest.mark.parametrize("payload,risk,authorization", [
    ('{"outcome":"allow"}', "low", "unknown"),
    ('{"outcome":"deny"}', "high", "unknown"),
    ('{"outcome":"allow","risk_level":"high","user_authorization":"high","rationale":"Authorized."}',
     "high", "high"),
    (' \n{"outcome":"deny","rationale":"Private destination."}\n ', "high", "unknown"),
])
def test_parser_retains_omitted_field_defaults(payload, risk, authorization):
    result = parse_assessment(payload)
    assert result.risk_level == risk
    assert result.user_authorization == authorization
    assert result.outcome in {"allow", "deny"}
    assert result.rationale
    assert result.rationale not in repr(result)


@pytest.mark.parametrize("payload", [
    "not an assessment", "[]", "{}", '{"outcome":"approve"}',
    '{"outcome":"allow","risk_level":"safe"}',
    '{"outcome":"allow","user_authorization":3}',
    '{"outcome":"allow","rationale":{}}',
    '{"outcome":"allow","outcome":"deny"}',
    '{"outcome":"allow","risk_level":null}',
    '{"outcome":"allow","user_authorization":null}',
    '{"outcome":"allow","rationale":null}',
    '{"outcome":"allow","unexpected":true}',
    '{"outcome":"allow","unexpected":NaN}',
    'The action is denied. {"outcome":"allow"}',
    '```json\n{"outcome":"deny"}\n```',
    '{"outcome":"allow"} {"outcome":"deny"}',
])
def test_invalid_assessment_has_safe_error_without_payload(payload):
    with pytest.raises(GuardianProtocolError) as error:
        parse_assessment(payload)
    assert error.value.code == "invalid_assessment"
    assert payload not in str(error.value)
