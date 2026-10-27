"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_running_with_open_parents_fires_only_while_running():
    """A running card whose parent is not terminal is flagged; the same graph
    on a ready/todo card (the gate is holding it) and a done parent are not."""
    graph = {"parents": [{"id": "t_parent", "title": "p", "status": "todo"}], "children": []}
    diags = kd.compute_task_diagnostics(_task(status="running", started_at=100), [], [], graph=graph)
    assert [d.kind for d in diags] == ["running_with_open_parents"]
    assert diags[0].data["open_parents"] == [{"id": "t_parent", "status": "todo"}]
    assert "hermes kanban unlink t_parent t_demo00" in diags[0].actions[0].payload["command"]
    assert kd.compute_task_diagnostics(_task(status="todo"), [], [], graph=graph) == []
    done_graph = {"parents": [{"id": "t_parent", "title": "p", "status": "done"}], "children": []}
    assert kd.compute_task_diagnostics(_task(status="running"), [], [], graph=done_graph) == []


def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48








# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True


# ---------------------------------------------------------------------------
# repeated_crashes — a streak that is over is reported as historical
# ---------------------------------------------------------------------------

# The board's real shape (t_2d283056 / t_6db2248b / t_8afe5089, 2026-09-14):
# five crashed runs days old, and a NEWER run that ended `unreported` (the
# deliberate "exited cleanly without a terminal kanban call" outcome) or
# `blocked`. The page said "The last 5 runs ended with outcome=crashed" about
# those cards in the present tense and paged them `critical`.
_PROTOCOL_ERR = (
    "worker exited cleanly (rc=0) without calling kanban_complete or "
    "kanban_block — protocol violation."
)
_LAST_CRASH_END = 1789234174        # 2026-09-12 17:29Z
_NEWEST_END = 1789346851            # 2026-09-14 00:47Z


def _run_at(outcome, run_id, started_at, ended_at, error=None, worker_pid=None,
            claim_lock="host:1234"):
    """A ``task_runs`` row. Claimed by default (every run a dispatcher opens
    carries ``claim_lock``); pass ``claim_lock=None`` for the zero-duration
    row ``_synthesize_ended_run`` writes on a never-claimed card."""
    return {
        "id": run_id, "outcome": outcome, "error": error,
        "started_at": started_at, "ended_at": ended_at, "worker_pid": worker_pid,
        "claim_lock": claim_lock,
    }


def _crash_rule_diag(task, runs):
    """The one repeated_crashes diagnostic, or a readable failure."""
    found = [d for d in kd.compute_task_diagnostics(task, [], runs)
             if d.kind == "repeated_crashes"]
    assert len(found) == 1, f"expected one repeated_crashes finding, got {found}"
    return found[0]


def _five_old_crashes():
    """Five consecutive crashes, newest first, all on 2026-09-12."""
    return [
        _run_at("crashed", 5, 1789233932, _LAST_CRASH_END, error=_PROTOCOL_ERR),
        _run_at("crashed", 4, 1789233691, 1789233932, error=_PROTOCOL_ERR),
        _run_at("crashed", 3, 1789233508, 1789233691, error=_PROTOCOL_ERR),
        _run_at("crashed", 2, 1789233327, 1789233508, error=_PROTOCOL_ERR),
        _run_at("crashed", 1, 1789233145, 1789233327, error=_PROTOCOL_ERR),
    ]


def test_repeated_crashes_finished_streak_is_historical_not_current():
    """A streak the card has since left must not be asserted in the present
    tense, and must not page at the critical floor the board-health page uses.
    """
    task = _task(status="ready")
    runs = [_run_at("unreported", 6, 1789346236, _NEWEST_END, error=_PROTOCOL_ERR)]
    runs += _five_old_crashes()

    d = _crash_rule_diag(task, runs)

    # Pre-fix this was "critical" with a present-tense claim.
    assert d.severity == "warning"
    assert "historical" in d.title
    assert "2026-09-12 17:29Z" in d.title
    assert "2026-09-12 17:29Z" in d.detail
    assert "not a current crash" in d.detail
    assert "2026-09-14 00:47Z" in d.detail and "outcome=unreported" in d.detail
    assert "crashed 5x" in d.title
    # The basis is inspectable: which runs were read, in which order, and what
    # the newest one ended as.
    assert d.data["is_current"] is False
    assert d.data["newest_outcome"] == "unreported"
    assert d.data["newest_run_id"] == 6
    assert d.data["last_crash_run_id"] == 5
    assert d.data["last_crash_at"] == _LAST_CRASH_END
    assert [r["id"] for r in d.data["runs_read"]] == [6, 5, 4, 3, 2, 1]
    assert [r["outcome"] for r in d.data["runs_read"]] == [
        "unreported", "crashed", "crashed", "crashed", "crashed", "crashed",
    ]


