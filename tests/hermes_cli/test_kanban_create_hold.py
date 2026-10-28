"""Create-time hold (``kanban_create(hold=True)``) and ``kanban_unhold``.

The race this closes: a filer creates a card and only then attaches the parents
it has to wait on. Between those two calls the card is ``ready``, so one
dispatcher tick is enough to claim it and start a worker on work whose gate did
not exist yet. ``hold=True`` writes the card in a non-claimable status in the
SAME statement as its INSERT -- no window, not one tick -- and the hold is
cleared from the graph: ``todo`` while a parent is open, ``ready`` once every
parent is done.

Covered here:
* the hold is written with the INSERT and is never claimable (claim_task, the
  dispatcher's ready lane, and recompute_ready all leave it alone);
* the clear recomputes from the parents and never promotes past an unmet one;
* only the creator clears it (``created_by`` AND ``session_id``), with the
  operator path as the escape hatch for a hold that outlived its filer;
* an unheld create keeps today's behaviour exactly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _hold(conn, **kw):
    kw.setdefault("title", "held")
    kw.setdefault("assignee", "worker")
    return kb.create_task(conn, hold=True, **kw)


def _finish(conn, tid: str) -> None:
    """Terminal without a run: enough for the parent gate, which reads status."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))


def _events(conn, tid: str, kind: str):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


# ---------------------------------------------------------------------------
# The hold exists from the INSERT
# ---------------------------------------------------------------------------

