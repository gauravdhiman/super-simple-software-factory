"""Validation gates: verify the envelope's CLAIMS, never guesses.

A gate is `gate(envelope, run) -> GateReport` — one check per item it looked at.
Violations are derived from the failed checks and sent back to the SAME agent
session as a correction. Every check is recorded either way, so a green gate
says WHAT it verified instead of only that it passed.

Gates check what is mechanically checkable; plan quality is a reviewer's job.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .data_types import EnvelopeBase, GateReport
from .utils import repo_path

TAIL_CHARS = 1000        # command output kept as evidence on a failure


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


def _repo_path(run, value: str) -> Path:
    """Envelope paths are repo-relative to where the agent worked."""
    return repo_path(getattr(run, "repo_root", None), value)


def artifacts_exist(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = _repo_path(run, a)
        report.check(a, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "declared artifact does not exist")
    return report


def files_non_empty(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = _repo_path(run, a)
        if not (p.exists() and p.is_file()):
            continue                       # existence is artifacts_exist's job
        empty = p.stat().st_size == 0
        report.check(a, not empty, "declared artifact is empty" if empty else _size(p))
    return report


def json_parses(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = _repo_path(run, a)
        if p.suffix != ".json" or not p.exists():
            continue
        try:
            parsed = json.loads(p.read_text())
            report.check(a, True, f"parses, {type(parsed).__name__}")
        except json.JSONDecodeError as e:
            report.check(a, False, f"declared JSON artifact does not parse: {e}")
    return report


def diff_matches_claims(envelope: EnvelopeBase, run) -> GateReport:
    """Every file claimed changed must exist on disk."""
    report = GateReport()
    for f in getattr(envelope, "changed_files", []):
        p = _repo_path(run, f)
        report.check(f, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "claimed changed file does not exist")
    return report


def verdict_consistent(envelope: EnvelopeBase, run) -> GateReport:
    """A review's verdict must agree with the findings it just wrote down.

    Nothing here judges the code — that is the reviewer's job. This checks the
    envelope against itself: an approval that ships blocking items, or a
    rejection that names no problem, is a claim the harness can refute without
    reading a line of the diff.
    """
    report = GateReport()
    approved = bool(getattr(envelope, "approved", False))
    blocking = list(getattr(envelope, "blocking", []))
    unmet = [f.requirement for f in getattr(envelope, "findings", []) if not f.met]

    report.check("approved vs blocking", not (approved and blocking),
                 "no blocking items" if not blocking
                 else f"{len(blocking)} blocking item(s) while approved=true"
                 if approved else f"{len(blocking)} blocking item(s), not approved")
    report.check("approved vs findings", not (approved and unmet),
                 "every requirement met" if not unmet
                 else f"{len(unmet)} unmet requirement(s) while approved=true"
                 if approved else f"{len(unmet)} unmet requirement(s), not approved")
    report.check("rejection names a problem", approved or bool(blocking or unmet),
                 "verdict is supported" if approved or blocking or unmet
                 else "approved=false but no blocking item or unmet requirement was given")
    return report


def tests_pass(command: str):
    """Gate factory: the given shell command must exit 0."""
    def gate(envelope: EnvelopeBase, run) -> GateReport:
        result = subprocess.run(command, shell=True, capture_output=True, text=True,
                                cwd=getattr(run, "repo_root", None))
        ok = result.returncode == 0
        note = f"exit {result.returncode}"
        if not ok:
            note += "\n" + (result.stdout + result.stderr)[-TAIL_CHARS:]
        return GateReport().check(command, ok, note)
    gate.__name__ = f"tests_pass({command})"
    return gate


# ── Plan gates: a plan must be shaped to build from ──────────────────────────
#
# These check the envelope's own contract (see the planner's user.md Report
# section), never plan quality — that is the reviewer's job, or the
# engineer's. A missing summary, zero artifacts, or an empty handoff means the
# planner skipped part of its report, and everything downstream (builder
# context, commit fallback, next-agent notes) reads that report. Deliberately
# absent: commit_message presence — it has a designed fallback, so an empty
# one degrades gracefully instead of failing a good plan.


def plan_declares_artifacts(envelope: EnvelopeBase, run) -> GateReport:
    """The plan left at least one artifact behind — a plan that wrote nothing
    wrote no plan."""
    artifacts = list(getattr(envelope, "artifacts", None) or [])
    return GateReport().check(
        "artifacts",
        bool(artifacts),
        f"{len(artifacts)} declared" if artifacts
        else "plan declares no artifacts — there is nothing for the builder to implement")


def plan_summary_present(envelope: EnvelopeBase, run) -> GateReport:
    """The one-sentence summary exists — the builder quotes it and the commit
    message falls back to it."""
    summary = (getattr(envelope, "summary", None) or "").strip()
    return GateReport().check(
        "summary",
        bool(summary),
        f"{len(summary)} chars" if summary
        else "plan declares no summary — say what the plan does in one sentence")


def plan_handoff_present(envelope: EnvelopeBase, run) -> GateReport:
    """The notes for the next agent exist — the chain's only handoff channel
    besides the artifacts themselves."""
    notes = (getattr(envelope, "notes_for_next_agent", None) or "").strip()
    return GateReport().check(
        "notes_for_next_agent",
        bool(notes),
        f"{len(notes)} chars" if notes
        else "plan names nothing for the next agent — say what the builder must know")
