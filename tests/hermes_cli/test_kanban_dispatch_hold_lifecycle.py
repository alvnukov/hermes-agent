"""No-progress work cannot evade its hold through specification or fanout."""

from pathlib import Path
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatcher
from hermes_cli import kanban_db_graph as graph
from hermes_cli import kanban_decompose as decompose


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("author", "reviewer"):
        path = home / "profiles" / name
        path.mkdir(parents=True)
        (path / "config.yaml").write_text("{}\n")
    with kbc.connect_closing() as conn:
        yield conn


def _loop(board, phase="ready"):
    tid = kb.create_task(board, title="Held candidate", body="Unresolved gate", assignee="author")
    active = kb.claim_task(board, tid)
    assert active is not None
    if phase == "review":
        assert kb.request_review(board, tid, summary="Candidate", reviewer="reviewer", expected_run_id=active.current_run_id)
        active = kb.claim_review_task(board, tid)
        assert active is not None
    assert kb.block_task(board, tid, kind="needs_input", reason="Unchanged gate", expected_run_id=active.current_run_id)
    assert kb.unblock_task(board, tid)
    active = (kb.claim_review_task if phase == "review" else kb.claim_task)(board, tid)
    assert active is not None
    assert kb.block_task(board, tid, kind="needs_input", reason="Same gate rewritten", expected_run_id=active.current_run_id)
    return tid


@pytest.mark.parametrize("application", ["single", "fanout"])
@pytest.mark.parametrize("rewrite", [False, True])
@pytest.mark.parametrize("phase", ["ready", "review"])
def test_loop_hold_survives_specification_and_dispatch(board, application, rewrite, phase):
    tid = _loop(board, phase)
    if rewrite:
        kb.add_comment(board, tid, author="author", body="Still waiting, rephrased")
        assert kb.edit_task(board, tid, body="Same gate with a different summary")
    held = kb.get_task(board, tid)
    assert held is not None
    if application == "single":
        routing = decompose._Routing("author", "author", True, [], {"author", "reviewer"})
        decompose._apply_single(held, {"title": held.title, "body": held.body}, routing, "auto-decomposer")
    else:
        assert graph.decompose_triage_task(board, tid, root_assignee="author", author="auto-decomposer", children=[{"title": "Continue held work", "assignee": "author"}]) is None
    attempts = []
    dispatcher.dispatch_once(board, spawn_fn=lambda task, *a, **k: attempts.append(task.id), reconcile_orphans=False)
    assert attempts == []
    task = kb.get_task(board, tid)
    assert task is not None and task.status == "triage" and task.block_recurrences == 2
    assert task.dispatch_hold and task.dispatch_hold["resume_status"] == phase
    assert len(kb.list_tasks(board)) == 1


def test_held_triage_is_filtered_before_auxiliary_work(board, monkeypatch):
    from hermes_cli import kanban_specify as specify
    held = _loop(board)
    fresh = kb.create_task(board, title="Fresh idea", assignee="author", triage=True)

    def refuse_aux(*args, **kwargs):
        pytest.fail("Held work spent an auxiliary call")

    monkeypatch.setattr(decompose, "_call_aux", refuse_aux)
    monkeypatch.setattr(specify, "_call_aux", refuse_aux)
    assert not decompose.decompose_task(held).ok
    assert not specify.specify_task(held).ok
    assert decompose.list_triage_ids() == [fresh]
    assert specify.list_triage_ids() == [fresh]
    assert kb.specify_triage_task(board, fresh, title="Fresh specified")
    kb.recompute_ready(board)
    assert kb.claim_task(board, fresh) is not None


@pytest.mark.parametrize("operator_path", ["promote", "unblock"])
def test_manual_children_stay_held_until_each_operator_approval(board, operator_path):
    root = kb.create_task(board, title="Awaiting approval", assignee="author", triage=True)
    children = graph.decompose_triage_task(board, root, root_assignee="author", auto_promote=False,
                                         children=[{"title": "One", "assignee": "author"}, {"title": "Two", "assignee": "author"}])
    assert children is not None
    kb.recompute_ready(board)
    attempts = []
    dispatcher.dispatch_once(board, spawn_fn=lambda task, *a, **k: attempts.append(task.id), reconcile_orphans=False)
    assert not attempts
    first = kb.get_task(board, children[0])
    assert first is not None and first.dispatch_hold
    assert first.dispatch_hold["kind"] == "manual_approval"
    kwargs = dict(actor="operator", reason="Approved", expected_hold_id=first.dispatch_hold["hold_id"])
    if operator_path == "promote":
        assert not kb.promote_task(board, first.id, actor="operator")[0]
        assert kb.promote_task(board, first.id, actor="operator", reason="Approved", expected_hold_id=first.dispatch_hold["hold_id"])[0]
    else:
        assert not kb.unblock_task(board, first.id)
        assert kb.unblock_task(board, first.id, **kwargs)
    claimed = kb.claim_task(board, first.id)
    assert claimed is not None
    assert kb.complete_task(board, first.id, summary="Finished", expected_run_id=claimed.current_run_id)
    sibling = kb.get_task(board, children[1])
    assert sibling is not None and sibling.status == "todo" and sibling.dispatch_hold
    assert kb.claim_task(board, sibling.id) is None


@pytest.mark.parametrize("phase", ["ready", "review"])
def test_operator_resume_requires_current_hold_and_keeps_phase(board, phase):
    tid = _loop(board, phase)
    held = kb.get_task(board, tid)
    assert held is not None and held.dispatch_hold
    assert not kb.unblock_task(board, tid, actor="operator", reason="Ready", expected_hold_id="stale")
    assert kb.unblock_task(board, tid, actor="operator", reason="Gate repaired", expected_hold_id=held.dispatch_hold["hold_id"])
    task = kb.get_task(board, tid)
    assert task is not None and task.status == phase and task.block_recurrences == 2 and task.dispatch_hold is None
    assert (kb.claim_review_task if phase == "review" else kb.claim_task)(board, tid) is not None
