"""A new kanban task branch starts from the remote default, not the anchor's HEAD.

The anchor clone is shared: other workers and people check out other branches in
it. A task branch cut from whatever HEAD happens to be there carries those commits
into an unrelated PR (#147233, item 3).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli.kanban_db_workspace import (
    _ensure_git_worktree,
    _worktree_start_point,
)

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=_ENV, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _init(cwd: Path) -> None:
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=str(cwd),
                   env=_ENV, check=True, capture_output=True)


def _commit(work: Path, text: str, message: str) -> str:
    (work / "README.md").write_text(text)
    _git(work, "add", "README.md")
    _git(work, "commit", "-qm", message)
    return _git(work, "rev-parse", "HEAD")


def _anchor(root: Path, name: str = "app") -> Path:
    """An anchor clone with a bare ``origin`` and one pushed commit on ``main``."""
    root.mkdir(parents=True, exist_ok=True)
    bare = root / f"{name}.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], env=_ENV,
                   check=True, capture_output=True)
    anchor = root / name
    anchor.mkdir()
    _init(anchor)
    _commit(anchor, "x\n", "c1")
    _git(anchor, "remote", "add", "origin", str(bare))
    _git(anchor, "push", "-q", "-u", "origin", "main")
    return anchor


def _cut(anchor: Path, tmp_path: Path, branch: str = "app/t_new") -> Path:
    target = tmp_path / ".worktrees" / branch.rsplit("/", 1)[-1]
    _ensure_git_worktree(anchor, target, branch)
    return target


def test_cut_ignores_a_parked_anchor_head(tmp_path):
    """Another worker left the anchor on its own branch; the cut must not follow."""
    anchor = _anchor(tmp_path)
    main_tip = _commit(anchor, "c2\n", "c2")
    _git(anchor, "push", "-q", "origin", "main")
    _git(anchor, "checkout", "-q", "-b", "someone-elses-pr", "HEAD~1")
    _commit(anchor, "theirs\n", "unrelated work")

    worktree = _cut(anchor, tmp_path)

    assert _git(worktree, "rev-parse", "HEAD") == main_tip
    assert _git(anchor, "symbolic-ref", "--short", "HEAD") == "someone-elses-pr"


def test_parked_branch_with_upstream_is_still_ignored(tmp_path):
    """A parked branch that tracks its own remote branch must not become the base."""
    anchor = _anchor(tmp_path)
    main_tip = _git(anchor, "rev-parse", "HEAD")
    _git(anchor, "checkout", "-q", "-b", "feature-x")
    _commit(anchor, "f\n", "feature work")
    _git(anchor, "push", "-q", "-u", "origin", "feature-x")

    worktree = _cut(anchor, tmp_path)

    assert _git(worktree, "rev-parse", "HEAD") == main_tip


def test_task_branch_does_not_track_the_base(tmp_path):
    """Cutting from origin/main must not make a plain `git push` target main."""
    anchor = _anchor(tmp_path)

    worktree = _cut(anchor, tmp_path)

    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "@{upstream}"], cwd=str(worktree),
        env=_ENV, capture_output=True, text=True,
    )
    assert upstream.returncode != 0, upstream.stdout


def test_origin_head_decides_the_base(tmp_path):
    """The remote's own default branch wins over ``main``."""
    anchor = _anchor(tmp_path)
    _git(anchor, "checkout", "-q", "-b", "trunk")
    trunk_tip = _commit(anchor, "t\n", "trunk work")
    _git(anchor, "push", "-q", "-u", "origin", "trunk")
    _git(anchor, "checkout", "-q", "main")
    _git(anchor, "remote", "set-head", "origin", "trunk")

    assert _worktree_start_point(anchor) == "origin/trunk"
    worktree = _cut(anchor, tmp_path)
    assert _git(worktree, "rev-parse", "HEAD") == trunk_tip


def test_unpushed_commits_on_the_base_branch_are_kept(tmp_path):
    """An anchor deliberately ahead of its base must not lose that work."""
    anchor = _anchor(tmp_path)
    local_tip = _commit(anchor, "local\n", "unpushed work")

    assert _worktree_start_point(anchor) == "HEAD"
    worktree = _cut(anchor, tmp_path)
    assert _git(worktree, "rev-parse", "HEAD") == local_tip


def test_no_remote_falls_back_to_head(tmp_path):
    """A repo with no ``origin`` keeps the old behaviour rather than failing."""
    anchor = tmp_path / "lonely"
    anchor.mkdir()
    _init(anchor)
    tip = _commit(anchor, "x\n", "c1")

    assert _worktree_start_point(anchor) == "HEAD"
    worktree = _cut(anchor, tmp_path)
    assert _git(worktree, "rev-parse", "HEAD") == tip


@pytest.mark.parametrize("existing", [True, False])
def test_existing_branch_is_reused_untouched(tmp_path, existing):
    """A branch that already exists is checked out as-is, never re-based."""
    anchor = _anchor(tmp_path)
    if existing:
        _git(anchor, "branch", "app/t_new")
        _commit(anchor, "later\n", "later on main")
        _git(anchor, "push", "-q", "origin", "main")
        expected = _git(anchor, "rev-parse", "app/t_new")
    else:
        expected = _git(anchor, "rev-parse", "origin/main")

    worktree = _cut(anchor, tmp_path)

    assert _git(worktree, "rev-parse", "HEAD") == expected