def test_hold_is_written_with_the_insert(kanban_home):
    """A card with no parents is normally ``ready``; held, it is ``hold``."""
    with kbc.connect_closing() as conn:
        tid = _hold(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "hold"
        created = _events(conn, tid, "created")
        assert created and created[-1].payload["status"] == "hold"
        # One event records the hold, so the timeline explains the park.
        held = _events(conn, tid, "hold")
        assert len(held) == 1
        assert held[0].payload["reason"] == "create_hold"
        assert [t.id for t in kb.list_tasks(conn, status="hold")] == [tid]


def test_a_held_card_is_never_claimable(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _hold(conn)
        assert kb.claim_task(conn, tid, claimer="worker") is None
        assert kb.get_task(conn, tid).status == "hold"
        assert kb.list_runs(conn, tid) == []
        rejected = _events(conn, tid, "claim_rejected")
        assert [e.payload["reason"] for e in rejected] == ["held"]


def test_the_dispatcher_never_claims_a_held_card(kanban_home, monkeypatch):
    """Three real ticks: the ready sibling is taken every time, the hold never."""
    # The tick drops an assignee no profile answers for, so name a real one the
    # way the host's profile registry would (and stub the lookup, not the board).
    monkeypatch.setattr(
        kbd, "_profile_exists_fn", lambda: (lambda name: str(name).startswith("vx-"))
    )
    with kbc.connect_closing() as conn:
        held = _hold(conn, assignee="vx-qa")
        ready = kb.create_task(conn, title="dispatchable", assignee="vx-qa")
        seen: list[str] = []
        for _ in range(3):
            result = kbd.dispatch_once(conn, dry_run=True)
            seen.extend(tid for tid, _assignee, _ws in result.spawned)
        assert ready in seen
        assert held not in seen
        assert kb.get_task(conn, held).status == "hold"


def test_recompute_ready_leaves_a_hold_alone(kanban_home):
    """Both shapes: no parents at all, and a parent that later finishes."""
    with kbc.connect_closing() as conn:
        lone = _hold(conn, title="no parents")
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _hold(conn, title="gated", parents=(parent,))
        assert kb.get_task(conn, child).status == "hold"

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, lone).status == "hold"

        _finish(conn, parent)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "hold", (
            "a finished parent must not clear a hold -- only unhold does"
        )


# ---------------------------------------------------------------------------
# Clearing recomputes from the graph
# ---------------------------------------------------------------------------

def test_clear_recomputes_from_the_parents(kanban_home):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _hold(conn, title="child", parents=(parent,),
                      created_by="filer", session_id="s1")

        ok, status = kb.unhold_task(conn, child, requester="filer", requester_session="s1")
        assert ok and status == "todo", "an open parent keeps the card in todo"
        assert kb.get_task(conn, child).status == "todo"
        assert _events(conn, child, "unheld")[-1].payload["status"] == "todo"
        assert kb.claim_task(conn, child, claimer="worker") is None

        _finish(conn, parent)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_clear_lands_ready_when_every_parent_is_done(kanban_home):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        _finish(conn, parent)
        child = _hold(conn, title="child", parents=(parent,))
        assert kb.hold_clear_status(conn, child) == "ready"
        ok, status = kb.unhold_task(conn, child, operator=True)
        assert ok and status == "ready"
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# Only the creator clears it
# ---------------------------------------------------------------------------

def test_only_the_creating_session_clears_the_hold(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _hold(conn, created_by="filer", session_id="session-a")

        assert kb.unhold_task(conn, tid, requester="someone-else",
                              requester_session="session-a")[0] is False
        assert kb.unhold_task(conn, tid, requester="filer",
                              requester_session="session-b")[0] is False
        assert kb.get_task(conn, tid).status == "hold"

        assert kb.unhold_task(conn, tid, requester="filer",
                              requester_session="session-a") == (True, "ready")


def test_a_hold_without_a_session_keys_on_the_profile(kanban_home):
    """No session on the row means no second key: the profile is the whole check."""
    with kbc.connect_closing() as conn:
        tid = _hold(conn, created_by="filer")
        assert kb.unhold_task(conn, tid, requester="someone-else")[0] is False
        assert kb.get_task(conn, tid).status == "hold"
        assert kb.unhold_task(conn, tid, requester="filer") == (True, "ready")


def test_unhold_refuses_a_card_that_is_not_held(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="plain", assignee="worker")
        ok, detail = kb.unhold_task(conn, tid, operator=True)
        assert ok is False and detail.startswith("not held")
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.unhold_task(conn, "t_missing", operator=True) == (False, "not found")


# ---------------------------------------------------------------------------
# An unheld create is unchanged
# ---------------------------------------------------------------------------

def test_an_unheld_create_keeps_todays_behaviour(kanban_home):
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, kb.create_task(
            conn, title="plain", assignee="worker")).status == "ready"
        parent = kb.create_task(conn, title="parent", assignee="worker")
        assert kb.get_task(conn, kb.create_task(
            conn, title="gated", assignee="worker", parents=(parent,))).status == "todo"
        assert kb.get_task(conn, kb.create_task(
            conn, title="blocked", assignee="worker",
            initial_status="blocked")).status == "blocked"
        assert kb.get_task(conn, kb.create_task(
            conn, title="triage", assignee="worker", triage=True)).status == "triage"


def test_held_retry_returns_the_card_already_held_under_its_key(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _hold(conn, idempotency_key="k-held")
        assert _hold(conn, idempotency_key="k-held") == tid
        assert kb.get_task(conn, tid).status == "hold"
        assert len(kb.list_tasks(conn, status="hold")) == 1


@pytest.mark.parametrize("status", ["ready", "todo", "running"])
def test_held_retry_refuses_a_claimable_card_under_its_key(kanban_home, status):
    """Handing back a non-held card would reopen the create-vs-link race."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="plain", assignee="worker",
                             idempotency_key="k-plain")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        with pytest.raises(ValueError, match=f"{tid} in status '{status}', not 'hold'"):
            _hold(conn, idempotency_key="k-plain")
        assert kb.get_task(conn, tid).status == status
        assert kb.list_tasks(conn, status="hold") == []
        # Without hold the key keeps its plain idempotent meaning.
        assert kb.create_task(conn, title="plain", assignee="worker",
                              idempotency_key="k-plain") == tid


def test_hold_refuses_the_other_parks(kanban_home):
    with kbc.connect_closing() as conn:
        for kw in ({"initial_status": "blocked"}, {"triage": True}):
            with pytest.raises(ValueError, match="hold=True conflicts"):
                kb.create_task(conn, title="contradiction", assignee="worker",
                               hold=True, **kw)


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------

def test_tool_creates_a_held_card_and_unholds_it(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "filer-profile")
    monkeypatch.setenv("HERMES_SESSION_ID", "")
    from tools import kanban_tools as kt

    created = json.loads(kt._handle_create({"title": "chain head", "assignee": "worker",
                                            "hold": True}))
    assert created["ok"] is True
    tid = created["task_id"]
    assert created["status"] == "hold"

    cleared = json.loads(kt._handle_unhold({"task_id": tid}))
    assert cleared["ok"] is True and cleared["status"] == "ready"

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        # Second clear is a refusal, not a silent no-op.
        assert "cannot unhold" in kt._handle_unhold({"task_id": tid})


def test_tool_unhold_refuses_another_profiles_hold(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", "")
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_PROFILE", "profile-a")
    tid = json.loads(kt._handle_create({"title": "a's card", "assignee": "worker",
                                        "hold": True}))["task_id"]
    monkeypatch.setenv("HERMES_PROFILE", "profile-b")
    refused = kt._handle_unhold({"task_id": tid})
    assert "cannot unhold" in refused
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "hold"


def test_filer_sequence_create_held_then_link_then_unhold(kanban_home):
    """The race narrative end to end: the parent is attached only after the
    create, and the clear still lands behind it."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _hold(conn, title="child", created_by="filer")
        assert kb.claim_task(conn, child, claimer="worker") is None
        kb.link_tasks(conn, parent, child)
        assert kb.get_task(conn, child).status == "hold"
        assert kb.unhold_task(conn, child, requester="filer") == (True, "todo")
        assert kb.claim_task(conn, child, claimer="worker") is None


def test_tool_rejects_a_held_retry_whose_key_names_a_claimable_card(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "filer-profile")
    monkeypatch.setenv("HERMES_SESSION_ID", "")
    from tools import kanban_tools as kt

    base = {"title": "chain head", "assignee": "worker", "idempotency_key": "k-tool"}
    first = json.loads(kt._handle_create(dict(base)))
    assert first["ok"] is True and first["status"] == "ready"

    retry = json.loads(kt._handle_create({**base, "hold": True}))
    assert retry.get("ok") is not True
    assert "not 'hold'" in retry["error"]
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, first["task_id"]).status == "ready"
        assert kb.list_tasks(conn, status="hold") == []


def test_cli_hold_flag_and_unhold(kanban_home):
    parser = argparse.ArgumentParser()
    kanban_cli.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["kanban", "create", "chain head", "--assignee", "worker", "--hold"])
    assert kanban_cli._cmd_create(args) == 0

    with kbc.connect_closing() as conn:
        rows = kb.list_tasks(conn, status="hold")
    assert len(rows) == 1
    tid = rows[0].id

    unhold = parser.parse_args(["kanban", "unhold", tid])
    assert kanban_cli._cmd_unhold(unhold) == 0

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"


