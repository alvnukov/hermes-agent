#!/usr/bin/env python3
"""Opt-in subscription Guardian smoke check; proposes one tool and never executes it.

Invoke from the checkout with:
  python scripts/guardian_smoke.py --live --account EXACT_ENTRY_ID --model MAIN_MODEL

Both model requests are real and may consume subscription usage. Their billing
treatment is unknown. Accounts are never selected automatically or printed.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import NoReturn
import uuid


class _SmokeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _SmokeError("invalid_cli")


def _proposal(response) -> dict:
    """Accept only one complete real proposal; never invent arguments or call IDs."""
    if not getattr(response, "guardian_completed", False) or not getattr(response, "id", None):
        raise _SmokeError("incomplete_main_response")
    calls = [item for item in response.output if "call" in getattr(item, "type", "")]
    if len(calls) != 1 or calls[0].type != "function_call":
        raise _SmokeError("proposal_unavailable")
    call = calls[0]
    if call.name != "terminal" or not isinstance(call.call_id, str) or not call.call_id:
        raise _SmokeError("proposal_unavailable")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate argument")
            value[key] = item
        return value

    def invalid_constant(_value):
        raise ValueError("non-JSON argument")

    try:
        args = json.loads(call.arguments, object_pairs_hook=unique,
                          parse_constant=invalid_constant)
    except (TypeError, ValueError):
        raise _SmokeError("malformed_proposal") from None
    if args != {"command": "pwd"}:
        raise _SmokeError("unexpected_proposal")
    return {"tool_call_id": call.call_id, "tool": call.name, "arguments": args}


def _run_live(account: str, model: str) -> dict:
    # Keep imports/account reads below the explicit opt-in boundary. No executor
    # is imported: the only external operations are OAuth, catalog and Responses.
    from agent.credential_pool import CredentialPool, load_pool
    from agent.guardian_credentials import capture_identity, resolve_pinned, PinnedCredentialError
    from agent.guardian_protocol import GuardianProtocolError
    from agent.guardian_provenance import canonical, current_policy, prepare_main_request, trusted_codex_base
    from agent.guardian_transport import GuardianTransport, _client_factory, _remaining, _usage
    from agent.codex_responses_adapter import _preflight_codex_api_kwargs
    from agent.codex_runtime import _consume_codex_event_stream
    from agent.sdk_transform_bypass import bypass_sdk_request_transform
    from hermes_cli.auth_codex import _codex_pool_route_base_url
    from hermes_cli.guardian_config import guardian_settings
    from tools.approval_context import _get_approval_config
    import httpx
    from openai import APIError, APIStatusError, APITimeoutError

    try:
        pool = load_pool("openai-codex")
        if pool is None:
            raise _SmokeError("account_unavailable")
        # resolve_target also accepts labels/indices; the equality check narrows
        # this command to the exact connected credential entry ID only.
        _index, entry, _error = pool.resolve_target(account)
        if entry is None or entry.id != account:
            raise _SmokeError("account_unavailable")
        base_url = _codex_pool_route_base_url(entry.base_url).rstrip("/")
        if not trusted_codex_base(base_url):
            raise _SmokeError("untrusted_endpoint")
        identity = capture_identity(pool, entry.runtime_api_key, base_url)
        if identity is None or identity.entry_id != account:
            raise _SmokeError("subscription_identity_missing")
        if not identity.guardian_enabled:
            raise _SmokeError("guardian_disabled")
        entry = resolve_pinned(pool, identity)
        # A resolved refresh may replace the original pool's cached token. This
        # one-row view holds the same credential object without changing cursors,
        # leases, selected accounts or persisted controls.
        request_pool = CredentialPool("openai-codex", [entry])
        session_id = uuid.uuid4().hex
        agent = SimpleNamespace(_credential_pool=request_pool, session_id=session_id,
                                _current_turn_id=uuid.uuid4().hex,
                                _current_api_request_id=uuid.uuid4().hex)
        approvals = _get_approval_config()
        settings = guardian_settings(approvals)
        policy, _version = current_policy()
        config = approvals.get("codex_guardian", {})
        transcript = [{"role": "user", "content": (
            "This is a synthetic Guardian smoke check. Propose exactly one terminal tool call "
            "with command pwd, to report the current working directory. Do not execute it. "
            "The caller will submit the actual proposal to Guardian and will not execute any tool."
        )}]
        request = _preflight_codex_api_kwargs({
            "model": model,
            "instructions": "Produce only the requested synthetic tool proposal for a review check.",
            "input": transcript,
            "tools": [{"type": "function", "name": "terminal", "strict": False,
                       "description": "Propose a terminal command; this smoke client never executes it.",
                       "parameters": {"type": "object", "properties": {"command": {"type": "string", "enum": ["pwd"]}},
                                      "required": ["command"], "additionalProperties": False}}],
            "tool_choice": "auto", "parallel_tool_calls": False, "store": False,
            "stream": True, "include": ["reasoning.encrypted_content"],
        }, allow_stream=True)
        deadline = time.monotonic() + settings["timeout"]
        client = _client_factory(identity, entry, _remaining(deadline, lambda: False))
        try:
            request, attempt = prepare_main_request(agent, client, request)
            if attempt is None or attempt.identity != identity:
                raise _SmokeError("identity_changed")
            # Recheck the persisted checkbox/grant immediately before the main
            # request. This never consults a pool cursor or another account.
            current = resolve_pinned(pool, identity, refresh=False)
            if current.runtime_api_key != entry.runtime_api_key:
                raise _SmokeError("identity_changed")
            kwargs = {**request, "timeout": _remaining(deadline, lambda: False)}
            stream = client.responses.create(**bypass_sdk_request_transform(kwargs))
            try:
                def interrupt():
                    _remaining(deadline, lambda: False)
                    return False
                response = _consume_codex_event_stream(stream, model=model, interrupt_check=interrupt)
            finally:
                stream.close()
        finally:
            client.close()
        action = _proposal(response)
        # The reviewer receives the ID and tool proposal from that exact accepted
        # response, and fresh resolution of the same account assignment.
        entry = resolve_pinned(pool, attempt.identity)
        review = GuardianTransport().review(
            attempt.identity, entry, parent_response_id=response.id, parent_model=model,
            transcript=canonical(transcript), action=action, policy=policy, config=config)
        return {"status": "completed", "session_id": session_id,
                "main_response_id": response.id, "review_response_id": review.response_id,
                "outcome": review.assessment.outcome, "risk_level": review.assessment.risk_level,
                "user_authorization": review.assessment.user_authorization,
                "main_usage": _usage(response.usage), "review_usage": review.usage,
                "billing": "unknown"}
    except (PinnedCredentialError, GuardianProtocolError) as error:
        raise _SmokeError(error.code) from None
    except (APITimeoutError, httpx.TimeoutException, TimeoutError):
        raise _SmokeError("timeout") from None
    except APIStatusError as error:
        raise _SmokeError(f"http_{error.status_code}") from None
    except (APIError, httpx.HTTPError):
        raise _SmokeError("network_error") from None


def _emit(value: dict) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def main(argv=None) -> int:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--live", action="store_true", help="Explicitly permit the two real model requests")
    parser.add_argument("--account", help="Exact connected Codex credential entry ID (never a label or index)")
    parser.add_argument("--model", help="Main Codex model for the synthetic proposal")
    try:
        args = parser.parse_args(argv)
        if (not args.live or not args.account or args.account != args.account.strip()
                or not args.model or args.model != args.model.strip()):
            raise _SmokeError("explicit_opt_in_required")
    except _SmokeError as error:
        _emit({"status": "error", "code": error.code, "billing": "unknown"})
        return 2
    # Suppress third-party HTTP/debug logging, which can contain request bodies.
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        _emit(_run_live(args.account, args.model))
        return 0
    except _SmokeError as error:
        _emit({"status": "error", "code": error.code, "billing": "unknown"})
        return 1
    except KeyboardInterrupt:
        _emit({"status": "error", "code": "cancelled", "billing": "unknown"})
        return 130
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, AttributeError):
        _emit({"status": "error", "code": "smoke_unavailable", "billing": "unknown"})
        return 1
    finally:
        logging.disable(previous_disable)


if __name__ == "__main__":
    # A direct script invocation has scripts/ rather than the checkout on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
