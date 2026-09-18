"""gc_worktrees: dead checkouts reclaimed, live/dirty/young ones never touched."""

import importlib.util
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from conftest import git

CFG = "adws/adw_sssf_config/sssf.config.yaml"
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"


def _gc():
    spec = importlib.util.spec_from_file_location(
        "gc_worktrees", TOOLS_DIR / "gc_worktrees.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # dataclasses resolve via sys.modules
    spec.loader.exec_module(module)
    return module


def _isolated_finished(modules, adw_id, commit=True):
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, adw_id)
    info = modules["worktree"].ensure(
        run, modules["IsolationRequest"](source_branch="main"))
    wt = Path(info.worktree_path)
    if commit:
        (wt / f"done_{adw_id}.txt").write_text("done\n")
        modules["git_helper"].commit_all(f"finish {adw_id}", root=wt)
    run.tracer.session_finish(adw_id, ok=True)
    return run, info


def _age(path: Path, hours: float) -> None:
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def _options(gc, repo, **kw):
    return gc.GCOptions(root=repo, db=repo / "adws" / "adw_data" / "sssf.db", **kw)


def test_finished_clean_old_is_reclaimed(repo, modules):
    gc = _gc()
    run, info = _isolated_finished(modules, "gc100001")
    _age(Path(info.worktree_path), 100)
    verdicts = gc.collect(_options(gc, repo))
    match = [v for v in verdicts if v.adw_id == "gc100001"]
    assert len(match) == 1 and match[0].action == "remove"


def test_running_run_is_never_touched(repo, modules):
    gc = _gc()
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, "gc200002")
    info = modules["worktree"].ensure(
        run, modules["IsolationRequest"](source_branch="main"))
    _age(Path(info.worktree_path), 100)
    verdicts = gc.collect(_options(gc, repo, min_age_hours=0, include_dirty=True))
    match = [v for v in verdicts if v.adw_id == "gc200002"]
    assert match and match[0].action == "keep"


def test_live_process_row_blocks_removal(repo, modules):
    gc = _gc()
    run, info = _isolated_finished(modules, "gc300003")
    run.tracer.process_start("gc300003", "agent", "builder", 987654321, "fake")
    _age(Path(info.worktree_path), 100)
    verdicts = gc.collect(_options(gc, repo, min_age_hours=0))
    match = [v for v in verdicts if v.adw_id == "gc300003"]
    assert match and match[0].action == "keep"


def test_dirty_worktree_kept_unless_explicit(repo, modules):
    gc = _gc()
    run, info = _isolated_finished(modules, "gc400004", commit=False)
    (Path(info.worktree_path) / "unsaved.txt").write_text("precious\n")
    _age(Path(info.worktree_path), 100)
    verdicts = gc.collect(_options(gc, repo, min_age_hours=0))
    assert [v for v in verdicts if v.adw_id == "gc400004"][0].action == "keep"
    verdicts = gc.collect(_options(gc, repo, min_age_hours=0, include_dirty=True))
    assert [v for v in verdicts if v.adw_id == "gc400004"][0].action == "remove"


def test_orphan_without_session_is_reclaimed(repo, modules):
    gc = _gc()
    wt = repo / ".worktrees" / "sssf-orphan90"
    git("worktree", "add", "-b", "sssf/orphan90", str(wt), "main", cwd=repo)
    _age(wt, 100)
    verdicts = gc.collect(_options(gc, repo))
    match = [v for v in verdicts if v.adw_id == "orphan90"]
    assert match and match[0].action == "remove" and "orphan" in " ".join(match[0].reasons)


def test_young_finished_worktree_is_kept(repo, modules):
    gc = _gc()
    _isolated_finished(modules, "gc600006")
    verdicts = gc.collect(_options(gc, repo))
    match = [v for v in verdicts if v.adw_id == "gc600006"]
    assert match and match[0].action == "keep"


def test_apply_removes_dir_keeps_branch_and_warns_unpushed(repo, modules, capsys):
    gc = _gc()
    run, info = _isolated_finished(modules, "gc700007")
    wt = Path(info.worktree_path)
    _age(wt, 100)
    code = gc.main(["--root", str(repo), "--min-age-hours", "0", "--apply"])
    assert code == 0
    assert not wt.exists()
    assert modules["git_helper"].ref_exists("sssf/gc700007", root=repo)
    out = capsys.readouterr().out
    assert "sssf/gc700007" in out and "unpushed" in out


def test_apply_spares_dirty_worktree(repo, modules):
    gc = _gc()
    run, info = _isolated_finished(modules, "gc800008", commit=False)
    wt = Path(info.worktree_path)
    (wt / "unsaved.txt").write_text("precious\n")
    code = gc.main(["--root", str(repo), "--min-age-hours", "0", "--apply"])
    assert code == 0 and wt.is_dir()


def test_non_git_root_is_an_error(repo, modules, tmp_path):
    gc = _gc()
    outside = tmp_path / "outside-any-repo"
    outside.mkdir()
    assert gc.main(["--root", str(outside)]) == 1