def test_hold_is_a_real_status_every_surface_can_see(kanban_home):
    """Not a private flag: the dashboard column list and the CLI share one set."""
    from plugins.kanban.dashboard import plugin_api

    assert "hold" in kb.VALID_STATUSES
    assert "hold" not in kb.VALID_INITIAL_STATUSES, (
        "hold is the documented spelling; initial_status stays running/blocked"
    )
    # The dashboard's own contract: every persisted status is a column.
    assert kb.VALID_STATUSES - {"archived"} <= set(plugin_api.BOARD_COLUMNS)
    with kbc.connect_closing() as conn:
        tid = _hold(conn)
        assert kb.get_task(conn, tid) is not None
        assert [t.id for t in kb.list_tasks(conn, status="hold")] == [tid]


@pytest.fixture
def dashboard(kanban_home):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard import plugin_api

    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/k")
    return TestClient(app)


def test_dashboard_drag_releases_a_hold_only_through_unhold(dashboard):
    """A drag to ``ready`` is an operator release: one ``unheld`` event, no raw
    ``status`` write, and the card is claimable only after that release."""
    with kbc.connect_closing() as conn:
        tid = _hold(conn, created_by="filer", session_id="s1")

    r = dashboard.patch(f"/k/tasks/{tid}", json={"status": "ready"})
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "ready"
    with kbc.connect_closing() as conn:
        unheld = _events(conn, tid, "unheld")
        assert [e.payload for e in unheld] == [{"status": "ready", "actor": "dashboard"}]
        assert _events(conn, tid, "status") == [], "the hold must not leave by direct write"
        assert kb.claim_task(conn, tid, claimer="worker") is not None


def test_dashboard_drag_never_promotes_a_hold_past_an_open_parent(dashboard):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")

    for target in ("ready", "todo"):
        with kbc.connect_closing() as conn:
            tid = _hold(conn, title=f"child-{target}", parents=(parent,))
        r = dashboard.patch(f"/k/tasks/{tid}", json={"status": target})
        assert r.status_code == 200, r.text
        with kbc.connect_closing() as conn:
            assert kb.get_task(conn, tid).status == "todo", target
            assert _events(conn, tid, "unheld")[-1].payload == {"status": "todo", "actor": "dashboard"}
            assert kb.claim_task(conn, tid, claimer="worker") is None


@pytest.mark.parametrize("target", ["triage", "blocked", "scheduled", "review", "done"])
def test_dashboard_refuses_every_other_exit_from_hold(dashboard, target):
    with kbc.connect_closing() as conn:
        tid = _hold(conn)

    body = {"status": target, **({"summary": "s"} if target in ("done", "review") else {})}
    r = dashboard.patch(f"/k/tasks/{tid}", json=body)
    assert r.status_code == 409, r.text
    assert "it is held" in r.json()["detail"]
    bulk = dashboard.post("/k/tasks/bulk", json={"ids": [tid], **body}).json()["results"][0]
    assert bulk["ok"] is False and "it is held" in bulk["error"]
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "hold"
        assert _events(conn, tid, "unheld") == []


def test_direct_status_write_refuses_a_hold(kanban_home):
    """The raw writer behind drag stays closed to a hold even if called directly."""
    from plugins.kanban.dashboard import plugin_api

    with kbc.connect_closing() as conn:
        tid = _hold(conn)
        for target in ("ready", "todo", "triage"):
            assert plugin_api._set_status_direct(conn, tid, target) is False
            assert kb.get_task(conn, tid).status == "hold"
