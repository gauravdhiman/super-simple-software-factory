"""One git worktree + branch per run, so parallel ADWs never share a tree.

An isolated run owns `<repo>/<worktree_dir>/sssf-<adw_id>` on branch
`<branch_prefix><adw_id>`, cut from the source branch (default `main`,
`--source-branch` or `isolation.source_branch` overrides). Every agent is
spawned in the worktree via `run.repo_root`; the session runtime (trace db,
envelopes, handoff) stays in the launching repo, so one trace still sees all
parallel runs.

Deliberate non-goals: nothing is pushed and no PR is opened — the branch is
left rebased and ready, and the engineer pushes it (`git push -u origin
<branch>` + `gh pr create --base <source>`) when they are happy. A workflow
that only commits locally keeps exactly that nature.

Rebase safety: the branch is never force-updated, conflicts are never
auto-resolved. A conflict aborts the rebase (commits intact) or keeps the
stash (uncommitted work intact) and fails the phase with the recovery
commands, so nothing the agents built can be silently lost.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import git_helper
from .data_types import (IsolationConfig, IsolationInfo, IsolationRequest,
                         RebaseResult)

ISOLATION_FILENAME = "isolation.json"
REMOTE = "origin"


def _isolation_file(run) -> Path:
    return Path(run.session_dir) / ISOLATION_FILENAME


def _read_record(run) -> IsolationInfo | None:
    path = _isolation_file(run)
    if not path.is_file():
        return None
    try:
        return IsolationInfo.model_validate_json(path.read_text())
    except ValueError:
        return None


def resolve_source_branch(cli_value: str | None, cfg: IsolationConfig) -> str:
    """Which ref a new worktree is cut from.

    Precedence: `--source-branch` flag, then `isolation.source_branch` from the
    config, then — only on an interactive terminal — ask, defaulting to `main`.
    Non-interactive runs without either setting silently take `main`, so
    automation never blocks on a prompt nobody can answer.
    """
    if cli_value:
        return cli_value
    if cfg.source_branch:
        return cfg.source_branch
    if sys.stdin.isatty():
        try:
            answer = input("Source branch [main]: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError("no source branch chosen — pass --source-branch")
        return answer or "main"
    return "main"


def _onto_ref(source: str, root) -> tuple[str, bool]:
    """Prefer the freshly fetched remote tracking ref; fall back to local."""
    remote_ref = f"{REMOTE}/{source}"
    if git_helper.remote_exists(REMOTE, root=root) and git_helper.ref_exists(remote_ref, root=root):
        return remote_ref, True
    return source, False


def _registered_branch(outer: Path, worktree_path: Path) -> str | None:
    """The branch a worktree path is registered under, if it still is one."""
    for entry in git_helper.worktree_list(root=outer):
        if Path(entry.get("path", "")).resolve() == worktree_path.resolve():
            return entry.get("branch")
    return None


def ensure(run, request: IsolationRequest) -> IsolationInfo | None:
    """Give this run its own worktree + branch, or re-attach on a joined run.

    Sets `run.repo_root` to the worktree and records it on `run.isolation`, so
    every downstream consumer (agent cwd, permissions, quality, gates) works
    there with no further changes. Returns None when isolation is off, leaving
    the run exactly where it always ran: the current checkout.
    """
    cfg: IsolationConfig = run.cfg.isolation
    if request.disable or not cfg.enabled:
        return None

    outer = Path(run.repo_root)  # still the launching repo at this point
    if not git_helper.is_repo(root=outer):
        raise RuntimeError(
            "isolation needs a git repository — run `git init` with a first commit, "
            "or pass --no-worktree to run in place.")

    # A joined run (--adw-id) re-attaches to the worktree it already owns.
    record = _read_record(run)
    if record is not None:
        path = Path(record.worktree_path)
        branch = _registered_branch(outer, path)
        if branch == record.branch and git_helper.ref_exists(record.branch, root=outer):
            run.repo_root = path
            run.isolation = record.model_copy(update={"reused": True})
            return run.isolation
        raise RuntimeError(
            f"run {run.adw_id} previously isolated onto {record.branch} at {record.worktree_path}, "
            f"but that worktree is gone — recover it (`git worktree list`) or start a fresh --adw-id.")

    source = resolve_source_branch(request.source_branch, cfg)
    if not git_helper.ref_exists(source, root=outer) and not git_helper.ref_exists(
            f"{REMOTE}/{source}", root=outer):
        known = ", ".join(git_helper.local_branches(root=outer)[:10]) or "(none)"
        raise RuntimeError(
            f"source branch {source!r} does not exist — pass --source-branch with one that "
            f"does. Local branches: {known}")

    # Fetch first so the cut (and the later rebase) sees the latest source.
    # Best-effort: a repo with no remote still isolates off its local ref.
    git_helper.fetch(REMOTE, source, root=outer)
    onto, _ = _onto_ref(source, outer)

    branch = f"{cfg.branch_prefix}{run.adw_id}"
    path = outer / cfg.worktree_dir / f"sssf-{run.adw_id}"
    if git_helper.ref_exists(branch, root=outer) or path.exists() \
            or _registered_branch(outer, path) is not None:
        raise RuntimeError(
            f"isolated branch {branch!r} or worktree {path} already exists — a previous run "
            f"with --adw-id {run.adw_id} may have left it behind. Remove it "
            f"(`git worktree remove --force {path}`) or use a fresh --adw-id.")

    git_helper.add_worktree(path, branch, git_helper.rev(onto, root=outer), root=outer)
    info = IsolationInfo(
        adw_id=run.adw_id, branch=branch, worktree_path=str(path.resolve()),
        source_branch=source, onto_ref=onto,
        base_commit=git_helper.rev("HEAD", root=path))
    _isolation_file(run).write_text(info.model_dump_json(indent=2))
    run.repo_root = path.resolve()
    run.isolation = info
    return info


def rebase_onto_source(run) -> RebaseResult:
    """Rebase this run's branch onto the latest source, safely.

    Runs at the end of the work, before the commit phase(s), so the commit
    lands on a fresh base. Dirty trees are stashed (tracked + untracked)
    across the rebase and popped after; a failed rebase is aborted and a
    conflicted pop keeps the stash — either way the phase fails LOUDLY with
    the worktree path and the recovery commands, and nothing is lost.
    """
    record = _read_record(run)
    if record is None or getattr(run, "isolation", None) is None:
        raise RuntimeError(
            "rebase needs an isolated run — this run has no worktree. "
            "Remove the rebase phase or drop --no-worktree.")
    root = Path(run.repo_root)
    if git_helper.current_branch(root=root) != record.branch:
        raise RuntimeError(
            f"expected to rebase on {record.branch} but the worktree is on "
            f"{git_helper.current_branch(root=root)!r} — refusing to move a branch this run does not own.")

    git_helper.fetch(REMOTE, record.source_branch, root=root)
    onto, _ = _onto_ref(record.source_branch, root)
    before = git_helper.rev("HEAD", root=root)
    onto_commit = git_helper.rev(onto, root=root)
    if before == onto_commit:
        return RebaseResult(rebased=False, strategy="already-current",
                            onto_ref=onto, from_commit=before, to_commit=before)

    stashed = git_helper.stash_push(f"sssf({record.adw_id}): pre-rebase", root=root)
    try:
        ahead = git_helper.rev_list_count(onto, "HEAD", root=root)
        if ahead == 0:
            git_helper.merge_ff_only(onto, root=root)
            strategy = "fast-forward"
        else:
            git_helper.rebase(onto, root=root)
            strategy = "rebase-with-stash" if stashed else "rebase"
    except RuntimeError as error:
        git_helper.rebase_abort(root=root)
        if stashed:
            git_helper.stash_pop(root=root)
        raise RuntimeError(
            f"rebase of {record.branch} onto {onto} conflicted — aborted, the branch is "
            f"exactly where it was ({git_helper.short_sha('HEAD', root=root)}). Rebase it by hand "
            f"inside {root} and re-run, or resolve and continue the run. Cause: {error}") from error

    if stashed:
        try:
            git_helper.stash_pop(root=root)
        except RuntimeError as error:
            raise RuntimeError(
                f"rebased onto {onto} cleanly, but restoring the uncommitted work conflicted — "
                f"the branch moved, the work is safe in the stash (`git -C {root} stash list`), "
                f"and the tree holds the conflict markers. Resolve inside {root}, "
                f"`git stash drop` when done. Cause: {error}") from error

    # The record keeps describing the run; only the base moved.
    run.isolation = record.model_copy(update={"onto_ref": onto})
    return RebaseResult(rebased=True, strategy=strategy, onto_ref=onto,
                        from_commit=before, to_commit=git_helper.rev("HEAD", root=root))


def push_hint(record: IsolationInfo) -> str:
    """The two commands that turn a finished isolated run into a PR. A hint —
    the factory never pushes on its own."""
    return (f"git -C {record.worktree_path} push -u {REMOTE} {record.branch} && "
            f"gh pr create --base {record.source_branch} --head {record.branch}")
