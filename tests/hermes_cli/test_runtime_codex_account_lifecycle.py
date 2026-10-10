"""Explicit account controls must also bind the provider resolution ladder."""
import json

import pytest

from hermes_cli.auth import AuthError
from hermes_cli.runtime_provider import _resolve_from_pool


@pytest.mark.parametrize("rows", [[], [{
    "id": "disabled", "label": "Disabled account", "source": "manual:device_code",
    "auth_type": "oauth", "access_token": "disabled-access", "enabled": False, "priority": 0,
}]])
def test_managed_codex_selection_never_falls_back_to_singleton(tmp_path, monkeypatch, rows):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    (tmp_path / "auth.json").write_text(json.dumps({
        "version": 1,
        "credential_pool_selections": {"openai-codex": True},
        "credential_pool": {"openai-codex": rows},
    }))
    with pytest.raises(AuthError, match="account"):
        _resolve_from_pool("openai-codex", "openai-codex", {}, None, None, "gpt-5")
