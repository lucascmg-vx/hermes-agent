"""Respawn-guard ``active_pr`` re-queue bypass tests.

Mirrors the ``recent_success`` exception: an explicit re-queue after a PR-URL
comment is a deliberate re-run and must not be held by the 24h PR window.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

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


PR_COMMENT = "Opened https://github.com/example/repo/pull/456 — follow-up needed."


def test_active_pr_guard_defers_ready_lane_without_requeue(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already PRed", assignee="worker")
        kb.add_comment(conn, tid, author="worker", body=PR_COMMENT)
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_active_pr_guard_honors_manual_promote_after_pr_comment(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="open parent", assignee="worker", triage=True)
        tid = kb.create_task(conn, title="PR then manual promote", assignee="worker")
        kb.add_comment(conn, tid, author="worker", body=PR_COMMENT)
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"

        kb.link_tasks(conn, parent_id=parent, child_id=tid)
        kb.block_task(conn, tid, reason="waiting on parent", kind="dependency")
        assert kb.get_task(conn, tid).status == "todo"
        conn.execute("DELETE FROM task_links WHERE parent_id = ? AND child_id = ?", (parent, tid))
        ok, _err = kb.promote_task(conn, tid, actor="dispatcher")
        assert ok is True

        assert kbd.check_respawn_guard(conn, tid) is None


def test_active_pr_guard_honors_unblock_after_pr_comment(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="PR then human unblock", assignee="worker")
        kb.add_comment(conn, tid, author="worker", body=PR_COMMENT)
        kb.block_task(conn, tid, reason="needs merge", kind="needs_input")
        assert kb.get_task(conn, tid).status == "blocked"

        kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"
        assert kbd.check_respawn_guard(conn, tid) is None


def test_active_pr_guard_honors_dependency_auto_promote_after_pr_comment(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="dependency", assignee="worker")
        tid = kb.create_task(conn, title="PR then waits on dependency", assignee="worker", parents=[parent])
        assert kb.get_task(conn, tid).status == "todo"
        kb.add_comment(conn, tid, author="worker", body=PR_COMMENT)
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"

        assert kb.complete_task(conn, parent, result="done") is True
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "ready"
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,))]
        assert "promoted" in kinds and "promoted_manual" not in kinds
        assert kbd.check_respawn_guard(conn, tid) is None


def _claim_on_this_host(conn, tid: str) -> None:
    host = kb._claimer_id().split(":", 1)[0]
    assert kb.claim_task(conn, tid, claimer=f"{host}:worker") is not None


def test_active_pr_guard_holds_after_automatic_stale_claim_reclaim(kanban_home: Path) -> None:
    """A worker that posted its PR URL and then died is reclaimed automatically;
    that must not re-spawn the same assignee against its own PR."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="PR then crash", assignee="worker")
        _claim_on_this_host(conn, tid)
        kb.add_comment(conn, tid, author="worker", body=PR_COMMENT)
        conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 3600, tid))

        assert kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None) == 1
        assert kb.get_task(conn, tid).status == "ready"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'reclaimed'", (tid,),
        ).fetchone()[0] == 1
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_active_pr_guard_honors_manual_reclaim_after_pr_comment(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="PR then operator reclaim", assignee="worker")
        _claim_on_this_host(conn, tid)
        kb.add_comment(conn, tid, author="worker", body=PR_COMMENT)

        assert kb.reclaim_task(conn, tid, reason="re-run", signal_fn=lambda _p, _s: None) is True
        assert kb.get_task(conn, tid).status == "ready"
        assert kbd.check_respawn_guard(conn, tid) is None
