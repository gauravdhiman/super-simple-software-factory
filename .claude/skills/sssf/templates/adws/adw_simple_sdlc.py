#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Simple SDLC — plan, build, test, review, document, committing as it goes.

Usage:
    uv run adws/adw_simple_sdlc.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4] [--source-branch main] [--no-worktree] [--cleanup]

Phases: engineer(request) -> git(isolate) -> planner -> handoff(seal_plan) -> git(commit_plan)
        -> handoff(verify_handoff) -> builder -> code(test) [-> builder(fix) -> code(test) ... bounded]
        -> reviewer [-> builder(revise) -> reviewer ... bounded]
        -> code(retest, only if a revision changed code)
        -> git(rebase) -> git(commit_build) -> code(changes) -> documenter -> git(commit_docs)

Three commits, three work products, three authors. The plan, the code, and the
write-up each land in their own commit, and each commit message is the words of
the agent that produced it — `commit_message` on PlanOutput describes the spec,
on BuildOutput the code, on DocumentOutput the write-up. No agent's sentence is
ever reused for another agent's diff.

Testing is CODE, not an agent. `bun test` is a command, not a judgement call:
an agent rediscovering it every run costs a million tokens to learn what a
subprocess already knows. Failures travel back to the builder as an envelope,
so the repair loop is unchanged — only the runner became free and repeatable.

Two different questions still get asked, in order. The suite asks "does it
run"; the reviewer asks "is this what was asked for", against `plan.md` — and
neither can answer the other's. A revision that closes a review finding
re-enters the suite, so the tree that gets committed is the tree that was both
tested and approved.

The code commit lands after verification, not straight after the build: fixes
and revisions are part of the same work product, and red code has no business
on the branch. A run that fails verification therefore leaves the plan
committed and the working tree dirty — the spec is a real artifact either way,
and the unfinished code stays where the engineer can see it.

The run is isolated in its own worktree on branch sssf/<adw_id>, cut from the
source branch. After verification the branch is rebased onto the latest source
before the code commit lands — push the branch and open a PR against the
source when ready. Nothing is pushed automatically.

