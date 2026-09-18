"""Low-level git operations for code phases. All low-level logic lives in adw_modules.

Every helper takes an optional `root`: the worktree the operation runs in.
Omitted, it is the process cwd — which is the pre-isolation behaviour, so
in-place runs are untouched. Isolated runs pass `run.repo_root` (the worktree).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

Root = str | Path | None


def _git(*args: str, cwd: Root = None) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True,
                            cwd=str(cwd) if cwd else None)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_ok(*args: str, cwd: Root = None) -> tuple[bool, str]:
    """Best-effort git: network steps (fetch) may fail, and that is an answer."""
    result = subprocess.run(["git", *args], capture_output=True, text=True,
                            cwd=str(cwd) if cwd else None)
    return result.returncode == 0, (result.stdout.strip() if result.returncode == 0
                                    else result.stderr.strip())


def current_branch(root: Root = None) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)


def create_branch(name: str, root: Root = None) -> str:
    _git("checkout", "-b", name, cwd=root)
    return name


def is_repo(root: Root = None) -> bool:
    result = subprocess.run(["git", "rev-parse", "--git-dir"],
                            capture_output=True, text=True,
                            cwd=str(root) if root else None)
    return result.returncode == 0


def repo_root() -> Path:
    """Absolute root of the codebase — where agents are spawned to work.

    The git toplevel when there is one, else the process cwd (ADWs run fine in a
    non-git dir; only a commit phase requires a repo). Always absolute, so it is
    safe to hand to a subprocess regardless of where the ADW was launched from.
    """
    if is_repo():
        return Path(_git("rev-parse", "--show-toplevel")).resolve()
    return Path.cwd().resolve()


def commit_all(message: str, root: Root = None) -> str:
    """Stage the working tree and commit it. Returns the new short sha."""
    if not is_repo(root):
        raise RuntimeError(
            "not a git repository — a commit phase needs one. Run `git init` in the "
            "repo root (and make a first commit) before running an ADW that commits.")
    _git("add", "-A", cwd=root)
    if not _git("status", "--porcelain", cwd=root):
        raise RuntimeError("nothing to commit — the preceding phases changed no files")
    _git("commit", "-m", message, cwd=root)
    return _git("rev-parse", "--short", "HEAD", cwd=root)


def changed_files(root: Root = None) -> list[str]:
    out = _git("status", "--porcelain", cwd=root)
    return [line[3:] for line in out.splitlines() if line]


# ── diff plumbing (composed into a ChangeSet by documentation.py) ────────────

def ref_exists(ref: str, root: Root = None) -> bool:
    """True when `ref` resolves to a commit. Never raises — this is a question."""
    result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                            capture_output=True, text=True,
                            cwd=str(root) if root else None)
    return result.returncode == 0


def rev(ref: str = "HEAD", root: Root = None) -> str:
    return _git("rev-parse", ref, cwd=root)


def short_sha(ref: str = "HEAD", root: Root = None) -> str:
    return _git("rev-parse", "--short", ref, cwd=root)


def merge_base(ref: str, other: str = "HEAD", root: Root = None) -> str:
    """The commit where `ref` and `other` diverged — the honest base of a branch.

    On the base branch itself this returns HEAD, which makes the diff exactly
    "what is not committed yet". Off it, the diff is the whole branch plus the
    working tree. One command covers both cases, so no ADW has to branch on it.
    """
    return _git("merge-base", ref, other, cwd=root)


def is_dirty(root: Root = None) -> bool:
    return bool(_git("status", "--porcelain", cwd=root))


def untracked_files(root: Root = None) -> list[str]:
    out = _git("ls-files", "--others", "--exclude-standard", cwd=root)
    return [line for line in out.splitlines() if line]


def diff_files(base: str, root: Root = None) -> list[str]:
    """Tracked files that differ between `base` and the working tree."""
    out = _git("diff", "--name-only", base, cwd=root)
    return [line for line in out.splitlines() if line]


def diff_stat(base: str, root: Root = None) -> str:
    return _git("diff", "--stat", base, cwd=root)


def diff_counts(base: str, root: Root = None) -> tuple[int, int]:
    """(insertions, deletions) across the diff. Binary files count as neither."""
    insertions = deletions = 0
    for line in _git("diff", "--numstat", base, cwd=root).splitlines():
        added, removed, *_ = line.split("\t")
        if added.isdigit():
            insertions += int(added)
        if removed.isdigit():
            deletions += int(removed)
    return insertions, deletions


def diff_text(base: str, root: Root = None) -> str:
    return _git("diff", base, cwd=root)


# ── isolation plumbing (composed into a run by worktree.py) ──────────────────

def fetch(remote: str, branch: str, root: Root = None) -> bool:
    """Fetch one branch. Best-effort: no remote is not an error, just False."""
    ok, _ = _git_ok("fetch", remote, branch, cwd=root)
    return ok


def remote_exists(name: str, root: Root = None) -> bool:
    out = _git("remote", cwd=root)
    return name in out.splitlines()


def local_branches(root: Root = None) -> list[str]:
    out = _git("branch", "--format=%(refname:short)", cwd=root)
    return [line.strip() for line in out.splitlines() if line.strip()]


def worktree_list(root: Root = None) -> list[dict]:
    """Every registered worktree: [{path, branch, head, bare}]. Never raises."""
    ok, out = _git_ok("worktree", "list", "--porcelain", cwd=root)
    if not ok:
        return []
    entries: list[dict] = []
    current: dict = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = {"path": line[len("worktree "):].strip()}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].strip().removeprefix("refs/heads/")
        elif line == "bare":
            current["bare"] = True
        elif line == "":
            if current:
                entries.append(current)
                current = {}
    if current:
        entries.append(current)
    return entries


def add_worktree(path: str | Path, branch: str, start_point: str,
                 root: Root = None) -> None:
    """Register a new worktree on a NEW branch cut from `start_point`."""
    _git("worktree", "add", "-b", branch, str(path), start_point, cwd=root)


def attach_worktree(path: str | Path, branch: str, root: Root = None) -> None:
    """Register a worktree on an EXISTING branch (re-attach after a crash)."""
    _git("worktree", "add", str(path), branch, cwd=root)


def rev_list_count(from_ref: str, to_ref: str, root: Root = None) -> int:
    """Commits in `to_ref` not in `from_ref` — how far ahead a branch is."""
    out = _git("rev-list", "--count", f"{from_ref}..{to_ref}", cwd=root)
    return int(out.strip() or 0)


def merge_ff_only(ref: str, root: Root = None) -> None:
    _git("merge", "--ff-only", ref, cwd=root)


def rebase(onto: str, root: Root = None) -> None:
    _git("rebase", onto, cwd=root)


def rebase_abort(root: Root = None) -> None:
    _git_ok("rebase", "--abort", cwd=root)


def stash_push(message: str, root: Root = None) -> bool:
    """Stash tracked + untracked work. True when something was stashed."""
    if not is_dirty(root):
        return False
    _git("stash", "push", "-u", "-m", message, cwd=root)
    return True


def stash_pop(root: Root = None) -> None:
    _git("stash", "pop", cwd=root)
