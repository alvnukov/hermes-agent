"""A bounded, request-local reviewer connection using Hermes' Codex transport."""
from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import time
from typing import Callable

from agent.guardian_protocol import (GuardianAssessment, GuardianProtocolError, build_review_request,
                                     parse_assessment, select_review_model)
from agent.guardian_provenance import trusted_codex_base
from agent.guardian_logging import private_provider_logs
from hermes_cli.guardian_config import guardian_settings


@dataclass(frozen=True)
class ReviewResponse:
    assessment: GuardianAssessment
    response_id: str | None
    usage: dict = field(repr=False)


def _client_factory(identity, entry, remaining):
    from agent.auxiliary_client import _create_openai_client
    from agent.process_bootstrap import build_keepalive_http_client
    from agent.codex_headers import codex_cloudflare_headers
    http = build_keepalive_http_client(identity.base_url)
    if http is None:
        raise GuardianProtocolError("transport_unavailable")
    http.follow_redirects = False
    return _create_openai_client(api_key=entry.runtime_api_key, base_url=identity.base_url,
                                 http_client=http, timeout=remaining, max_retries=0,
                                 default_headers=codex_cloudflare_headers(entry.runtime_api_key))


def _remaining(deadline: float, cancelled: Callable[[], bool]) -> float:
    if cancelled():
        raise GuardianProtocolError("cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GuardianProtocolError("timeout")
    return remaining


def _usage(value) -> dict:
    result = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        count = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            result[key] = count
    return result


def _retry_delay(error, attempt: int) -> float:
    response = getattr(error, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                return 0.2 * (2 ** attempt)
    return 0.2 * (2 ** attempt)


class GuardianTransport:
    """No model/account/provider fallback, cache, WS state, or mutable shared headers."""

    def __init__(self, *, client_factory=_client_factory):
        self._client_factory = client_factory

    def _catalog(self, client, identity, entry, deadline, cancelled):
        from agent.codex_headers import codex_cloudflare_headers
        from agent.model_metadata import fetch_codex_catalog_entries
        headers = {**codex_cloudflare_headers(entry.runtime_api_key),
                   "Authorization": f"Bearer {entry.runtime_api_key}"}
        def get(url):
            return client._client.get(url, headers=headers, timeout=_remaining(deadline, cancelled),
                                      follow_redirects=False)
        entries, status = fetch_codex_catalog_entries(get, identity.base_url)
        if status != 200 or not entries:
            raise GuardianProtocolError("capability_unavailable")
        return entries

    def _attempt(self, client, request, deadline, cancelled, on_usage):
        from agent.codex_runtime import _consume_codex_event_stream
        from agent.sdk_transform_bypass import bypass_sdk_request_transform
        def interrupt():
            _remaining(deadline, cancelled)
            return False
        kwargs = {**request, "timeout": _remaining(deadline, cancelled)}
        stream = client.responses.create(**bypass_sdk_request_transform(kwargs))
        try:
            result = _consume_codex_event_stream(stream, model=request["model"], interrupt_check=interrupt)
        finally:
            stream.close()
        _remaining(deadline, cancelled)
        if not result.guardian_completed or not result.id:
            raise GuardianProtocolError("incomplete_review")
        usage = _usage(result.usage)
        if on_usage is not None:
            on_usage(result.id, usage)
        # Any attempted investigation call is unsupported and cannot become an ALLOW.
        if any(getattr(item, "type", "") != "message" and "call" in getattr(item, "type", "")
               for item in result.output):
            raise GuardianProtocolError("investigation_unavailable")
        assessment = parse_assessment(result.output_text)
        return ReviewResponse(assessment, result.id, usage)

    def _review_with_retries(self, client, request, settings, deadline, cancelled, on_usage):
        import httpx
        from openai import APIConnectionError, APIStatusError, APITimeoutError
        for attempt in range(settings["max_retries"] + 1):
            try:
                return self._attempt(client, request, deadline, cancelled, on_usage)
            except (APITimeoutError, httpx.TimeoutException):
                raise GuardianProtocolError("timeout") from None
            except APIStatusError as error:
                status = error.status_code
                if status != 429 and status < 500:
                    raise GuardianProtocolError(f"http_{status}") from None
                if attempt >= settings["max_retries"]:
                    raise GuardianProtocolError(f"http_{status}") from None
                delay = _retry_delay(error, attempt)
                remaining = _remaining(deadline, cancelled)
                if delay >= remaining:
                    raise GuardianProtocolError("rate_limited" if status == 429 else "timeout") from None
                until = time.monotonic() + delay
                while time.monotonic() < until:
                    time.sleep(min(0.1, _remaining(deadline, cancelled), until - time.monotonic()))
            except (APIConnectionError, httpx.TransportError):
                raise GuardianProtocolError("network_error") from None
        raise GuardianProtocolError("review_unavailable")

    @private_provider_logs()
    def review(self, identity, entry, *, parent_response_id: str, parent_model: str,
               transcript: str, action: dict, policy: str, config: dict,
               cancelled=lambda: False, deadline: float | None = None,
               on_usage: Callable[[str, dict], None] | None = None) -> ReviewResponse:
        settings = guardian_settings({"codex_guardian": config})
        import httpx
        from openai import OpenAIError
        deadline = deadline if deadline is not None else time.monotonic() + settings["timeout"]
        if not trusted_codex_base(identity.base_url):
            raise GuardianProtocolError("untrusted_endpoint")
        client = None
        try:
            client = self._client_factory(identity, entry, _remaining(deadline, cancelled))
            entries = self._catalog(client, identity, entry, deadline, cancelled)
            model, effort, lite = select_review_model(entries, parent_model, settings["model"],
                                                      settings["reasoning_effort"])
            request = build_review_request(parent_response_id=parent_response_id, model=model,
                                           reasoning_effort=effort, transcript=transcript, action=action,
                                           policy=policy, use_responses_lite=lite)
            return self._review_with_retries(client, request, settings, deadline, cancelled, on_usage)
        except GuardianProtocolError:
            raise
        except (OpenAIError, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, OSError) as error:
            # Provider bodies, stream frames, prompts and OAuth fields are not diagnostic text.
            raise GuardianProtocolError("timeout" if isinstance(error, TimeoutError) else "transport_error") from None
        finally:
            if client is not None:
                client.close()