def _crash_findings(task, runs):
    return [d for d in kd.compute_task_diagnostics(task, [], runs)
            if d.kind == "repeated_crashes"]


@pytest.mark.parametrize("outcome", ["blocked", "review_requested"])
def test_repeated_crashes_synthesized_terminal_run_keeps_historical_warning(outcome):
    """An operator blocking (or handing to review) a `ready` card makes
    `_end_or_synthesize_run` write a zero-duration run with no claim. No worker
    ran, so the crash streak stays a dated historical warning."""
    task = _task(status="ready")
    runs = [_run_at(outcome, 6, 1789346472, 1789346472, claim_lock=None)]
    runs += _five_old_crashes()

    d = _crash_rule_diag(task, runs)

    assert d.severity == "warning"
    assert d.data["is_current"] is False
    assert d.data["newest_outcome"] == outcome
    assert f"outcome={outcome}" in d.detail
    assert "not a current crash" in d.detail


@pytest.mark.parametrize("outcome", ["blocked", "review_requested"])
@pytest.mark.parametrize("worker_pid", [4242, None])
@pytest.mark.parametrize("started_at", [1789346111, 1789346472])
def test_repeated_crashes_worker_block_or_review_request_breaks_streak(
    outcome, worker_pid, started_at,
):
    """A claimed run that ended `blocked` / `review_requested` came from the
    worker's own `kanban_block` / `kanban_request_review`: the agent booted and
    ended properly, so the streak ends like it does on `completed`. The
    reaper clears `worker_pid` once that worker exits, and a fast worker can
    end in the second it was claimed, so the claim alone must be enough."""
    task = _task(status="ready")
    runs = [_run_at(outcome, 6, started_at, 1789346472, worker_pid=worker_pid)]
    runs += _five_old_crashes()

    assert _crash_findings(task, runs) == []


def test_repeated_crashes_claim_marker_matches_real_block_paths(kanban_home):
    """The marker the rule reads, on rows the kanban DB itself writes: a
    claimed run closed by `block_task` keeps `claim_lock`; an operator block of
    a `ready` card synthesizes a run without one."""
    conn = kbc.connect()
    try:
        worked = kb.create_task(conn, title="worked", assignee="w")
        assert kb.claim_task(conn, worked) is not None
        assert kb.block_task(conn, worked, reason="needs input")
        operator = kb.create_task(conn, title="operator", assignee="w")
        assert kb.block_task(conn, operator, reason="parked by operator")
        worker_runs = kb.list_runs(conn, worked)
        synth_runs = kb.list_runs(conn, operator)
    finally:
        conn.close()

    assert [(r.outcome, bool(r.claim_lock)) for r in worker_runs] == [("blocked", True)]
    assert [(r.outcome, bool(r.claim_lock)) for r in synth_runs] == [("blocked", False)]

    # Newest by id, ahead of the five fixture crashes (ids 1..5).
    worker_run = dataclasses.replace(worker_runs[0], id=6)
    synth_run = dataclasses.replace(synth_runs[0], id=6)
    task = _task(status="blocked")
    assert _crash_findings(task, [worker_run] + _five_old_crashes()) == []
    d = _crash_rule_diag(task, [synth_run] + _five_old_crashes())
    assert d.severity == "warning" and d.data["is_current"] is False


