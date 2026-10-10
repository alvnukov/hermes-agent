"""Append-only worker log attempt boundaries and file rotation."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Optional

WORKER_LOG_START = "HERMES_KANBAN_WORKER_START"
_WORKER_LOG_START_RE = re.compile(r"^" + WORKER_LOG_START + r"[^\S\n]*$", re.MULTILINE)


def current_worker_log_tail(task_id: str, board: Optional[str] = None) -> str:
    """Retain the shared log but never read an earlier attempt as the current worker.

    A short startup failure can leave the prior run's summary and exit trailer
    inside the tail window. The dispatcher writes a boundary before each spawn;
    logs predating that boundary keep the legacy best-effort behaviour.
    """
    from hermes_cli import kanban_db_dispatch as _dispatch
    raw = _dispatch._kb.read_worker_log(task_id, tail_bytes=4000, board=board) or ""
    starts = list(_WORKER_LOG_START_RE.finditer(raw))
    return raw[starts[-1].end():] if starts else raw


def rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = 1,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``: ``<log>`` → ``<log>.1``,
    older generations shift up to ``backup_count``.
    """
    from hermes_cli import kanban_db_dispatch as _dispatch
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return
        backup_count = _dispatch._positive_int(backup_count, _dispatch.DEFAULT_LOG_BACKUP_COUNT, minimum=0)
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _dispatch._rotated_log_path(log_path, backup_count)
        with contextlib.suppress(OSError):
            if oldest.exists():
                oldest.unlink()
        for generation in range(backup_count - 1, 0, -1):
            src = _dispatch._rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            with contextlib.suppress(OSError):
                src.rename(_dispatch._rotated_log_path(log_path, generation + 1))
        log_path.rename(_dispatch._rotated_log_path(log_path, 1))
    except OSError:
        pass
