"""Path transparency: worktree runs resolve repo-relative paths at the worktree,
while the session runtime stays in the launching repo."""

from pathlib import Path

CFG = "adws/adw_sssf_config/sssf.config.yaml"


class _Envelope:
    artifacts = ["agent_made.txt"]
    changed_files = ["agent_made.txt"]


class _Run:
    def __init__(self, repo_root):
        self.repo_root = repo_root


def test_gates_anchor_repo_relative_paths_at_the_worktree(repo, modules):
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, "p1111111")
    info = modules["worktree"].ensure(
        run, modules["IsolationRequest"](source_branch="main"))
    (Path(info.worktree_path) / "agent_made.txt").write_text("x\n")
    assert modules["gates"].artifacts_exist(_Envelope(), run).passed
    assert modules["gates"].diff_matches_claims(_Envelope(), run).passed
    # the launching checkout does not have the file — anchoring is what finds it
    assert not (repo / "agent_made.txt").exists()


def test_gates_still_work_in_place(repo, modules):
    (repo / "agent_made.txt").write_text("x\n")
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, "p2222222")
    assert modules["gates"].artifacts_exist(_Envelope(), run).passed


def test_session_runtime_stays_in_the_launching_repo(repo, modules):
    cfg = modules["agents"].load_config(CFG)
    run = modules["session"].ensure(cfg, "p3333333")
    modules["worktree"].ensure(run, modules["IsolationRequest"](source_branch="main"))
    assert Path(run.session_dir).is_absolute()
    assert Path(run.session_dir).resolve().is_relative_to(repo.resolve())
    assert Path(run.context_handoff_dir).resolve().is_relative_to(repo.resolve())
