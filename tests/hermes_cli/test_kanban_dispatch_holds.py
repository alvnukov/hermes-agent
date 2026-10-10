"""Persisted scheduling holds survive status changes and fence both claim lanes."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path):
    with kbc.connect_closing(tmp_path / "kanban.db") as db:
        yield db


@pytest.mark.parametrize("kind", ["block_loop", "manual_approval", "policy_gate"])
def test_hold_requires_exact_release_and_survives_reopen(conn, kind):
    from hermes_cli import kanban_db_holds as holds
    tid = kb.create_task(conn, title="Held task")
    with kb.write_txn(conn):
        hold = holds.set_dispatch_hold(conn, tid, kind=kind, cause_key="cause:1", resume_status="ready")
        assert holds.set_dispatch_hold(conn, tid, kind=kind, cause_key="cause:1", resume_status="ready") == hold
        with pytest.raises(ValueError, match="active hold"):
            holds.set_dispatch_hold(conn, tid, kind=kind, cause_key="other", resume_status="ready")
    assert kb.claim_task(conn, tid) is None
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    with kbc.connect_closing(Path(db_path)) as reopened:
        assert holds.get_dispatch_hold(reopened, tid) == hold
        task = kb.get_task(reopened, tid)
        assert task is not None and task.dispatch_hold == hold
        with kb.write_txn(reopened):
            assert not holds.release_dispatch_hold(reopened, tid, expected_hold_id="stale", wake_kind="operator", actor="operator", evidence={"reason": "Ready"})
            assert holds.release_dispatch_hold(reopened, tid, expected_hold_id=hold["hold_id"], wake_kind="operator", actor="operator", evidence={"reason": "Ready"})
        assert kb.claim_task(reopened, tid) is not None
    assert [e.kind for e in kb.list_events(conn, tid)].count("dispatch_held") == 1
    assert [e.kind for e in kb.list_events(conn, tid)].count("dispatch_hold_released") == 1


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_malformed_hold_fails_closed_and_release_allows_only_one_claim(conn, lane):
    from hermes_cli import kanban_db_holds as holds
    tid = kb.create_task(conn, title="Concurrent hold", assignee="author")
    if lane == "review":
        assert kb.request_review(conn, tid, reviewer="reviewer", summary="Candidate")
    conn.execute("UPDATE tasks SET dispatch_hold = 'not-json' WHERE id = ?", (tid,))
    claim = kb.claim_review_task if lane == "review" else kb.claim_task
    assert holds.get_dispatch_hold(conn, tid)
    assert claim(conn, tid) is None
    conn.execute("UPDATE tasks SET dispatch_hold = NULL WHERE id = ?", (tid,))
    with kb.write_txn(conn):
        hold = holds.set_dispatch_hold(conn, tid, kind="manual_approval", cause_key="approval", resume_status=lane)
        assert holds.release_dispatch_hold(conn, tid, expected_hold_id=hold["hold_id"], wake_kind="operator", actor="operator", evidence={"reason": "Approved"})
    path = conn.execute("PRAGMA database_list").fetchone()[2]

    def attempt(lock):
        with kbc.connect_closing(Path(path)) as claimant:
            return claim(claimant, tid, claimer=lock) is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["one", "two"]))
    assert sum(results) == 1
    assert len([e for e in kb.list_events(conn, tid) if e.kind == "claimed"]) == 1
