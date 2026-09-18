"""Isolate outcome line: the log says what happened — created, reused, or why not."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import worktree  # noqa: E402
from adw_modules.data_types import (IsolationInfo, IsolationRequest,  # noqa: E402
                                    SSSFConfig)


class Log:
    def __init__(self):
        self.lines = []

    def log(self, **payload):
        self.lines.append(payload)


def _run(enabled=True):
    cfg = SSSFConfig()
    cfg.isolation.enabled = enabled
    return SimpleNamespace(adw_id="abc", repo_root="/repo", cfg=cfg)


def _info(**kw):
    args = {"adw_id": "abc", "branch": "sssf/abc",
            "worktree_path": "/repo/.worktrees/sssf-abc",
            "source_branch": "main", "onto_ref": "origin/main",
            "base_commit": "0" * 40}
    args.update(kw)
    return IsolationInfo(**args)


def test_created_names_path_and_branch():
    ph = Log()
    worktree.log_result(ph, _run(), _info(),
                        IsolationRequest(disable=False))
    (line,) = ph.lines
    assert "worktree created" in line["outcome"]
    assert "/repo/.worktrees/sssf-abc" in line["outcome"]
    assert "sssf/abc" in line["outcome"]
    assert line["branch"] == "sssf/abc" and line["worktree"] == "/repo/.worktrees/sssf-abc"


def test_reused_and_recreated_say_so():
    ph = Log()
    worktree.log_result(ph, _run(), _info(reused=True),
                        IsolationRequest(disable=False))
    assert "reused" in ph.lines[0]["outcome"] and "joined run" in ph.lines[0]["outcome"]
    worktree.log_result(ph, _run(), _info(reused=True, recreated=True),
                        IsolationRequest(disable=False))
    assert "recreated" in ph.lines[1]["outcome"]


def test_in_place_names_the_reason():
    cases = [
        (IsolationRequest(disable=True, reason="read-only agent, nothing to isolate"),
         "read-only agent, nothing to isolate"),
        (IsolationRequest(disable=True, reason="--no-worktree passed"),
         "--no-worktree passed"),
        (IsolationRequest(disable=True), "--no-worktree passed"),
    ]
    for request, expected in cases:
        ph = Log()
        worktree.log_result(ph, _run(), None, request)
        (line,) = ph.lines
        assert "no worktree created" in line["outcome"]
        assert expected in line["outcome"]
        assert line["root"] == "/repo"


def test_in_place_config_off_says_config():
    ph = Log()
    worktree.log_result(ph, _run(enabled=False), None, IsolationRequest(disable=False))
    assert "isolation.enabled is false" in ph.lines[0]["outcome"]
