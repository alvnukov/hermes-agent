"""Immutable, token-free evidence from the actual Codex network attempt."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
import hashlib
import json
from urllib.parse import urlparse
import uuid

from agent.guardian_credentials import CredentialIdentity, capture_identity, guardian_requested_for_token
from agent.message_sanitization import coalesce_tool_call_id

POLICY_VERSION = "hermes-guardian-v1:8e23d1836fd22d1c920533f39851db1c7d795744"
_RESERVED = frozenset({"guardian_credits_requested", "parent_response_id"})


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def trusted_codex_base(base_url: str) -> bool:
    try:
        parsed = urlparse(base_url)
        return (parsed.scheme == "https" and parsed.hostname == "chatgpt.com"
                and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
                and parsed.path.rstrip("/") == "/backend-api/codex" and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def current_policy() -> tuple[str, str]:
    from agent.guardian_protocol import policy_fingerprint
    from tools.approval_context import _get_approval_config
    config = _get_approval_config()
    policy = config.get("smart_policy", "")
    policy = policy if isinstance(policy, str) else ""
    payload = canonical({"approvals": config, "resources": policy_fingerprint()})
    return policy, POLICY_VERSION + ":" + hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class MainAttempt:
    identity: CredentialIdentity | None
    session_id: str
    turn_id: str
    request_id: str
    attempt_id: str
    parent_model: str
    policy_version: str
    guardian_requested: bool = False


@dataclass(frozen=True)
class ReviewBinding:
    attempt: MainAttempt
    parent_response_id: str
    tool_call_id: str
    tool: str
    args_json: str = field(repr=False)
    transcript_json: str = field(repr=False)
    policy: str = field(repr=False)


def _main_metadata(request: dict, client=None) -> dict:
    result = dict(request)
    extra = dict(result.get("extra_body") or {})
    metadata = {**(result.pop("client_metadata", {}) or {}), **(extra.get("client_metadata") or {})}
    metadata = {key: value for key, value in metadata.items() if key not in _RESERVED
                and not (key == "x-openai-subagent" and value == "guardian")}
    metadata["guardian_credits_requested"] = "true"
    extra["client_metadata"] = metadata
    result["extra_body"] = extra
    result["extra_headers"] = {key: value for key, value in (result.get("extra_headers") or {}).items()
                               if str(key).lower() != "x-codex-guardian"
                               and not (str(key).lower() == "x-openai-subagent" and value == "guardian")}
    # SDK merging is case-sensitive before constructing HTTP headers. Remove each exact default
    # spelling locally; shared defaults must remain untouched for other concurrent requests.
    from openai import Omit
    for key, value in (getattr(client, "default_headers", {}) or {}).items():
        if str(key).lower() == "x-codex-guardian" or (str(key).lower() == "x-openai-subagent" and value == "guardian"):
            result["extra_headers"][key] = Omit()
    return result


def prepare_main_request(agent, client, request: dict) -> tuple[dict, MainAttempt | None]:
    """Capture by the actual client's token, immediately before each physical POST."""
    base = str(getattr(client, "base_url", ""))
    pool = getattr(agent, "_credential_pool", None)
    token = getattr(client, "api_key", "")
    requested = False
    if pool is not None and getattr(pool, "provider", None) == "openai-codex":
        requested = guardian_requested_for_token(pool, token)
    if not trusted_codex_base(base):
        identity = None
    else:
        identity = capture_identity(pool, token, base)
    if identity is None and not requested:
        return request, None
    requested = identity.guardian_enabled if identity is not None else requested
    version = current_policy()[1] if requested else ""
    attempt = MainAttempt(identity, str(getattr(agent, "session_id", "") or ""),
                          str(getattr(agent, "_current_turn_id", "") or ""),
                          str(getattr(agent, "_current_api_request_id", "") or ""),
                          uuid.uuid4().hex, str(request.get("model", "")), version, requested)
    return (_main_metadata(request, client) if requested and identity is not None else request), attempt


def unproved_main_attempt(agent, client, request: dict) -> MainAttempt | None:
    """Keep opt-in failures manual when middleware never opens a physical request."""
    _, attempt = prepare_main_request(agent, client, request)
    return replace(attempt, identity=None) if attempt is not None else None


def bind_response_actions(agent, response, assistant_message, api_messages) -> None:
    """Only the accepted response contributes bindings; retries never inherit a prior ID."""
    agent._guardian_actions = {}
    agent._guardian_response_backend = None
    attempt = getattr(response, "_guardian_attempt", None)
    if not isinstance(attempt, MainAttempt):
        return
    agent._guardian_response_backend = "codex_guardian" if attempt.guardian_requested else "legacy"
    if attempt.identity is None or not attempt.guardian_requested:
        return
    calls = getattr(assistant_message, "tool_calls", None) or []
    counts = Counter(coalesce_tool_call_id(call) for call in calls)
    policy, version = current_policy()
    parent = getattr(response, "id", None)
    complete = getattr(response, "guardian_completed", False)
    if not complete or not isinstance(parent, str) or not parent or version != attempt.policy_version:
        return
    try:
        transcript = canonical(api_messages)
        for call in calls:
            call_id = coalesce_tool_call_id(call)
            if not call_id or counts[call_id] != 1:
                continue
            function = getattr(call, "function", call)
            args = json.loads(function.arguments)
            if not isinstance(args, dict):
                continue
            binding = ReviewBinding(attempt, parent, call_id, function.name, canonical(args), transcript, policy)
            agent._guardian_actions[call_id] = binding
    except (ValueError, TypeError, AttributeError):
        agent._guardian_actions = {}
