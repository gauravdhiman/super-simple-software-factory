# SSSF Overview

The system map the orchestrator reads on startup — what SSSF is, how a stamped repo is laid out, and which cookbook to load next.

## What SSSF is

Super Simple Software Factory builds repeatable **agents plus code** workflows. Deterministic Python (an ADW script) owns sequencing, retries, and acceptance; agents are bounded nodes inside that graph. Agent proposes, code disposes.

Your job as orchestrator: **run the system, observe the system, help the engineer interact with it.** You do not do the work an ADW exists to do.

## Layout of a stamped repo

```
adws/
├── adw_sssf_config/
│   └── sssf.config.yaml         the agent roster — one agent, one prompt, one purpose
├── adw_prompt.py                smallest ADW: one agent, one prompt, traced end-to-end
├── adw_plan.py, adw_scout.py, adw_build.py, adw_plan_build.py, adw_build_test.py, adw_plan_build_test.py
├── adw_build_review.py          build → review: is this what was asked for? (not testing)
├── adw_document.py              write up the work just done, from git diff vs main
├── adw_simple_sdlc.py           plan → build → test → review → document; commits each product
├── tests/                       pytest suite for the factory itself — git fixtures only, no agents or keys
├── tools/gc_worktrees.py        reclaim finished runs' checkouts, keeping every branch (dry run unless --apply)
├── adw_modules/                 ALL low-level logic — ADW scripts stay thin
│   ├── data_types.py            AgentCall, PhaseParams, Phase, Envelope + one output type per agent call
│   ├── agents.py                load_config, validate, resolve entry → interface + model + thinking
│   ├── runner.py                the Run object: run.phase(PhaseParams) → ph.call(AgentCall)
│   ├── agent_pi.py              Pi interface (v1)   ·   agent_muse.py  Muse interface   ·   agent_opencode.py  OpenCode interface   ·   agent_codex.py  Codex interface   ·   agent_cc.py  Claude Code (stubbed)
│   ├── harness.py               worker-backend contract + dispatch: one adapter per coding_agent value
│   ├── gates.py                 gate(envelope, run) -> GateReport — one check per item verified
│   ├── changes.py               git diff vs a resolved base → ChangeSet → envelope for the documenter
│   ├── handoff.py               fingerprint handoff artifacts at seal time; refuse to build from drifted bytes
│   ├── manifest.py              one file saying what a run did — phases, commits, branch, seals
│   ├── worktree.py              one git worktree + branch per run → rebase onto source, ready to push
│   ├── prompts.py, session.py, tracer.py, console.py, git_helper.py, utils.py
└── adw_data/
    ├── prompt_engineering/{agent}/{system.md,user.md}   tracked — edit prompts HERE, never in the skill
    │                                planner · builder · scout · reviewer · documenter
    ├── sessions/{adw_id}/                               gitignored runtime
    │   ├── agent_map.json       agent → coding-agent session_id + model
    │   ├── context_handoff/     the one place agents write files for the agents that follow
    │   └── {agent}/{prompts/, raw_output.jsonl, envelope.json}
    └── sssf.db                  gitignored SQLite trace db the visualizer polls
```

Workers run on a configured harness: `pi`, `muse`, `opencode`, and `codex` implemented (`coding_agent: pi | muse | opencode | codex`), default model `gemini-3.6-flash`, thinking `medium`. `claude_code` and `omp` are schema-valid with adapters to come.

## The phase model

Every ADW run is a sequence of **phases**, each one `with run.phase(PhaseParams(...))`. Three kinds, three swim lanes:

- **engineer** — the human lane; today the system-input phase (who asked, and for what).
- **agent** — `ph.call(AgentCall(...))`: prompt in → typed envelope out → gates verified.
- **code** — deterministic steps that stand alone (git branch, git commit, migrate). Never buried inside an agent phase.

**Success must be earned — every phase defaults to `fail`.** A clean exit flips it to success; agent phases additionally require the envelope to parse and all gates to come back green. A raise keeps it failed, records an error event, and aborts the run. `retries=N` on an agent phase buys extra gate-correction rounds through the same session before that raise happens.

## Envelopes

Agents have exactly two output channels: reference files written into `context_handoff/`, and a **final valid-JSON response** parsed against the output type the call declared. Code persists it as `envelope.json` and injects it into the next agent's `user.md` via `{{previous_envelope}}`. Bad JSON is never a restart — the harness re-prompts the *same session, context intact*, until it parses (bounded). See `references/handoff.md`.

**The output contract is a synced triad**: the type in `data_types.py` ↔ the `## Report` JSON example in the agent's `user.md` ↔ `output_type=` at the call site. Editing any one of the three means editing all three in the same change — drift between them taxes every call with correction retries.

## Running an ADW

```bash
uv run adws/adw_plan.py "add a /health endpoint"
uv run adws/adw_plan_build.py requests/health.md --adw-id a1b2c3d4
```

The prompt is inline text or a file path. `--adw-id` is optional on every ADW: given one, the run joins that session (same dirs, same `context_handoff/`, agents resume their existing context windows); omitted, a fresh id is minted and printed.

Mutating ADWs run isolated by default: an `isolate` code phase cuts worktree `.worktrees/sssf-<adw_id>` on branch `sssf/<adw_id>` from the source branch (`--source-branch`, else `isolation.source_branch`, else `main`), and committing ADWs add a `rebase` code phase before the commit so it lands on a fresh base. `--no-worktree` runs in place. Read-only ADWs (`scout`, `quality`) and `document` stay in place. Nothing is ever pushed — the branch is left rebased and ready (`git worktree list`, then push + `gh pr create --base <source>`).

## When you have finished reading this

You are done with startup. List the ADWs (`ls adws/adw_*.py`, plus each `Phases:` docstring line) as a table, and **wait for the engineer's request.**

Do not survey anything else — not the trace db, not the config, not past runs, not the repo tree. You do not yet know what the request is, so anything you gather now is a guess about what will matter, spent from the context the real work needs. Every cookbook and reference below is lazy-loaded, one per request, and that is the whole design.

## Where to go next

Load one cookbook per request — this overview is the only one you read up front.

| Request | Cookbook |
|---|---|
| Turn a request into the prompt an ADW gets | `how_to_prompt_for_the_eng.md` — **read before every launch** |
| Set the system up in a repo | `install.md` |
| Write a new ADW script | `create_adw.md` |
| Change an existing ADW chain | `update_adw.md` |
| Generate `sssf.config.yaml` | `create_config.md` |
| Add or retune an agent | `update_config.md` |
| Add low-level logic or a gate | `update_modules.md` |
| Run and monitor a workflow | `how_to_prompt_for_the_eng.md`, then `run_adw.md` |

References, loaded when you need the spec: `references/config.md` (full config schema), `references/handoff.md` (envelope + session layout), `references/observability.md` (events, db tables, polling).
