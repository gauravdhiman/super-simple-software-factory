"""Run manifest: one file saying what a run did.

The trace already records everything — phases, events, envelopes, gates —
but answering "what did this run do" takes SQL. The manifest is the curated
summary, built in run.finish() from the in-memory run plus the trace it
already wrote: no new information, just one place to read it.

Two homes, per the factory's standing split: `manifest.json` beside the
session stays the raw record; `sessions.manifest_json` is the queryable
mirror the visualizer polls. Writing it must never fail the run — a finished
run with a missing manifest is a logging gap, not a failed run.
"""

from __future__ import annotations

from pathlib import Path

from . import handoff as handoff_module
from .data_types import (CommitRecord, HandoffSeal, HandoffState, PhaseSummary,
                         RunManifest)

MANIFEST_FILENAME = "manifest.json"


def build(run) -> RunManifest:
    """Assemble the manifest. Read-only: touches neither the repo nor the run."""
    record = RunManifest(
        adw_id=run.adw_id,
        engineer=run.engineer,
        tokens=run.tokens,
        cost=run.cost,
        phases=[PhaseSummary(seq=p.seq, name=p.params.name, kind=p.params.kind,
                             owner=p.params.owner, status=p.status)
                for p in run.phases],
    )
    row = run.tracer.session_row(run.adw_id)
    record.adw_name = row.get("adw_name") or ""
    record.status = row.get("status") or ""
    record.started_at = row.get("started_at") or ""
    record.ended_at = row.get("ended_at") or ""

    isolation = getattr(run, "isolation", None)
    if isolation is not None:
        record.branch = isolation.branch
        record.worktree_path = isolation.worktree_path
        record.source_branch = isolation.source_branch
        record.onto_ref = isolation.onto_ref

    names = {p.phase_id: p.params.name for p in run.phases}
    for phase_id, _name, payload in run.tracer.log_payloads(run.adw_id, "log"):
        if isinstance(payload, dict) and payload.get("sha"):
            record.commits.append(CommitRecord(
                phase=names.get(phase_id, ""),
                sha=str(payload["sha"]),
                message=str(payload.get("message", ""))))

    for sidecar in sorted(Path(run.session_dir).glob("handoff_*.json")):
        try:
            seal = HandoffSeal.model_validate_json(sidecar.read_text())
        except ValueError:
            continue                       # a half-written seal is not a handoff
        record.handoffs.append(HandoffState(
            label=seal.label, digest=handoff_module.digest_of(seal),
            files=len(seal.files)))
    return record


def write(run) -> RunManifest:
    """Persist the manifest to both its homes. Called from run.finish()."""
    record = build(run)
    payload = record.model_dump_json(indent=2)
    (Path(run.session_dir) / MANIFEST_FILENAME).write_text(payload)
    run.tracer.session_manifest(run.adw_id, payload)
    return record
