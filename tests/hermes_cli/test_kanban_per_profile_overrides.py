"""Per-profile concurrency cap OVERRIDES for the kanban dispatcher.

``kanban.max_in_progress_per_profile`` is a single integer shared by every
profile, which cannot express the shape a mixed fleet actually needs. A heavy
profile is one whose worker runs a build and a test suite in the same process
tree, costing roughly an order of magnitude more memory and CPU per worker than
a light one. A uniform cap low enough to protect the host throttles the cheap
profiles too, and one high enough not to protect it is no protection at all.

``kanban.max_in_progress_per_profile_overrides`` is the map that expresses it:

- a listed profile uses its override;
- unlisted profiles fall back to ``max_in_progress_per_profile``;
- with that uniform cap unset, ONLY the listed profiles are capped;
- ready and review workers share one ceiling per profile;
- invalid entries are ignored individually, with a warning, so one typo cannot
  void the limits for every other profile.

Ported onto the post-decomposition dispatcher: ``dispatch_once`` lives in
``hermes_cli.kanban_db_dispatch`` and ``_dispatch_lane_task`` is the single
shared helper behind both the ready and the review lane, so a ceiling is
applied in one place instead of two.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

# A squad worker exports the board it is running on, and ``kanban_db_path()``
# resolves HERMES_KANBAN_DB (as ``kanban_home()`` does HERMES_KANBAN_HOME)
# BEFORE the throwaway HERMES_HOME the fixture below sets. One inherited
# variable is enough to write these fixture cards to the LIVE board, where the
# dispatcher then spends real worker runs on them (53 leaked smoke fixtures,
# 2026-09-04..09-10).
_BOARD_ENV_VARS = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_HOME",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_TENANT",
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB, proven throwaway.

    The board path is asserted to sit under ``tmp_path`` before any test body
    runs: a silently re-pinned board would put these fixture cards in front of
    the live dispatcher.
    """
    for _var in _BOARD_ENV_VARS:
        monkeypatch.delenv(_var, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path().resolve()
    assert tmp_path.resolve() in db_path.parents, (
        "fixture board escaped tmp_path: %s -- an inherited board-pinning "
        "variable re-pinned the live board" % db_path
    )
    kb.init_db()
    return home


def _set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    """Test helper: move a card between lanes without the lifecycle events."""
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _fake_spawn(*_args, **_kwargs):
    return 42


def _seed(conn, assignee: str, count: int) -> list[str]:
    """``count`` ready cards for one profile; returns their ids."""
    return [
        kb.create_task(conn, title="%s-%d" % (assignee, i), assignee=assignee)
        for i in range(count)
    ]


def _assignees(result, field: str) -> list[str]:
    return [row[1] for row in getattr(result, field)]


# ---------------------------------------------------------------------------
# Precedence: override wins, unlisted falls back, overrides-only is scoped
# ---------------------------------------------------------------------------


def test_override_caps_only_listed_profile_when_global_unset(
    kanban_home, all_assignees_spawnable,
):
    """Global cap null + {alpha: 2}: alpha stops at 2, beta is unconstrained.

    This is the whole point of the map -- bounding the profiles that actually
    exhaust the host without throttling the ones that do not.
    """
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _seed(conn, "alpha", 5)
        _seed(conn, "beta", 5)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_per_profile=None,
            max_in_progress_per_profile_overrides={"alpha": 2},
        )

    assert _assignees(res, "spawned").count("alpha") == 2
    assert _assignees(res, "spawned").count("beta") == 5
    assert _assignees(res, "skipped_per_profile_capped").count("alpha") == 3
    assert "beta" not in _assignees(res, "skipped_per_profile_capped")


def test_override_takes_priority_over_global_cap(
    kanban_home, all_assignees_spawnable,
):
    """Uniform cap 4 but {alpha: 1}: alpha gets 1, beta keeps the fallback 4."""
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _seed(conn, "alpha", 5)
        _seed(conn, "beta", 5)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_per_profile=4,
            max_in_progress_per_profile_overrides={"alpha": 1},
        )

    assert _assignees(res, "spawned").count("alpha") == 1
    assert _assignees(res, "spawned").count("beta") == 4
    assert _assignees(res, "skipped_per_profile_capped").count("alpha") == 4
    assert _assignees(res, "skipped_per_profile_capped").count("beta") == 1


