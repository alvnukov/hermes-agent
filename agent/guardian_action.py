"""Bounded local state evidence for a single exact reviewable action."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import shlex

from agent.guardian_protocol import GuardianProtocolError
from agent.guardian_provenance import canonical

_PURE_CALLS = frozenset({"print", "len", "str", "int", "float", "bool", "range", "sum", "min", "max", "abs", "round"})


@dataclass(frozen=True)
class ResourceState:
    path: str
    resolved: str
    stamp: tuple
    digest: str | None
    content: str | None = field(repr=False)


def _stamp(stat) -> tuple:
    return (stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _resource(path: Path, budget: list[int] | None = None) -> ResourceState:
    budget = budget if budget is not None else [0, 0]
    budget[0] += 1
    if budget[0] > 128:
        raise GuardianProtocolError("resource_state_too_large")
    resolved = str(path.resolve())
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return ResourceState(str(path), resolved, (), None, None)
    stamp = _stamp(stat) + _stamp(path.stat())
    content, digest = None, None
    if path.is_file():
        if stat.st_size > 262144:
            raise GuardianProtocolError("resource_state_too_large")
        data = path.read_bytes()
        budget[1] += len(data)
        if budget[1] > 2097152:
            raise GuardianProtocolError("resource_state_too_large")
        digest = hashlib.sha256(data).hexdigest()
        if path.suffix.lower() in {".py", ".sh", ".js", ".rb", ".ps1"}:
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                raise GuardianProtocolError("resource_state_unavailable") from None
    elif path.is_dir():
        if path.is_symlink():
            raise GuardianProtocolError("dynamic_resources")
        entries = []
        for child in path.iterdir():
            state = _resource(child, budget)
            entries.append((child.name, state.resolved, state.stamp, state.digest))
        digest = hashlib.sha256(canonical(sorted(entries)).encode()).hexdigest()
    else:
        raise GuardianProtocolError("resource_state_unavailable")
    return ResourceState(str(path), resolved, stamp, digest, content)


def _literal_path(node, cwd: Path) -> Path:
    value = node.value if isinstance(node, ast.Constant) else None
    if not isinstance(value, str):
        raise GuardianProtocolError("dynamic_resources")
    target = Path(value)
    return target if target.is_absolute() else cwd / target


def _python_call_paths(node: ast.Call, cwd: Path) -> set[Path]:
    function = node.func
    name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
    if name in _PURE_CALLS:
        return set()
    methods = {"unlink", "rmdir", "read_text", "read_bytes", "write_text", "write_bytes", "touch",
               "exists", "is_file", "is_dir", "stat", "chmod", "rename", "replace"}
    receiver = getattr(function, "value", None)
    if name in methods and isinstance(receiver, ast.Call) and getattr(receiver.func, "id", "") == "Path":
        paths = {_literal_path(receiver.args[0], cwd)} if receiver.args else set()
        if name in {"rename", "replace"} and node.args:
            paths.add(_literal_path(node.args[0], cwd))
        if not paths:
            raise GuardianProtocolError("dynamic_resources")
        return paths
    if name in {"open", "Path", "remove", "unlink", "rmdir", "rmtree"} and node.args:
        return {_literal_path(node.args[0], cwd)}
    raise GuardianProtocolError("dynamic_resources")


def _python_paths(code: str, cwd: Path) -> set[Path]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        raise GuardianProtocolError("resource_state_unavailable") from None
    targets = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in _PURE_CALLS:
            raise GuardianProtocolError("dynamic_resources")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module]
            if any(module not in {"pathlib", "os", "shutil", "math"} for module in modules) or any(alias.asname for alias in node.names):
                raise GuardianProtocolError("dynamic_resources")
        if isinstance(node, ast.Call):
            targets.update(_python_call_paths(node, cwd))
    return targets


def _terminal_paths(command: str, cwd: Path) -> set[Path]:
    # Expansion cannot be proven from the command string; require human approval.
    if any(marker in command for marker in ("$", "`", "*", "?", "<<", "\n")):
        raise GuardianProtocolError("dynamic_resources")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        raise GuardianProtocolError("invalid_action") from None
    targets = set()
    if not tokens or any(token and set(token) <= set(";&|()<>") for token in tokens):
        raise GuardianProtocolError("dynamic_resources")
    python = Path(tokens[0]).name in {"python", "python3", "python3.14", "python3.13", "python3.12", "python3.11"}
    if "-c" in tokens and not python:
        raise GuardianProtocolError("dynamic_resources")
    for index, token in enumerate(tokens):
        if token.startswith("-") and ("=" in token or "/" in token):
            raise GuardianProtocolError("dynamic_resources")
        if token.startswith("-") or "://" in token:
            continue
        candidate = Path(token)
        candidate = candidate if candidate.is_absolute() else cwd / candidate
        if index and tokens[index - 1] == "-c" and python:
            targets.update(_python_paths(token, cwd))
            continue
        if index or "/" in token or candidate.exists():
            targets.add(candidate)
        if candidate.is_file() and candidate.suffix == ".py":
            targets.update(_python_paths(_resource(candidate).content or "", cwd))
        if candidate.is_file() and candidate.suffix in {".sh", ".js", ".rb", ".ps1"}:
            raise GuardianProtocolError("dynamic_resources")
    return targets


@dataclass(frozen=True)
class ActionSnapshot:
    tool: str
    args_json: str
    cwd: str
    env_type: str
    cwd_identity: tuple
    resources: tuple[ResourceState, ...]

    def unchanged(self, args: dict, cwd: str | None = None) -> bool:
        if canonical(args) != self.args_json or (cwd is not None and str(Path(cwd).resolve()) != self.cwd):
            return False
        current = Path(self.cwd).stat()
        if (current.st_dev, current.st_ino) != self.cwd_identity:
            return False
        return all(_resource(Path(resource.path)) == resource for resource in self.resources)

    def descriptor(self, args: dict) -> dict:
        return {"tool": self.tool, "args": args, "cwd": self.cwd, "environment": self.env_type,
                "sandbox": "Hermes local backend; existing independent approval and hard-deny rules apply",
                "permissions": args.get("sandbox_permissions", "local configured tool permissions"),
                "resources": [{"path": resource.path, "resolved": resource.resolved,
                               "exists": bool(resource.stamp), "sha256": resource.digest,
                               "script_content": resource.content} for resource in self.resources]}


def snapshot_action(tool: str, args: dict, cwd: str, env_type: str) -> ActionSnapshot:
    if env_type != "local":
        raise GuardianProtocolError("environment_unavailable")
    root = Path(cwd).resolve()
    state = root.stat()
    if tool == "terminal":
        paths = _terminal_paths(args["command"], root)
    elif tool == "execute_code":
        paths = _python_paths(args["code"], root)
    else:
        raise GuardianProtocolError("tool_unavailable")
    if len(paths) > 32:
        raise GuardianProtocolError("resource_state_too_large")
    resources = tuple(_resource(path) for path in sorted(paths))
    return ActionSnapshot(tool, canonical(args), str(root), env_type, (state.st_dev, state.st_ino), resources)
