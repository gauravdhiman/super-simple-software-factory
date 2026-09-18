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
import subprocess
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

    # A joined run (--adw-id) re-attaches to the worktree it already owns. When
    # the checkout is gone (gc, --cleanup, or a hand) but the branch lives on,
    # re-attach a fresh checkout at the recorded path instead of dead-ending:
    # that is what a follow-up run under the same id obviously wants.
    record = _read_record(run)
    if record is not None:
        path = Path(record.worktree_path)
        branch = _registered_branch(outer, path)
        if branch == record.branch and git_helper.ref_exists(record.branch, root=outer):
            run.repo_root = path
            run.isolation = record.model_copy(update={"reused": True})
            run.tracer.session_isolation(run.adw_id, record.branch,
                                         str(path), record.source_branch)
            return run.isolation
        if git_helper.ref_exists(record.branch, root=outer):
            if path.exists() or _registered_branch(outer, path) is not None:
                raise RuntimeError(
                    f"run {run.adw_id} owns branch {record.branch}, but {path} is occupied "
                    f"by something else — clear it (`git worktree remove --force {path}`) "
                    f"or start a fresh --adw-id.")
            git_helper.attach_worktree(path, record.branch, root=outer)
            run.repo_root = path.resolve()
            run.isolation = record.model_copy(update={"reused": True, "recreated": True})
            run.tracer.session_isolation(run.adw_id, record.branch,
                                         str(path.resolve()), record.source_branch)
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
    run.tracer.session_isolation(run.adw_id, info.branch,
                                 str(path.resolve()), info.source_branch)
    return info


def log_result(ph, run, info, request: IsolationRequest) -> None:
    """One unmistakable line saying what the isolate phase decided.

    The phase description cannot know the outcome — it is fixed before
    ensure() runs — so the outcome rides this log line instead: created,
    re-attached, or in place, and when in place, exactly why (the flag, a
    read-only agent, or config). Structured keys ride along for the trace;
    `outcome` is the sentence the terminal shows.
    """
    if info is None:
        if not run.cfg.isolation.enabled:
            why = "isolation.enabled is false in sssf.config.yaml"
        elif request.reason:
            why = request.reason
        elif request.disable:
            why = "--no-worktree passed"
        else:
            why = "isolation did not apply"
        ph.log(outcome=f"in place: no worktree created ({why})",
               root=str(run.repo_root))
        return
    if info.recreated:
        ph.log(outcome=f"worktree recreated at {info.worktree_path} "
                       f"on branch {info.branch}",
               branch=info.branch, worktree=info.worktree_path,
               source=f"{info.source_branch} ({info.onto_ref})")
    elif info.reused:
        ph.log(outcome=f"worktree reused at {info.worktree_path} "
                       f"on branch {info.branch} (joined run)",
               branch=info.branch, worktree=info.worktree_path,
               source=f"{info.source_branch} ({info.onto_ref})")
    else:
        ph.log(outcome=f"worktree created at {info.worktree_path} "
                       f"on branch {info.branch} from {info.source_branch} "
                       f"({info.onto_ref}) — parallel runs cannot share a tree",
               branch=info.branch, worktree=info.worktree_path,
               source=f"{info.source_branch} ({info.onto_ref})")


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


def maybe_cleanup(run, requested: bool) -> str:
    """Remove this run's worktree checkout when that is provably safe.

    Opt-in only (`--cleanup` or `isolation.cleanup_on_success`), and only when
    the tree is clean — every committed bit lives on in the branch, which is
    NEVER deleted here. Anything else (not requested, in-place run, dirty
    tree) keeps the checkout and says why. Never raises: cleanup must not fail
    a run whose work is already done.
    """
    record = getattr(run, "isolation", None)
    if not requested:
        return "off"
    if record is None:
        return "in-place"
    root = Path(run.repo_root)
    try:
        if git_helper.changed_files(root=root):
            run.console.note(f"cleanup: keeping {record.branch} — uncommitted work stays reviewable")
            return "kept-dirty"
        ok, err = _remove_checkout(record)
        if not ok:
            run.console.note(f"cleanup: keeping {record.branch} — could not remove checkout ({err})")
            return f"remove-failed: {err}"
    except Exception as error:                       # cleanup never fails the run
        run.console.note(f"cleanup: keeping {record.branch} — {error}")
        return f"remove-failed: {error}"
    run.console.note(f"cleanup: removed checkout for {record.branch} — branch kept; {push_hint(record)}")
    return "removed"


def _remove_checkout(record: IsolationInfo) -> tuple[bool, str]:
    """Unregister the worktree, run from the main checkout (never from inside
    the tree being removed). The branch and its commits are untouched."""
    wt = Path(record.worktree_path)
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"],
                            cwd=str(wt), capture_output=True, text=True)
    if common.returncode != 0:
        return False, common.stderr.strip()
    outer = str(Path(common.stdout.strip()).parent)
    result = subprocess.run(["git", "worktree", "remove", record.worktree_path],
                            cwd=outer, capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, ""
