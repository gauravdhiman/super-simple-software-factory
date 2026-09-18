"""Sealed handoffs: the build starts from the reviewed bytes or not at all."""

from pathlib import Path

import pytest

from adw_modules import handoff

CFG = "adws/adw_sssf_config/sssf.config.yaml"


def _run(modules, adw_id):
    cfg = modules["agents"].load_config(CFG)
    return modules["session"].ensure(cfg, adw_id)


def test_seal_verify_roundtrip(repo, modules, tmp_path):
    run = _run(modules, "h1111111")
    (repo / "specs").mkdir(exist_ok=True)
    (repo / "specs" / "plan.md").write_text("the plan\n")
    outside = tmp_path / "external.md"
    outside.write_text("outside\n")
    seal = handoff.seal_artifacts(
        run, "plan", ["specs/plan.md", str(outside)])
    assert seal.files == {"specs/plan.md": seal.files["specs/plan.md"],
                          str(outside.resolve()): seal.files[str(outside.resolve())]}
    assert len(handoff.digest_of(seal)) == 12
    assert (run.session_dir / "handoff_plan.json").is_file()
    checked = handoff.verify_artifacts(run, "plan")
    assert checked.files == seal.files


def test_verify_refuses_tampered_plan(repo, modules):
    run = _run(modules, "h2222222")
    (repo / "specs").mkdir(exist_ok=True)
    target = repo / "specs" / "plan.md"
    target.write_text("v1\n")
    handoff.seal_artifacts(run, "plan", ["specs/plan.md"])
    target.write_text("v2-sneaky-edit\n")
    with pytest.raises(RuntimeError, match="specs/plan.md"):
        handoff.verify_artifacts(run, "plan")


def test_verify_refuses_vanished_plan(repo, modules):
    run = _run(modules, "h3333333")
    target = repo / "doomed.md"
    target.write_text("here today\n")
    handoff.seal_artifacts(run, "plan", ["doomed.md"])
    target.unlink()
    with pytest.raises(RuntimeError, match="doomed.md"):
        handoff.verify_artifacts(run, "plan")


def test_seal_refuses_missing_file_and_empty_list(repo, modules):
    run = _run(modules, "h4444444")
    with pytest.raises(RuntimeError, match="no such file"):
        handoff.seal_artifacts(run, "plan", ["nope.md"])
    with pytest.raises(RuntimeError, match="nothing to seal"):
        handoff.seal_artifacts(run, "plan", [])


def test_verify_without_seal_tells_you_to_seal(repo, modules):
    run = _run(modules, "h5555555")
    with pytest.raises(RuntimeError, match="no seal"):
        handoff.verify_artifacts(run, "plan")


def test_seal_anchors_worktree_relative_paths(repo, modules):
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, "h6666666")
    info = modules["worktree"].ensure(
        run, modules["IsolationRequest"](source_branch="main"))
    wt = Path(info.worktree_path)
    (wt / "specs").mkdir(exist_ok=True)
    (wt / "specs" / "plan.md").write_text("worktree plan\n")
    # the file exists ONLY in the worktree — an in-place resolution would fail
    assert not (repo / "specs" / "plan.md").exists()
    seal = handoff.seal_artifacts(run, "plan", ["specs/plan.md"])
    assert handoff.verify_artifacts(run, "plan").files == seal.files
