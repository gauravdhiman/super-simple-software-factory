"""Run manifest: every finished run self-describes, in the session dir and the db."""

import json
import sqlite3
from pathlib import Path

from adw_modules import handoff
from adw_modules import manifest as manifest_module
from adw_modules.data_types import PhaseParams
from adw_modules.tracer import Tracer

CFG = "adws/adw_sssf_config/sssf.config.yaml"


def _request(run):
    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input="manifest probe")


def test_finished_isolated_run_writes_manifest_both_homes(repo, modules):
    agents, session, worktree = modules["agents"], modules["session"], modules["worktree"]
    git_helper = modules["git_helper"]
    run = session.ensure(agents.load_config(CFG), "m1111111")
    _request(run)
    with run.phase(PhaseParams(name="isolate", kind="code", owner="git",
                               description="Isolate this probe run")) as ph:
        info = worktree.ensure(run, modules["IsolationRequest"](source_branch="main"))
        ph.log(branch=info.branch)
    wt = Path(info.worktree_path)
    (wt / "specs").mkdir(exist_ok=True)
    (wt / "specs" / "plan.md").write_text("the plan\n")
    (wt / "built.txt").write_text("the work\n")
    with run.phase(PhaseParams(name="seal_plan", kind="code", owner="handoff",
                               description="Seal the probe plan")) as ph:
        seal = handoff.seal_artifacts(run, "plan", ["specs/plan.md"])
        ph.log(digest=handoff.digest_of(seal))
    with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                               description="Commit the probe work")) as ph:
        sha = git_helper.commit_all("probe commit", root=wt)
        ph.log(sha=sha, message="probe commit")
    assert run.finish() == 0

    on_disk = json.loads((run.session_dir / "manifest.json").read_text())
    assert on_disk["adw_id"] == "m1111111" and on_disk["status"] == "success"
    assert [p["name"] for p in on_disk["phases"]] == ["request", "isolate", "seal_plan", "commit"]
    assert on_disk["commits"] == [{"phase": "commit", "sha": sha, "message": "probe commit"}]
    assert on_disk["branch"] == "sssf/m1111111" and on_disk["source_branch"] == "main"
    assert on_disk["handoffs"] == [{"label": "plan", "digest": handoff.digest_of(seal), "files": 1}]

    mirrored = json.loads(sqlite3.connect(run.tracer.db_path).execute(
        "SELECT manifest_json FROM sessions WHERE adw_id='m1111111'").fetchone()[0])
    assert mirrored == on_disk

    listed = sqlite3.connect(run.tracer.db_path).execute(
        "SELECT branch, worktree_path, source_branch FROM sessions WHERE adw_id='m1111111'").fetchone()
    assert listed[0] == "sssf/m1111111" and listed[2] == "main"


def test_failed_run_manifest_records_fail(repo, modules):
    run = modules["session"].ensure(modules["agents"].load_config(CFG), "m2222222")
    _request(run)
    assert run.finish(accepted=False, reason="probe failure") == 1
    on_disk = json.loads((run.session_dir / "manifest.json").read_text())
    assert on_disk["status"] == "fail"


def test_in_place_run_manifest_has_no_branch(repo, modules):
    run = modules["session"].ensure(modules["agents"].load_config(CFG), "m3333333")
    _request(run)
    assert run.finish() == 0
    on_disk = json.loads((run.session_dir / "manifest.json").read_text())
    assert on_disk["branch"] == "" and on_disk["worktree_path"] == ""


def test_old_database_gains_the_new_columns(repo, modules):
    db = repo / "adws" / "adw_data" / "sssf.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (adw_id TEXT PRIMARY KEY, status TEXT)")
    conn.commit()
    conn.close()
    tracer = Tracer(db, repo / "adws" / "adw_data" / "sessions" / "x" / "events.jsonl")
    cols = {row[1] for row in tracer.conn.execute("PRAGMA table_info(sessions)")}
    assert {"branch", "worktree_path", "source_branch", "manifest_json"} <= cols


def test_manifest_failure_never_breaks_finish(repo, modules, monkeypatch):
    run = modules["session"].ensure(modules["agents"].load_config(CFG), "m4444444")
    _request(run)

    def boom(_run):
        raise RuntimeError("boom")

    monkeypatch.setattr(manifest_module, "write", boom)
    assert run.finish() == 0
