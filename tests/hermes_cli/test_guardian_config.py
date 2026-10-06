"""Guardian config is recognized through real loaders and rejects malformed settings."""

import pytest

from hermes_cli import config


@pytest.mark.parametrize("settings", [
    {"backend": "unknown"},
    {"backend": False},
    {"backend": "codex_guardian", "codex_guardian": "invalid"},
    {"backend": "codex_guardian", "codex_guardian": {"timeout": 0}},
    {"backend": "codex_guardian", "codex_guardian": {"timeout": float("nan")}},
    {"backend": "codex_guardian", "codex_guardian": {"max_retries": -1}},
    {"backend": "codex_guardian", "codex_guardian": {"max_context_bytes": True}},
    {"backend": "codex_guardian", "codex_guardian": {"model": []}},
])
def test_invalid_guardian_settings_have_actionable_config_error(settings):
    issues = config.validate_config_structure({"approvals": settings})
    assert any(issue.severity == "error" and "approvals" in issue.message for issue in issues)


def test_guardian_keys_load_through_profile_scoped_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "approvals:\n  backend: codex_guardian\n  codex_guardian:\n    timeout: 11\n", encoding="utf-8")
    loaded = config.load_config_readonly()
    assert loaded["approvals"]["backend"] == "codex_guardian"
    assert loaded["approvals"]["codex_guardian"]["timeout"] == 11
    assert loaded["approvals"]["codex_guardian"]["max_retries"] == 1
    assert config._validate_config_key("approvals.codex_guardian.max_context_bytes")[0] is True
