#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Plan Build Test — the full starter chain.

Usage:
    uv run adws/adw_plan_build_test.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4] [--source-branch main] [--no-worktree] [--cleanup]

Phases: engineer(request) -> git(isolate) -> planner -> handoff(seal_plan) -> handoff(verify_handoff) -> builder -> code(test) [-> builder(fix) -> code(test) ... bounded] -> git(rebase) -> git(commit)

Testing is CODE: the suite's command lives in adw_modules/quality.py, so no
agent spends a context window rediscovering it. Failures flow back to the
builder as an envelope, and only an exhausted fix loop fails the run.

The run is isolated in its own worktree on branch sssf/<adw_id>. After the
suite goes green the branch is rebased onto the latest source and the commit
lands there — push the branch and open a PR against the source when ready.
Nothing is pushed automatically.
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

    def record(ph, result) -> None:
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
                                     gates=[gates.artifacts_exist]))

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
            previous = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                                         previous=quality.as_envelope(test, "tests"),
                                         gates=[gates.artifacts_exist]))

    # Only tested work gets committed — a red suite leaves the tree uncommitted.
    if test is not None and test.passed:
        if run.isolation is not None:
            with run.phase(PhaseParams(name="rebase", kind="code", owner="git",
                                       description="Rebase this run's branch onto the latest source so the commit lands on a fresh base")) as ph:
                result = worktree.rebase_onto_source(run)
                ph.log(strategy=result.strategy, onto=result.onto_ref,
                       base=f"{result.from_commit[:7]} -> {result.to_commit[:7]}")
        with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                                   description="Land the code only after the suite came back green")) as ph:
            message = previous.commit_message or f"sssf({run.adw_id}): {previous.summary}"
            ph.log(sha=git_helper.commit_all(message, root=run.repo_root), message=message)

    code = run.finish(accepted=test is not None and test.passed,
                      reason=f"the suite still failed after {MAX_FIX_LOOPS} fix attempt(s)")
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
