"""Opt-in cleanup: the checkout goes only on success with a clean tree, and a
re-attach rebuilds a checkout whose branch survived."""

from pathlib import Path

import pytest

from conftest import git

CFG = "adws/adw_sssf_config/sssf.config.yaml"


def _isolated(modules, adw_id, **kw):
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, adw_id)
    info = modules["worktree"].ensure(
        run, modules["IsolationRequest"](source_branch="main", **kw))
    return run, info


def test_cleanup_removes_checkout_keeps_branch(repo, modules):
    run, info = _isolated(modules, "cc100001")
    wt = Path(info.worktree_path)
    (wt / "done.txt").write_text("done\n")
    modules["git_helper"].commit_all("finish", root=wt)
    outcome = modules["worktree"].maybe_cleanup(run, True)
    assert outcome == "removed"
    assert not wt.exists()
    assert modules["git_helper"].ref_exists("sssf/cc100001", root=repo)


def test_cleanup_off_by_default(repo, modules):
    run, info = _isolated(modules, "cc200002")
    assert modules["worktree"].maybe_cleanup(run, False) == "off"
    assert Path(info.worktree_path).is_dir()


def test_cleanup_keeps_dirty_tree(repo, modules):
    run, info = _isolated(modules, "cc300003")
    (Path(info.worktree_path) / "unsaved.txt").write_text("precious\n")
    assert modules["worktree"].maybe_cleanup(run, True) == "kept-dirty"
    assert Path(info.worktree_path).is_dir()


def test_cleanup_in_place_run_is_noop(repo, modules):
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, "cc400004")
    assert modules["worktree"].maybe_cleanup(run, True) == "in-place"


def test_cleanup_missing_checkout_never_raises(repo, modules):
    run, info = _isolated(modules, "cc500005")
    wt = Path(info.worktree_path)
    git("worktree", "remove", "--force", str(wt), cwd=repo)
    outcome = modules["worktree"].maybe_cleanup(run, True)
    assert outcome.startswith("remove-failed")


def test_rejoin_recreates_checkout_from_surviving_branch(repo, modules):
    run, info = _isolated(modules, "cc600006")
    wt = Path(info.worktree_path)
    (wt / "kept.txt").write_text("kept\n")
    modules["git_helper"].commit_all("keep me", root=wt)
    git("worktree", "remove", "--force", str(wt), cwd=repo)
    run2, info2 = _isolated(modules, "cc600006")
    assert info2 is not None and info2.reused and info2.recreated
    assert Path(info2.worktree_path).is_dir()
    assert (Path(info2.worktree_path) / "kept.txt").read_text() == "kept\n"


def test_rejoin_without_branch_still_fails_loudly(repo, modules):
    run, info = _isolated(modules, "cc700007")
    wt = Path(info.worktree_path)
    git("worktree", "remove", "--force", str(wt), cwd=repo)
    git("branch", "-D", "sssf/cc700007", cwd=repo)
    with pytest.raises(RuntimeError, match="fresh --adw-id"):
        _isolated(modules, "cc700007")
