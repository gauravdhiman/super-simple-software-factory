#!/usr/bin/env python3
"""Reclaim finished runs' worktrees. Dry run by default; --apply deletes.

An isolated run owns `<repo>/<worktree_dir>/sssf-<adw_id>` on branch
`sssf/<adw_id>`. When the run ends, the checkout stays behind — the branch is
the PR vehicle, but the directory is dead weight. This tool classifies every
managed worktree and, only when told to, removes the ones that are provably
done with. It is a reclaimer, not a lifecycle actor:

* branches are NEVER deleted — an unpushed branch is the point, and the tool
  says so instead of touching it;
* a worktree holding uncommitted work is NEVER removed unless --include-dirty
  is passed explicitly (uncommitted work is the only thing removal can lose);
* a running run (session `running`, or live process rows) is NEVER touched,
  whatever the flags say;
* anything younger than --min-age-hours is left alone, because a worktree a
  just-finished engineer is still reading looks exactly like a dead one.

    python3 adws/tools/gc_worktrees.py                      # dry run
    python3 adws/tools/gc_worktrees.py --apply              # actually reclaim
    python3 adws/tools/gc_worktrees.py --min-age-hours 0 --apply   # ...now

Stdlib only, so it runs wherever git + python3 exist. Exit 0 on a clean
pass (even when there was work to do — read the report); exit 1 on an
operational error such as a non-git --root.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GCOptions:
    root: Path
    db: Path
    worktree_dir: str = ".worktrees"
    branch_prefix: str = "sssf/"
    source_branch: str = "main"
    min_age_hours: float = 24.0
    include_dirty: bool = False
    apply: bool = False
    verbose: bool = False


@dataclass
class Verdict:
    path: str                    # worktree path, "" when the dir is already gone
    branch: str
    adw_id: str
    action: str                  # "keep" | "remove"
    reasons: list[str] = field(default_factory=list)
    unpushed: bool = False       # branch holds commits worth pushing first


def _git(*args: str, cwd: Path) -> tuple[bool, str]:
    result = subprocess.run(["git", *args], cwd=str(cwd),
                            capture_output=True, text=True)
    return result.returncode == 0, result.stdout.strip()


def _read_isolation_config(root: Path) -> dict:
    """Best-effort worktree naming from the stamped config. Falls back to the
    defaults when pyyaml is absent or the config does not name them."""
    found: dict = {}
    try:
        import yaml  # type: ignore
    except ImportError:
        return found
    cfg_file = root / "adws" / "adw_sssf_config" / "sssf.config.yaml"
    if not cfg_file.is_file():
        return found
    try:
        isolation = (yaml.safe_load(cfg_file.read_text()) or {}).get("isolation") or {}
    except Exception:
        return found
    for key in ("worktree_dir", "branch_prefix", "source_branch"):
        if isolation.get(key):
            found[key] = str(isolation[key])
    return found


def _worktree_list(root: Path) -> list[dict]:
    ok, out = _git("worktree", "list", "--porcelain", cwd=root)
    if not ok:
        raise RuntimeError(f"cannot list worktrees in {root}")
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


def _session_status(db: Path, adw_id: str) -> tuple[str, bool]:
    """(session status, any live process). Unknown db/table/row -> ("missing", False)."""
    if not db.is_file():
        return "missing", False
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return "missing", False
    try:
        row = conn.execute(
            "SELECT status FROM sessions WHERE adw_id=?", (adw_id,)).fetchone()
        live = conn.execute(
            "SELECT COUNT(*) FROM processes WHERE adw_id=? AND ended_at IS NULL",
            (adw_id,)).fetchone()
        return (row[0] if row else "missing"), bool(live and live[0])
    except sqlite3.Error:
        return "missing", False
    finally:
        conn.close()


def _branch_state(root: Path, wt: Path, branch: str, source: str) -> tuple[bool, bool]:
    """(dirty, unpushed). Best-effort: an unanswerable question keeps the tree."""
    ok, out = _git("status", "--porcelain", cwd=wt)
    dirty = ok and bool(out)
    unpushed = False
    ok, upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=wt)
    if not ok:
        unpushed = True                      # no upstream at all
    else:
        ok, count = _git("rev-list", "--count", f"{upstream}..HEAD", cwd=wt)
        unpushed = ok and count.strip().isdigit() and int(count) > 0
    if not unpushed:
        # No upstream need not mean pushed nowhere: commits past the source
        # are exactly what a PR would carry.
        onto = f"origin/{source}"
        ok, _ = _git("rev-parse", "--verify", "--quiet", f"{onto}^{{commit}}", cwd=root)
        ref = onto if ok else source
        ok, count = _git("rev-list", "--count", f"{ref}..{branch}", cwd=root)
        unpushed = ok and count.strip().isdigit() and int(count) > 0
    return dirty, unpushed


def collect(options: GCOptions) -> list[Verdict]:
    """Classify every managed worktree. Pure inspection — never deletes."""
    root = options.root.resolve()
    if _git("rev-parse", "--git-dir", cwd=root)[0] is False:
        raise RuntimeError(f"{root} is not a git repository")
    managed_home = (root / options.worktree_dir).resolve()
    verdicts: list[Verdict] = []
    now = time.time()
    for entry in _worktree_list(root):
        path = Path(entry.get("path", ""))
        branch = entry.get("branch", "")
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved != path and not str(resolved).startswith(str(managed_home) + os.sep):
            if not str(path).startswith(str(managed_home) + os.sep):
                continue                            # not one of ours
        if branch == "" or entry.get("bare"):
            continue
        adw_id = (branch[len(options.branch_prefix):]
                  if branch.startswith(options.branch_prefix) else "")
        if not adw_id:
            continue                                # foreign branch, not ours
        verdict = Verdict(path=str(path), branch=branch, adw_id=adw_id, action="keep")

        if not path.is_dir():
            verdict.action = "remove"
            verdict.reasons.append("directory is gone — only the registration is left")
            verdicts.append(verdict)
            continue

        status, live = _session_status(options.db, adw_id)
        dirty, unpushed = _branch_state(root, path, branch, options.source_branch)
        verdict.unpushed = unpushed
        age_hours = (now - path.stat().st_mtime) / 3600

        if live or status == "running":
            verdict.reasons.append(f"run is live (session {status})")
        elif dirty and not options.include_dirty:
            verdict.reasons.append("holds uncommitted work (pass --include-dirty to override)")
        elif age_hours < options.min_age_hours:
            verdict.reasons.append(f"only {age_hours:.1f}h old (< {options.min_age_hours}h)")
        elif status == "missing":
            verdict.action = "remove"
            verdict.reasons.append("no session in the trace db — orphaned worktree")
        else:
            verdict.action = "remove"
            verdict.reasons.append(f"session {status}, clean tree")
        verdicts.append(verdict)
    return verdicts


def apply(verdicts: list[Verdict], options: GCOptions) -> None:
    """Remove every worktree verdict says so, then prune stale registrations."""
    for verdict in verdicts:
        if verdict.action != "remove" or not verdict.path:
            continue
        path = Path(verdict.path)
        if not path.is_dir():
            continue
        args = ["worktree", "remove", str(path)]
        if options.include_dirty:
            args.append("--force")
        ok, err = _git(*args, cwd=options.root)
        if not ok:
            print(f"  ! could not remove {verdict.path}: {err}", file=sys.stderr)
    _git("worktree", "prune", cwd=options.root)


def report(verdicts: list[Verdict], options: GCOptions) -> None:
    for verdict in verdicts:
        if verdict.action == "keep" and not options.verbose:
            continue
        mark = "-" if verdict.action == "remove" else "·"
        line = f"  {mark} {verdict.branch} ({verdict.path or 'dir gone'}): {verdict.action}"
        detail = "; ".join(verdict.reasons)
        if detail:
            line += f" — {detail}"
        if verdict.action == "remove" and verdict.unpushed:
            line += f" — NOTE: branch {verdict.branch} is unpushed, push it before it matters"
        print(line)
    removable = sum(1 for v in verdicts if v.action == "remove")
    mode = "would remove" if not options.apply else "removed"
    print(f"{removable} of {len(verdicts)} managed worktree(s) {mode}"
          + ("" if options.apply else " (dry run — pass --apply to delete)"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".",
                        help="repo root holding the worktrees (default: cwd)")
    parser.add_argument("--db", default=None,
                        help="trace db (default: <root>/adws/adw_data/sssf.db)")
    parser.add_argument("--worktree-dir", default=None)
    parser.add_argument("--branch-prefix", default=None)
    parser.add_argument("--source-branch", default=None)
    parser.add_argument("--min-age-hours", type=float, default=24.0)
    parser.add_argument("--include-dirty", action="store_true",
                        help="also reclaim worktrees holding uncommitted work (of finished runs only)")
    parser.add_argument("--apply", action="store_true", help="actually delete; without it nothing is removed")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    defaults = {"worktree_dir": ".worktrees", "branch_prefix": "sssf/",
                "source_branch": "main", **_read_isolation_config(root)}
    options = GCOptions(
        root=root,
        db=Path(args.db).resolve() if args.db else root / "adws" / "adw_data" / "sssf.db",
        worktree_dir=args.worktree_dir or defaults["worktree_dir"],
        branch_prefix=args.branch_prefix or defaults["branch_prefix"],
        source_branch=args.source_branch or defaults["source_branch"],
        min_age_hours=args.min_age_hours,
        include_dirty=args.include_dirty,
        apply=args.apply,
        verbose=args.verbose,
    )
    try:
        verdicts = collect(options)
    except RuntimeError as error:
        print(f"gc_worktrees: {error}", file=sys.stderr)
        return 1
    if options.apply:
        apply(verdicts, options)
    report(verdicts, options)
    return 0


if __name__ == "__main__":
    sys.exit(main())
