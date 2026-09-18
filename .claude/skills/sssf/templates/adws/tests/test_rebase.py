"""Safe rebase: fresh base before the commit, conflicts abort loudly."""

from pathlib import Path

import pytest

from conftest import advance_main, git

CFG = "adws/adw_sssf_config/sssf.config.yaml"


def _isolated(modules, adw_id):
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, adw_id)
    info = modules["worktree"].ensure(
        run, modules["IsolationRequest"](source_branch="main"))
    return run, info


def test_rebase_already_current(repo, modules):
    run, info = _isolated(modules, "r1111111")
    res = modules["worktree"].rebase_onto_source(run)
    assert not res.rebased and res.strategy == "already-current"
    assert res.from_commit == res.to_commit


def test_rebase_dirty_tree_stashes_across_fast_forward(repo, modules):
    run, info = _isolated(modules, "r2222222")
    wt = Path(info.worktree_path)
    (wt / "uncommitted.txt").write_text("precious uncommitted work\n")
    new_tip = advance_main(repo, "hello\nmain-step-1\n", "main step 1")
    res = modules["worktree"].rebase_onto_source(run)
    assert res.rebased and res.strategy == "fast-forward"
    assert modules["git_helper"].rev("HEAD", root=wt) == new_tip
    assert (wt / "uncommitted.txt").read_text() == "precious uncommitted work\n"
    assert git("stash", "list", cwd=wt) == ""


def test_rebase_replays_branch_commits(repo, modules):
    run, info = _isolated(modules, "r3333333")
    wt = Path(info.worktree_path)
    (wt / "feature.txt").write_text("feature work\n")
    modules["git_helper"].commit_all("feature commit", root=wt)
    advance_main(repo, "hello\nmain-side\n", "main side step")
    res = modules["worktree"].rebase_onto_source(run)
    assert res.rebased and res.strategy == "rebase"
    assert (wt / "feature.txt").read_text() == "feature work\n"
    assert (wt / "app.txt").read_text() == "hello\nmain-side\n"
    log = git("log", "--oneline", cwd=wt)
    assert "feature commit" in log and "main side step" in log


def test_rebase_conflict_aborts_with_branch_intact(repo, modules):
    run, info = _isolated(modules, "r4444444")
    wt = Path(info.worktree_path)
    (wt / "app.txt").write_text("hello\nbranch-line\n")
    modules["git_helper"].commit_all("branch-side change", root=wt)
    branch_head = modules["git_helper"].rev("HEAD", root=wt)
    advance_main(repo, "hello\nmain-line\n", "main conflicting step")
    with pytest.raises(RuntimeError, match="conflicted"):
        modules["worktree"].rebase_onto_source(run)
    assert modules["git_helper"].rev("HEAD", root=wt) == branch_head
    assert git("status", "--porcelain", cwd=wt) == ""
    assert git("stash", "list", cwd=wt) == ""


def test_rebase_without_isolation_refuses(repo, modules):
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, "r5555555")
    with pytest.raises(RuntimeError, match="no worktree"):
        modules["worktree"].rebase_onto_source(run)


def test_rebase_on_foreign_branch_refuses(repo, modules):
    run, info = _isolated(modules, "r6666666")
    wt = Path(info.worktree_path)
    git("checkout", "-qb", "some-other-branch", cwd=wt)
    with pytest.raises(RuntimeError, match="does not own"):
        modules["worktree"].rebase_onto_source(run)