def test_unlisted_profiles_are_unconstrained_without_a_uniform_cap(
    kanban_home, all_assignees_spawnable,
):
    """An overrides-only config must not accidentally cap the other profiles.

    Guards the obvious over-correction of the gating fix below: the in-tick
    counter is enabled by *any* per-profile limit, and that must not be read as
    "every profile now has one".
    """
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _seed(conn, "designer", 4)
        _seed(conn, "dev", 4)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_per_profile_overrides={"designer": 1, "dev": 2},
        )

    spawned = _assignees(res, "spawned")
    capped = _assignees(res, "skipped_per_profile_capped")
    assert spawned.count("designer") == 1
    assert spawned.count("dev") == 2
    assert capped.count("designer") == 3
    assert capped.count("dev") == 2


# ---------------------------------------------------------------------------
# The in-tick counter must not be gated on the UNIFORM cap
# ---------------------------------------------------------------------------


def test_override_is_not_re_read_stale_within_one_tick(
    kanban_home, all_assignees_spawnable,
):
    """With only overrides set, one profile cannot spawn past its ceiling.

    The regular reading is "count the in-flight workers while at least one
    per-profile limit is active". Gating that counter on the *uniform* cap
    instead looks equivalent and is not: in an overrides-only configuration the
    uniform cap is ``None``, so the count is never incremented and every row in
    the same tick re-reads a stale zero. Five ready cards would then all spawn
    against an override of 1 -- a fan-out that the config explicitly forbade,
    inside a single tick, on the host this map exists to protect.
    """
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _seed(conn, "alpha", 5)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn,
            max_in_progress_per_profile_overrides={"alpha": 1},
        )

    assert _assignees(res, "spawned").count("alpha") == 1, (
        "an override of 1 let more than one worker through in a single tick: "
        "the in-tick counter is gated on the uniform cap"
    )
    assert len(res.skipped_per_profile_capped) == 4


def test_override_counts_pre_existing_running(
    kanban_home, all_assignees_spawnable,
):
    """A card already in ``running`` consumes the profile's allowance."""
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        running = kb.create_task(conn, title="already running", assignee="alpha")
        assert kb.claim_task(conn, running) is not None
        _seed(conn, "alpha", 3)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_per_profile_overrides={"alpha": 1},
        )

    assert not _assignees(res, "spawned")
    assert len(res.skipped_per_profile_capped) == 3


# ---------------------------------------------------------------------------
# One ceiling per profile, shared by both lanes
# ---------------------------------------------------------------------------


