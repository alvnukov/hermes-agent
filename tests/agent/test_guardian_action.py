"""Unresolved shell grammar and mutable resources cannot reuse an ALLOW."""
import pytest

from agent.guardian_action import snapshot_action
from agent.guardian_protocol import GuardianProtocolError


@pytest.mark.parametrize("command", ["cd subdir && sh -c 'rm -rf target'", "sh -c 'rm -rf target'",
                                   "pwd; rm -rf target", "echo ok | sh", "(rm -rf target)"])
def test_shell_control_flow_requires_manual(command, tmp_path):
    with pytest.raises(GuardianProtocolError):
        snapshot_action("terminal", {"command": command}, str(tmp_path), "local")


def test_nested_directory_content_change_invalidates_snapshot(tmp_path):
    target = tmp_path / "target" / "subdir"
    target.mkdir(parents=True)
    file = target / "private.txt"
    file.write_text("before")
    args = {"command": "rm -rf target"}
    state = snapshot_action("terminal", args, str(tmp_path), "local")
    file.write_text("after!")
    assert not state.unchanged(args)


def test_script_dependencies_are_part_of_snapshot(tmp_path):
    (tmp_path / "script.py").write_text("from pathlib import Path\nPath('target').unlink()")
    (tmp_path / "target").write_text("before")
    args = {"command": "python script.py"}
    state = snapshot_action("terminal", args, str(tmp_path), "local")
    (tmp_path / "target").write_text("after!")
    assert not state.unchanged(args)


@pytest.mark.parametrize("code", ["from os import system as print; print('rm -rf target')",
                                "import os; print = os.system; print('rm -rf target')"])
def test_python_alias_cannot_hide_dynamic_resource_access(code, tmp_path):
    with pytest.raises(GuardianProtocolError):
        snapshot_action("execute_code", {"code": code}, str(tmp_path), "local")
