"""Shared fixtures: a throwaway git repo with the factory modules on sys.path.

Every test gets a fresh remote + clone on `main`, local git identity, and a
minimal agent-less config — enough for session/isolate/rebase/quality paths,
with no agents and no API keys. Tests chdir into the repo (the factory
resolves relative paths against the process cwd) and monkeypatch restores it.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ADWS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ADWS_DIR))

CONFIG_TEXT = """\
defaults:
  coding_agent: pi
  model: google/gemini-3.6-flash
  thinking: medium
  data_dir: adws/adw_data
observability:
  db: adws/adw_data/sssf.db
isolation:
  enabled: true
  source_branch: main
  worktree_dir: .worktrees
  branch_prefix: sssf/
agents: []
"""


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    remote = tmp_path / "remote.git"
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(root)], check=True)
    git("checkout", "-qb", "main", cwd=root)
    git("config", "user.name", "t", cwd=root)
    git("config", "user.email", "t@t", cwd=root)
    (root / "app.txt").write_text("hello\n")
    (root / ".gitignore").write_text(
        ".worktrees/\nadws/adw_data/sessions/\nadws/adw_data/sssf.db*\n")
    (root / "adws" / "adw_sssf_config").mkdir(parents=True)
    (root / "adws" / "adw_sssf_config" / "sssf.config.yaml").write_text(CONFIG_TEXT)
    git("add", "-A", cwd=root)
    git("commit", "-qm", "init", cwd=root)
    git("push", "-q", "origin", "main", cwd=root)
    monkeypatch.chdir(root)
    return root


@pytest.fixture()
def modules():
    """The factory modules, imported from this checkout's adws/ tree."""
    import adw_modules  # noqa: F401
    from adw_modules import agents, gates, git_helper, quality, session, worktree
    from adw_modules.data_types import IsolationRequest
    return {
        "agents": agents, "gates": gates, "git_helper": git_helper,
        "quality": quality, "session": session, "worktree": worktree,
        "IsolationRequest": IsolationRequest,
    }


def advance_main(root: Path, content: str, msg: str) -> str:
    (root / "app.txt").write_text(content)
    git("add", "-A", cwd=root)
    git("commit", "-qm", msg, cwd=root)
    git("push", "-q", "origin", "main", cwd=root)
    return git("rev-parse", "HEAD", cwd=root)