The documenter measures against the commit this run's worktree STARTED from,
not against the source branch, because by then the branch has moved. That
baseline is pinned right after isolation and printed in the isolate phase.
"""

import argparse
import sys

from adw_modules import agents, changes, gates, git_helper, handoff, quality, session, utils, worktree
from adw_modules.data_types import (AgentCall, BuildOutput, ChangeCapture,
                                    DocumentOutput, IsolationRequest,
                                    PhaseParams, PlanOutput, ReviewOutput)

REQUIRED_AGENTS = ["planner", "builder", "reviewer", "documenter"]
MAX_FIX_LOOPS = 3
MAX_REVISION_LOOPS = 2

DOCUMENT_NOTES = ("Read diff_path in full before writing. Document only what the "
                  "diff shows, then copy the write-up into app_docs/ as your task "
                  "describes.")


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None,
         source_branch: str | None = None, no_worktree: bool = False,
         cleanup: bool = False) -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)

    def commit(ph, envelope) -> None:
        """Commit what the preceding phase produced, in that agent's own words."""
        message = envelope.commit_message or f"sssf({run.adw_id}): {envelope.summary}"
        ph.log(sha=git_helper.commit_all(message, root=run.repo_root), message=message)

    def record(ph, result) -> None:
        """Log a deterministic block's verdict — the same shape every ADW uses."""
        passed = sum(1 for check in result.checks if check.passed)
        ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
               artifacts=", ".join(result.artifacts))

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt)

    with run.phase(PhaseParams(name="isolate", kind="code", owner="git",
                               description="Give this run its own worktree and branch so parallel runs never share a tree")) as ph:
        info = worktree.ensure(run, IsolationRequest(source_branch=source_branch, disable=no_worktree))
        if info is None:
            ph.log(mode="in-place", root=str(run.repo_root))
        else:
            ph.log(branch=info.branch, worktree=info.worktree_path,
                   source=f"{info.source_branch} ({info.onto_ref})",
                   base=git_helper.short_sha(info.base_commit, root=run.repo_root),
                   reused=info.reused, recreated=info.recreated)
    baseline = git_helper.rev("HEAD", root=run.repo_root)  # pinned after isolation, before this run commits anything

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                               description="Turn the request into an implementable plan")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                                 gates=[gates.artifacts_exist, gates.files_non_empty]))

    with run.phase(PhaseParams(name="seal_plan", kind="code", owner="handoff",
                               description="Fingerprint the plan so the build provably implements this exact spec")) as ph:
        seal = handoff.seal_artifacts(run, "plan", plan.artifacts)
        ph.log(label=seal.label, files=len(seal.files), digest=handoff.digest_of(seal))

    with run.phase(PhaseParams(name="commit_plan", kind="code", owner="git",
                               description="Put the spec on record before any code exists to blur it")) as ph:
        commit(ph, plan)

    with run.phase(PhaseParams(name="verify_handoff", kind="code", owner="handoff",
                               description="Refuse to build when the plan changed since it was fingerprinted")) as ph:
        checked = handoff.verify_artifacts(run, "plan")
        ph.log(label=checked.label, files=len(checked.files), digest=handoff.digest_of(checked))

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                               description="Implement the plan exactly")) as ph:
        build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=plan,
                                  gates=[gates.diff_matches_claims]))

    test = None
    for i in range(1, MAX_FIX_LOOPS + 1):
        with run.phase(PhaseParams(name=f"test_{i}", kind="code", owner="quality",
                                   description="Run the suite — a known command, so code runs "
                                               "it and no agent has to rediscover it")) as ph:
            test = quality.run_tests(run, ph.phase)
            record(ph, test)

        if test.passed:
            break

        with run.phase(PhaseParams(name=f"fix_{i}", kind="agent", owner="builder", retries=1,
                                   description="Repair what the suite reported, from its "
                                               "verbatim output")) as ph:
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                                      previous=quality.as_envelope(test, "tests"),
                                      gates=[gates.diff_matches_claims]))

    review = None
    revised = False
    for i in range(1, MAX_REVISION_LOOPS + 1):
        with run.phase(PhaseParams(name=f"review_{i}", kind="agent", owner="reviewer",
                                   description="Confirm the build matches the plan")) as ph:
            review = ph.call(AgentCall(output_type=ReviewOutput, prompt=prompt, previous=build,
                                       gates=[gates.artifacts_exist, gates.verdict_consistent]))

        if review.approved or i == MAX_REVISION_LOOPS:
            break

        with run.phase(PhaseParams(name=f"revise_{i}", kind="agent", owner="builder", retries=1,
                                   description="Close the reviewer's blocking findings")) as ph:
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=review,
                                      gates=[gates.diff_matches_claims]))
            revised = True

    # A revision edited code after the suite last ran, so the green light is
    # stale. Re-run it rather than commit on a result that predates the change.
    if revised and review is not None and review.approved:
        with run.phase(PhaseParams(name="retest", kind="code", owner="quality",
                                   description="Re-run the suite — the revision changed code "
                                               "after the last green result")) as ph:
            test = quality.run_tests(run, ph.phase)
            record(ph, test)

    # Red tests or a rejected review stop the chain here: the code stays
    # uncommitted and nothing is documented, because there is nothing worth
    # describing yet. The plan commit stands — it is a record of what was asked.
    verified = (test is not None and test.passed
                and review is not None and review.approved)
    if verified:
        if run.isolation is not None:
            with run.phase(PhaseParams(name="rebase", kind="code", owner="git",
                                       description="Rebase this run's branch onto the latest source so the code commit lands on a fresh base")) as ph:
                result = worktree.rebase_onto_source(run)
                ph.log(strategy=result.strategy, onto=result.onto_ref,
                       base=f"{result.from_commit[:7]} -> {result.to_commit[:7]}")
        with run.phase(PhaseParams(name="commit_build", kind="code", owner="git",
                                   description="Land the code only now: green suite, approved review")) as ph:
            commit(ph, build)

        with run.phase(PhaseParams(name="changes", kind="code", owner="git",
                                   description="Diff the whole run against its pinned baseline, for the documenter")) as ph:
            changeset = changes.capture(run, ChangeCapture(base=baseline))
            ph.log(base=f"{changeset.base.label} @ {changeset.base.commit[:7]}",
                   reason=changeset.base.reason,
                   files=len(changeset.files) + len(changeset.untracked),
                   lines=f"+{changeset.insertions} -{changeset.deletions}",
                   diff=changeset.diff_path)
            if changeset.empty:
                raise RuntimeError(
                    f"nothing changed since {changeset.base.label} "
                    f"({changeset.base.reason}) — there is nothing to document.")

        with run.phase(PhaseParams(name="document", kind="agent", owner="documenter", retries=1,
                                   description="Write up the completed change")) as ph:
            document = ph.call(AgentCall(output_type=DocumentOutput, prompt=prompt,
                                         previous=changes.as_envelope(changeset, DOCUMENT_NOTES,
                                                                      root=run.repo_root),
                                         gates=[gates.artifacts_exist, gates.files_non_empty]))

        with run.phase(PhaseParams(name="commit_docs", kind="code", owner="git",
                                   description="Ship the write-up in its own commit, beside the code it describes")) as ph:
            commit(ph, document)

    code = run.finish(accepted=verified,
                      reason="the suite or the review never came back clean")
    if code == 0:
        worktree.maybe_cleanup(run, cleanup or cfg.isolation.cleanup_on_success)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text or a path to a prompt file")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    parser.add_argument("--source-branch", default=None,
                        help="branch the isolated worktree is cut from (default: isolation.source_branch in config, else main)")
    parser.add_argument("--no-worktree", action="store_true",
                        help="run in the current checkout instead of an isolated worktree")
    parser.add_argument("--cleanup", action="store_true",
                        help="remove this run's worktree checkout on success when the tree is clean (the branch is always kept)")
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id,
                  source_branch=args.source_branch, no_worktree=args.no_worktree,
                  cleanup=args.cleanup))