def test_repeated_crashes_worker_block_two_back_still_breaks_streak():
    """The fleet's shape: crashed, crashed, then two real `kanban_block` runs
    11 days apart. No crash finding, historical or current."""
    task = _task(status="blocked")
    runs = [
        _run_at("blocked", 4, 1790300000, 1790300400),
        _run_at("blocked", 3, 1789346111, 1789346472),
        _run_at("crashed", 2, 1789233508, _LAST_CRASH_END, error=_PROTOCOL_ERR),
        _run_at("crashed", 1, 1789233327, 1789233508, error="pid 364463 not alive"),
    ]

    assert _crash_findings(task, runs) == []


@pytest.mark.parametrize("outcome", ["timed_out", "spawn_failed"])
def test_repeated_crashes_non_worker_outcome_keeps_historical_warning(outcome):
    """`timed_out` / `spawn_failed` are not a worker's own terminal call, so
    they do not break the streak even on a claimed run."""
    task = _task(status="ready")
    runs = [_run_at(outcome, 6, 1789346111, _NEWEST_END)]
    runs += _five_old_crashes()

    d = _crash_rule_diag(task, runs)

    assert d.severity == "warning"
    assert d.data["is_current"] is False
    assert d.data["newest_outcome"] == outcome
    assert "not a current crash" in d.detail


def test_repeated_crashes_finished_streak_behind_a_live_run_is_historical():
    """A newest run with no outcome yet (in flight while the card is not
    `running`) is not a crash: the streak reads as historical, not current."""
    task = _task(status="blocked")
    runs = [_run_at(None, 6, 1789346800, None, error=None)]
    runs += _five_old_crashes()

    d = _crash_rule_diag(task, runs)

    assert d.severity == "warning"
    assert d.data["is_current"] is False
    assert "is still in flight (no outcome yet)" in d.detail


def test_repeated_crashes_current_streak_pages_exactly_as_before():
    """The other direction: when the newest run IS a crash the finding pages
    exactly as before — same title, same detail, same critical severity. Those
    three assertions pass pre-fix on purpose (they are the no-regression
    contract); the basis keys asserted below them are the new ones.
    """
    task = _task(status="ready")
    runs = _five_old_crashes()

    d = _crash_rule_diag(task, runs)

    assert d.severity == "critical"
    assert d.title == f"Agent crashed 5x: {_PROTOCOL_ERR}"
    assert d.detail == (
        f"The last 5 runs ended with outcome=crashed. Full last error:\n\n{_PROTOCOL_ERR}"
    )
    assert d.data["is_current"] is True
    assert d.data["newest_outcome"] == "crashed"


def test_repeated_crashes_pid_not_alive_mode_still_pages():
    """The `pid <n> not alive` crash mode is a live incident and keeps paging
    critical exactly as before — the fix must not mute it."""
    task = _task(status="ready")
    runs = [
        _run_at("crashed", 4, 1789233508, _LAST_CRASH_END, error="pid 364463 not alive"),
        _run_at("crashed", 3, 1789233327, 1789233508, error=_PROTOCOL_ERR),
        _run_at("crashed", 2, 1789233145, 1789233327, error=_PROTOCOL_ERR),
        _run_at("crashed", 1, 1789233000, 1789233145, error=_PROTOCOL_ERR),
    ]

    d = _crash_rule_diag(task, runs)

    assert d.severity == "critical"
    assert d.title == "Agent crashed 4x: pid 364463 not alive"
    assert d.data["is_current"] is True


def test_repeated_crashes_threshold_and_vocabulary_are_untouched():
    """The fix distinguishes; it does not suppress. One crash below the default
    threshold still fires nothing, and `unreported` is reported by name rather
    than dropped from the vocabulary."""
    task = _task(status="ready")
    below = [d for d in kd.compute_task_diagnostics(
        task, [], [_run_at("crashed", 1, 1789233145, _LAST_CRASH_END, error=_PROTOCOL_ERR)],
    ) if d.kind == "repeated_crashes"]
    assert below == []

    runs = [_run_at("unreported", 6, 1789346236, _NEWEST_END, error=_PROTOCOL_ERR)]
    runs += _five_old_crashes()
    d = _crash_rule_diag(task, runs)
    assert "unreported" in d.detail
    assert d.count == 5
