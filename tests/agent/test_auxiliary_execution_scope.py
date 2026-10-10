"""Auxiliary requests keep their native path after the Privacore core rollback."""

import asyncio
from types import SimpleNamespace

import pytest

from agent import auxiliary_client as aux
from hermes_cli import plugins
from hermes_cli.plugins import PluginManager


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
def test_auxiliary_call_preserves_native_request(monkeypatch, mode):
    manager = PluginManager()
    monkeypatch.setattr(plugins, "_delivery_manager", lambda: manager)
    sent = []
    response = object()

    def create(**request):
        sent.append(request)
        return iter([response]) if mode == "stream" else response

    client = SimpleNamespace(
        base_url="https://provider.test/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )

    def unrelated_middleware(request, next_call, **context):
        changed = {**request, "messages": [{"role": "user", "content": "rewritten"}]}
        return next_call(changed)

    manager._middleware["llm_execution"] = [unrelated_middleware]
    request = {"model": "m", "messages": [{"role": "user", "content": "native"}]}
    if mode == "async":
        async def async_create(payload):
            return create(**payload)

        result = asyncio.run(aux._relay_async_completion(
            client, request, provider="actual", create=async_create,
        ))
    elif mode == "sync":
        result = aux._relay_sync_completion(
            client, request, provider="actual", create=lambda payload: create(**payload),
        )
    else:
        request = {**request, "stream": True}
        result = aux._relay_sync_stream(client, request, provider="actual")

    assert sent == [request]
    assert (list(result) if mode == "stream" else result) == (
        [response] if mode == "stream" else response
    )
