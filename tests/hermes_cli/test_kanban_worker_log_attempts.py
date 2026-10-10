"""A new worker must not inherit diagnostics or exit codes from an earlier run."""

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


def _task(tmp_path, monkeypatch) -> kb.Task:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="Worker log attempts"))
    assert task is not None
    task.current_run_id = 1
    return task


def test_startup_failure_keeps_current_error_after_previous_success(tmp_path, monkeypatch):
    task = _task(tmp_path, monkeypatch)
    with dispatch._open_worker_log(task, "default") as log:
        log.write(
            ("Previous implementation succeeded\n"
             + dispatch._exit_summary_marker() + "\n"
             + dispatch.KANBAN_WORKER_EXIT_TRAILER + "0\n").encode()
        )

    task.current_run_id = 2
    with dispatch._open_worker_log(task, "default") as log:
        log.write(b"Error: Unknown skill(s): sdlc-review\n")

    diagnostic = dispatch._worker_final_output(task.id, board="default")
    assert "Unknown skill(s): sdlc-review" in diagnostic
    assert "Previous implementation succeeded" not in diagnostic
    history = kb.read_worker_log(task.id, board="default")
    assert history is not None
    assert "Previous implementation succeeded" in history


def test_new_attempt_does_not_inherit_previous_quota_exit(tmp_path, monkeypatch):
    task = _task(tmp_path, monkeypatch)
    with dispatch._open_worker_log(task, "default") as log:
        log.write((dispatch.KANBAN_WORKER_EXIT_TRAILER + str(kb.KANBAN_RATE_LIMIT_EXIT_CODE) + "\n").encode())
    assert dispatch._worker_log_exit_code(task.id, board="default") == kb.KANBAN_RATE_LIMIT_EXIT_CODE

    task.current_run_id = 2
    with dispatch._open_worker_log(task, "default") as log:
        # The boundary must reach disk before the child starts writing.
        assert dispatch._worker_log_exit_code(task.id, board="default") is None
        assert dispatch._worker_final_output(task.id, board="default") == ""
        log.write(("Current error\n" + dispatch.KANBAN_WORKER_EXIT_TRAILER + "1\n").encode())

    assert dispatch._worker_log_exit_code(task.id, board="default") == 1