def test_ready_and_review_lanes_share_the_override(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Ready and review workers draw on the SAME per-profile allowance.

    Counting them separately would double every ceiling as soon as a card sat
    in review -- a review worker is a worker, and it holds the same local model
    / API quota / browser pool the ceiling exists to protect.
    """
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        running = kb.create_task(conn, title="running alpha", assignee="alpha")
        assert kb.claim_task(conn, running) is not None
        ready_alpha = kb.create_task(conn, title="ready alpha", assignee="alpha")
        review_alpha = kb.create_task(conn, title="review alpha", assignee="alpha")
        _set_task_status(conn, review_alpha, "review")
        beta_ids = _seed(conn, "beta", 4)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_per_profile=3,
            max_in_progress_per_profile_overrides={"alpha": 1},
        )

    spawned = {task_id for task_id, _, _ in res.spawned}
    capped = {task_id for task_id, _, _ in res.skipped_per_profile_capped}
    assert {ready_alpha, review_alpha} <= capped, (
        "a review worker must consume the same per-profile allowance as a "
        "ready one"
    )
    assert not {ready_alpha, review_alpha} & spawned
    assert len(spawned & set(beta_ids)) == 3


# ---------------------------------------------------------------------------
# Validation: one bad entry must not void the rest
# ---------------------------------------------------------------------------


def test_invalid_override_entries_are_ignored(
    kanban_home, all_assignees_spawnable,
):
    """Non-numeric and below-1 values are dropped; valid entries still apply."""
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _seed(conn, "alpha", 3)
        _seed(conn, "beta", 3)
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=True,
            max_in_progress_per_profile_overrides={
                "alpha": 2,
                "beta": "not-a-number",
                "gamma": 0,
            },
        )

    assert _assignees(res, "spawned").count("alpha") == 2
    # beta's invalid override is dropped, so it stays unconstrained.
    assert _assignees(res, "spawned").count("beta") == 3


def test_normalize_profile_cap_overrides_warns_and_keeps_valid_entries(caplog):
    """Gateway, CLI and the dispatcher share one parser with actionable logs."""
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        parsed = kbd.normalize_profile_cap_overrides(
            {
                "supervisor": 1,
                "implementer": "3",
                "designer": 2,
                "zero": 0,
                "bad": "many",
                "": 2,
                # Three shapes that must be dropped individually and never
                # fatally: `True` is an `int` subclass, so int() would read a
                # YAML `true` as a cap of 1; `inf` is what PyYAML makes of
                # `1e400`, and int(inf) raises OverflowError out of the tick;
                # and 2.5 would be truncated to 2 without a word.
                "flag": True,
                "inf": float("inf"),
                "frac": 2.5,
            }
        )

    assert parsed == {"supervisor": 1, "implementer": 3, "designer": 2}
    messages = [record.getMessage() for record in caplog.records]
    assert any("'zero'" in m and "below 1" in m for m in messages)
    assert any("'bad'" in m and "invalid" in m for m in messages)
    assert any("'flag'" in m and "a boolean" in m for m in messages)
    assert any("'inf'" in m and "whole number" in m for m in messages)
    assert any("'frac'" in m and "whole number" in m for m in messages)
    assert any("profile name" in m for m in messages)


@pytest.mark.parametrize("raw", [None, [], "designer=1", 3])
def test_non_mapping_overrides_disables_only_the_overrides(raw):
    """A malformed container means "no overrides", never "no per-profile cap"."""
    assert kbd.normalize_profile_cap_overrides(raw) == {}


# ---------------------------------------------------------------------------
# CLI passthrough: a manual tick must honour what the operator configured
# ---------------------------------------------------------------------------


def test_cli_dispatch_passes_overrides_from_config(
    kanban_home, monkeypatch,
):
    """``hermes kanban dispatch`` must forward the override map.

    Otherwise the cap silently applies from the gateway and not from the CLI,
    which is the surface an operator reaches for while debugging exactly this.
    """
    from hermes_cli import kanban as kb_cli

    fake_config = {
        "kanban": {
            "max_in_progress_per_profile": 3,
            "max_in_progress_per_profile_overrides": {"designer": 1, "dev": 2},
        }
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: fake_config)

    captured: dict = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False)
    kb_cli._cmd_dispatch(args)

    assert captured.get("max_in_progress_per_profile") == 3
    assert captured.get("max_in_progress_per_profile_overrides") == {
        "designer": 1,
        "dev": 2,
    }


def test_cli_dispatch_survives_unreadable_config(kanban_home, monkeypatch):
    """A config read failure must fall back to an empty map, not crash.

    ``None`` here would reach the parser as "no overrides" anyway, but an
    explicit empty map keeps the CLI's contract with ``dispatch_once`` a
    mapping at every call site.
    """
    from hermes_cli import kanban as kb_cli

    def boom():
        raise RuntimeError("config is unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config", boom)
    captured: dict = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False)
    assert kb_cli._cmd_dispatch(args) == 0
    assert captured.get("max_in_progress_per_profile_overrides") == {}


# ---------------------------------------------------------------------------
# The parser is reachable from a second interpreter state
# ---------------------------------------------------------------------------


def test_parser_is_a_module_level_symbol():
    """The gateway imports it late-bound through ``_kbd()``; keep it importable."""
    assert callable(kbd.normalize_profile_cap_overrides)
    assert "hermes_cli.kanban_db_dispatch" in sys.modules


def test_dispatcher_settings_overrides_default_to_empty_map():
    """Positional callers that predate the field must keep constructing it."""
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings

    first = _DispatcherSettings(60.0, None, 8, 3, 0, True, None, 2)
    second = _DispatcherSettings(60.0, None, 8, 3, 0, True, None, 2)
    assert first.max_in_progress_per_profile_overrides == {}
    # default_factory, not a shared mutable default.
    assert (
        first.max_in_progress_per_profile_overrides
        is not second.max_in_progress_per_profile_overrides
    )


# ---------------------------------------------------------------------------
# Every dispatch surface reads the same config: daemon and dashboard too
# ---------------------------------------------------------------------------


def _patch_readonly_config(monkeypatch, kanban_cfg):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda *a, **k: {"kanban": kanban_cfg},
    )


def test_configured_per_profile_caps_uses_the_shared_parser(monkeypatch, caplog):
    _patch_readonly_config(monkeypatch, {
        "max_in_progress_per_profile": "2",
        "max_in_progress_per_profile_overrides": {"alpha": 1, "beta": True},
    })
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        cap, overrides = kbd.configured_per_profile_caps()

    assert cap == 2
    assert overrides == {"alpha": 1}
    assert any("'beta'" in r.getMessage() and "a boolean" in r.getMessage()
               for r in caplog.records)


def test_configured_per_profile_caps_logs_an_unreadable_config(monkeypatch, caplog):
    def boom(*_a, **_k):
        raise RuntimeError("config is unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        assert kbd.configured_per_profile_caps() == (None, {})
    assert any("config is unreadable" in r.getMessage() for r in caplog.records)


def test_run_daemon_honours_overrides(kanban_home, all_assignees_spawnable, monkeypatch):
    """``hermes kanban daemon``: 8 ready alpha cards under {alpha: 1} -> 1 spawn."""
    import threading

    _patch_readonly_config(monkeypatch, {
        "max_in_progress_per_profile_overrides": {"alpha": 1},
    })
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: None)
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _seed(conn, "alpha", 8)

    real_dispatch_once = kbd.dispatch_once
    captured: dict = {}

    def dispatch_with_fake_spawn(conn, **kwargs):
        captured.update(kwargs)
        return real_dispatch_once(conn, spawn_fn=_fake_spawn, **kwargs)

    monkeypatch.setattr(kbd, "dispatch_once", dispatch_with_fake_spawn)
    stop = threading.Event()
    results: list = []

    def on_tick(res):
        results.append(res)
        stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert captured["max_in_progress_per_profile_overrides"] == {"alpha": 1}
    assert len(results) == 1
    assert _assignees(results[0], "spawned") == ["alpha"]
    assert _assignees(results[0], "skipped_per_profile_capped").count("alpha") == 7


def test_run_daemon_honours_the_uniform_per_profile_cap(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    import threading

    _patch_readonly_config(monkeypatch, {"max_in_progress_per_profile": 2})
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: None)
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _seed(conn, "alpha", 5)

    real_dispatch_once = kbd.dispatch_once
    monkeypatch.setattr(
        kbd, "dispatch_once",
        lambda conn, **kw: real_dispatch_once(conn, spawn_fn=_fake_spawn, **kw),
    )
    stop = threading.Event()
    results: list = []

    def on_tick(res):
        results.append(res)
        stop.set()

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=on_tick)

    assert _assignees(results[0], "spawned") == ["alpha", "alpha"]


def _dashboard_client():
    import importlib.util

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    plugin_file = (
        Path(__file__).resolve().parents[2]
        / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_overrides_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def test_dashboard_dispatch_honours_overrides(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Dashboard ``POST /dispatch``: 8 ready alpha cards under {alpha: 1} -> 1."""
    _patch_readonly_config(monkeypatch, {
        "max_in_progress_per_profile": 4,
        "max_in_progress_per_profile_overrides": {"alpha": 1},
    })
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        _seed(conn, "alpha", 8)
        _seed(conn, "beta", 6)

    r = _dashboard_client().post("/api/plugins/kanban/dispatch?dry_run=true&max=20")

    assert r.status_code == 200, r.text
    body = r.json()
    spawned = [row[1] for row in body["spawned"]]
    capped = [row[1] for row in body["skipped_per_profile_capped"]]
    assert spawned.count("alpha") == 1
    assert capped.count("alpha") == 7
    # beta is unlisted, so it falls back to the uniform cap of 4.
    assert spawned.count("beta") == 4
    assert capped.count("beta") == 2


# ---------------------------------------------------------------------------
# The review-lane reservation resolves the cap through the overrides map
# ---------------------------------------------------------------------------


def _review_case(conn, uniform, override):
    """``reviewer`` already runs one worker; one ready card and one review card."""
    busy = kb.create_task(conn, title="busy", assignee="reviewer")
    assert kb.claim_task(conn, busy) is not None
    ready_id = kb.create_task(conn, title="ready-now", assignee="alice")
    review_id = kb.create_task(conn, title="review-me", assignee="reviewer")
    _set_task_status(conn, review_id, "review")
    res = kbd.dispatch_once(
        conn, spawn_fn=_fake_spawn, max_in_progress=2,
        max_in_progress_per_profile=uniform,
        max_in_progress_per_profile_overrides={"reviewer": override},
    )
    return ready_id, review_id, [task_id for task_id, *_ in res.spawned]


def test_review_at_its_override_does_not_reserve_the_ready_slot(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Uniform cap unset, {reviewer: 1}: the review card is capped this tick.

    Read through the uniform cap alone it looks spawnable, holds back the one
    free slot, and the ready card starves while the reserved slot goes unused.
    """
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    with kbc.connect_closing() as conn:
        ready_id, _review_id, spawned = _review_case(conn, uniform=None, override=1)

    assert spawned == [ready_id]


def test_review_below_its_override_still_reserves_the_slot(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Uniform cap 1 but {reviewer: 2}: the review card is spawnable and keeps
    its reserved slot, though the uniform cap alone would call it capped."""
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    with kbc.connect_closing() as conn:
        _ready_id, review_id, spawned = _review_case(conn, uniform=1, override=2)

    assert spawned == [review_id]
