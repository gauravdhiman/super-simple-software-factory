"""Isolation: one worktree + branch per run, in-place escape hatch."""

from pathlib import Path

import pytest

CFG = "adws/adw_sssf_config/sssf.config.yaml"


def _ensure(modules, adw_id, **kw):
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, adw_id)
    info = modules["worktree"].ensure(
        run, modules["IsolationRequest"](**kw))
    return run, info


def test_isolate_creates_branch_and_worktree(repo, modules):
    run, info = _ensure(modules, "a1b2c3d4", source_branch="main")
    assert info is not None and not info.reused
    assert info.branch == "sssf/a1b2c3d4"
    assert info.source_branch == "main"
    wt = Path(info.worktree_path)
    assert (wt / "app.txt").is_file()
    assert Path(run.repo_root).resolve() == wt.resolve()
    assert run.isolation is not None
    assert (run.session_dir / "isolation.json").is_file()
    # the launching checkout is untouched and still on main
    from conftest import git
    assert git("branch", "--show-current", cwd=repo) == "main"
    assert git("status", "--porcelain", cwd=repo) == ""


def test_isolate_disabled_runs_in_place(repo, modules):
    run, info = _ensure(modules, "b1b2c3d4", disable=True)
    assert info is None and run.isolation is None
    from conftest import git
    assert Path(run.repo_root).resolve() == repo.resolve()
    assert git("worktree", "list", "--porcelain", cwd=repo).count("worktree ") == 1


def test_isolate_disabled_by_config(repo, modules):
    cfg_path = Path(CFG)
    cfg_path.write_text(cfg_path.read_text().replace("enabled: true", "enabled: false"))
    run, info = _ensure(modules, "c1b2c3d4", source_branch="main")
    assert info is None


def test_isolate_unknown_source_branch_fails_cleanly(repo, modules):
    with pytest.raises(RuntimeError, match="--source-branch"):
        _ensure(modules, "d1b2c3d4", source_branch="definitely-not-a-branch")


def test_join_reattaches_to_the_same_worktree(repo, modules):
    run, info = _ensure(modules, "e1b2c3d4", source_branch="main")
    run2, info2 = _ensure(modules, "e1b2c3d4", source_branch="main")
    assert info2 is not None and info2.reused
    assert info2.worktree_path == info.worktree_path
    assert Path(run2.repo_root).resolve() == Path(info.worktree_path).resolve()


def test_legacy_config_without_isolation_section(repo, modules):
    cfg_path = Path(CFG)
    cfg_path.write_text(cfg_path.read_text().split("isolation:")[0])
    cfg = modules["agents"].load_config(CFG)
    assert cfg.isolation.source_branch == "main"
    assert cfg.isolation.enabled is True
    run, info = _ensure(modules, "f1b2c3d4")
    assert info is not None and info.branch == "sssf/f1b2c3d4"
