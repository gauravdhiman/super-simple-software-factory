#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Plan Build Test Quality — full agent chain plus deterministic quality.

Usage:
    uv run adws/adw_plan_build_test_quality.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4] [--source-branch main] [--no-worktree] [--cleanup]

Phases: engineer(request) -> git(isolate) -> planner -> handoff(seal_plan) -> handoff(verify_handoff) -> builder -> [code(verify) -> code(test) -> builder(fix)] bounded -> git(rebase) -> git(commit)

Verify and test are CODE, not agents. Their commands are known, so running them
needs no judgement — only repairing them does. A failing block does not fail its
phase: the runner did its job, the code is what failed. The failure becomes an
envelope and flows back into the builder, and only an exhausted repair loop
fails the run.

The run is isolated in its own worktree on branch sssf/<adw_id>. After
verification the branch is rebased onto the latest source and the commit lands
there — push the branch and open a PR against the source when ready. Nothing
is pushed automatically.
"""

import argparse
import sys

from adw_modules import agents, gates, git_helper, handoff, quality, session, utils, worktree
from adw_modules.data_types import (AgentCall, BuildOutput, IsolationRequest,
                                    PhaseParams, PlanOutput)

REQUIRED_AGENTS = ["planner", "builder"]
MAX_FIX_LOOPS = 3


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None,
         source_branch: str | None = None, no_worktree: bool = False,
         cleanup: bool = False) -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt)

    with run.phase(PhaseParams(name="isolate", kind="code", owner="git",
                               description="Settle where this run works — its own worktree and branch when isolation applies, otherwise in place — so parallel runs never share a tree")) as ph:
        request = IsolationRequest(source_branch=source_branch, disable=no_worktree)
        info = worktree.ensure(run, request)
        worktree.log_result(ph, run, info, request)

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                               description="Turn the request into an implementable plan")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                                 gates=[gates.artifacts_exist, gates.files_non_empty,
                                        gates.plan_declares_artifacts, gates.plan_summary_present,
                                        gates.plan_handoff_present]))

    with run.phase(PhaseParams(name="seal_plan", kind="code", owner="handoff",
                               description="Fingerprint the plan so the build provably implements this exact spec")) as ph:
        seal = handoff.seal_artifacts(run, "plan", plan.artifacts)
        ph.log(label=seal.label, files=len(seal.files), digest=handoff.digest_of(seal))

    with run.phase(PhaseParams(name="verify_handoff", kind="code", owner="handoff",
                               description="Refuse to build when the plan changed since it was fingerprinted")) as ph:
        checked = handoff.verify_artifacts(run, "plan")
        ph.log(label=checked.label, files=len(checked.files), digest=handoff.digest_of(checked))

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                               description="Implement the plan exactly")) as ph:
        previous = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=plan,
                                     gates=[gates.diff_matches_claims]))

    def record(ph, result) -> None:
        passed = sum(1 for check in result.checks if check.passed)
        ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
               artifacts=", ".join(result.artifacts))

    test_result = None
    quality_result = None
    for i in range(1, MAX_FIX_LOOPS + 1):
        with run.phase(PhaseParams(name=f"verify_{i}", kind="code", owner="quality",
                                   description="Lint, typecheck, and build before testing")) as ph:
            quality_result = quality.run_quality(run, ph.phase)
            record(ph, quality_result)

        # run_quality() already includes the test block; a repo that wants tests
        # in their own phase can split them out the way this comment does.
        test_result = quality_result

        if quality_result.passed and test_result.passed:
            break
        if i == MAX_FIX_LOOPS:
            break

        # Whichever block failed becomes the builder's spec — verbatim command
        # output, no parser standing between the failure and the fix.
        broken = quality_result if not quality_result.passed else test_result
        what = "verification" if not quality_result.passed else "tests"
        with run.phase(PhaseParams(name=f"fix_{i}", kind="agent", owner="builder", retries=1,
                                   description=f"Resolve the reported {what} failures")) as ph:
            previous = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                                         previous=quality.as_envelope(broken, what),
                                         gates=[gates.diff_matches_claims]))

    verified = (quality_result is not None and quality_result.passed
                and test_result is not None and test_result.passed)
    if verified:
        if run.isolation is not None:
            with run.phase(PhaseParams(name="rebase", kind="code", owner="git",
                                       description="Rebase this run's branch onto the latest source so the commit lands on a fresh base")) as ph:
                result = worktree.rebase_onto_source(run)
                ph.log(strategy=result.strategy, onto=result.onto_ref,
                       base=f"{result.from_commit[:7]} -> {result.to_commit[:7]}")
        with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                                   description="Commit the tested and quality-verified working tree")) as ph:
            message = previous.commit_message or f"sssf({run.adw_id}): {previous.summary}"
            ph.log(sha=git_helper.commit_all(message, root=run.repo_root), message=message)

    code = run.finish(accepted=verified,
                      reason=f"verify/test never came back clean after {MAX_FIX_LOOPS} fix attempt(s)")
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
