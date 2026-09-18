"""Codex coding-agent interface.

Runs `codex exec --json` and tails its JSONL stdout line by line, forwarding
each event to a callback WHILE the agent works. Every stream opens with the
server-minted thread id, so the adapter captures it off the first event and
persists the factory-id -> thread-id mapping beside the agent's session dir —
first send runs `codex exec`, every later send runs `codex exec resume` with
the stored id. Resumes stream only new events with context intact (verified
live); resumed turns keep the thread's model, sandbox, and working root, so
resume passes no `-s`/`-C` (the subcommand does not accept them) and the
spawn cwd carries the workspace.

Four deliberate choices, documented because they differ from pi:

* No system-prompt flag: the system text is composed ahead of the user text
  with a separator. Identity and output contract survive; only the channel
  changes.
* No tools allowlist: headless runs set `-c approval_policy="never"` (asks
  never block; failures return to the model) with `-s workspace-write` (the
  observed default is read-only, under which builders cannot write). The
  sandbox stays ON — `--dangerously-bypass-approvals-and-sandbox` is never
  used — operator `--ignore-rules` is never passed, and the repo boundary
  stays enforced post-hoc by permissions.py either way.
* Thinking is model-gated: codex 400-fails on effort values a model does not
  support (verified), so the Pi ladder maps to codex's vocabulary with
  `off` -> `none` and `minimal` -> `low` (the floor below low is none, which
  would disable reasoning rather than quiet it). Unknown values fall back to
  `medium` — never passed through, because passthrough can fail the run.
* `--skip-git-repo-check` always: factory runs land in non-repos (in-place
  scouts), where codex otherwise refuses to start. Harmless inside repos.

Usage rides the stream (`turn.completed` tokens); cost does not, so dollars
read zero, honestly. `input_tokens` already includes the cached share, so
spend per turn is input + output while `cached_input_tokens` is tracked as
the cached share of input — the thinking share of output is reasoning's
blunder-twin: reported, never added. `total` is absent, so window occupancy
is approximated by the turn's input size (the context as it stands).

Auth is the operator's: `codex login`, subscription or keys — the factory
never sees them. Model ids pass through opaquely (non-empty is the only
check); codex itself reports an unknown id clearly at spawn time.

Tested against the installed codex CLI (exec/resume --json).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .data_types import PiRequest, PiResult
from .harness import ARG_VALUE_CHARS, RESULT_SNIPPET_CHARS, clip_text, tool_label
from .utils import now_iso, operator_env

BINARY = ("CODEX_PATH", "codex")
IMPLEMENTED = True

CODEX_PATH = os.environ.get("CODEX_PATH", "codex")
SESSION_MAP_FILENAME = "codex_sessions.json"

SYSTEM_SEPARATOR = "\n\n---\n\n"

# Pi thinking ladder -> codex model_reasoning_effort. Codex rejects values a
# model does not support (HTTP 400, verified live), so only codex's own
# vocabulary is ever sent: `off` disables to `none`, `minimal` floors to
# `low`, and anything unrecognized settles on `medium` instead of risking
# the run on passthrough.
EFFORT = {
    "off": "none",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# Item types that carry the reply rather than work. Everything else with an
# id is a unit of work and folds into one tool_call record.
MESSAGE_ITEM_TYPES = ("agent_message", "reasoning")

# Item types that end a unit of work. Anything else (in_progress and future
# transitional states) only stages the span.
TERMINAL_ITEM_STATES = ("completed", "failed")


def resolve_model(pattern: str) -> str:
    """Codex model ids are opaque to the factory — non-empty passes, and
    codex itself reports an unknown id clearly at spawn time."""
    model = (pattern or "").strip()
    if not model:
        raise ValueError("codex agent needs a model id — "
                         "set model: in sssf.config.yaml")
    return model


def context_window(model: str) -> int:
    """Codex publishes no queryable ceiling — 0 reads as unknown in the UI."""
    return 0


def effort_for(thinking: str) -> str:
    """Pi thinking ladder -> codex reasoning effort. Always a codex-vocabulary
    value: unknown rungs fall back to medium, never passthrough."""
    return EFFORT.get((thinking or "").strip(), "medium")


def compose_prompt(system_prompt: str, user_prompt: str) -> str:
    """System text ahead of user text — the fallback for no --system flag."""
    if system_prompt.strip():
        return f"{system_prompt.strip()}{SYSTEM_SEPARATOR}{user_prompt}"
    return user_prompt


def lookup_session_id(session_dir: str, our_session_id: str) -> Optional[str]:
    """The codex thread id for this factory session, if one was captured on
    an earlier send. None = first send, run `exec` and let the server mint one."""
    mapping = _read_map(session_dir)
    found = mapping.get(our_session_id)
    return found if isinstance(found, str) and found else None


def record_session_id(session_dir: str, our_session_id: str, harness_id: str) -> str:
    """Persist a captured thread id. First write wins — every event in a run
    (including resume's re-emitted thread.started) carries the same id."""
    mapping = _read_map(session_dir)
    if not isinstance(mapping.get(our_session_id), str):
        mapping[our_session_id] = harness_id
        path = _map_path(session_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mapping, indent=2))
    return mapping[our_session_id]


def _map_path(session_dir: str) -> Path:
    return Path(session_dir).parent / SESSION_MAP_FILENAME


def _read_map(session_dir: str) -> dict:
    path = _map_path(session_dir)
    if path.is_file():
        try:
            mapping = json.loads(path.read_text())
            if isinstance(mapping, dict):
                return mapping
        except ValueError:
            pass
    return {}


def build_command(request: PiRequest, harness_id: Optional[str]) -> list[str]:
    """Argv for one headless codex turn. Pure — unit-tested without spawning.

    Fresh sends set everything (model, sandbox, approvals, effort, working
    root); resumes only continue — model/sandbox/root persist on the thread,
    and the resume subcommand accepts neither -s nor -C.
    """
    effort = ["-c", f"model_reasoning_effort={effort_for(request.thinking)}"]
    approvals = ["-c", "approval_policy=never"]
    if harness_id:
        return [
            CODEX_PATH, "exec", "resume",
            "--json", "--skip-git-repo-check",
            "-m", request.model,
            *approvals, *effort,
            harness_id,
            compose_prompt(request.system_prompt, request.prompt),
        ]
    return [
        CODEX_PATH, "exec",
        "--json", "--skip-git-repo-check",
        "-C", request.cwd,
        "-m", request.model,
        "-s", "workspace-write",
        *approvals, *effort,
        compose_prompt(request.system_prompt, request.prompt),
    ]


class TextCollector:
    """Folds codex's reply into one string.

    Each assistant message arrives whole as one `agent_message` item;
    tool-interleaved turns carry several. Concatenated in stream order they
    are the turn's reply, in the order the agent wrote them.
    """

    def __init__(self) -> None:
        self._texts: list[str] = []

    def observe(self, event: dict) -> None:
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            return
        text = item.get("text")
        if isinstance(text, str) and text:
            self._texts.append(text)

    @property
    def text(self) -> str:
        return "".join(self._texts)


class ToolCallTracker:
    """Folds codex's item stream into ONE normalized record per finished item.

    Work arrives as items keyed by id (`command_execution` with its command
    and output, `file_change` with its paths), announced by `item.started`
    and finished by `item.completed` carrying the result. Only the terminal
    state emits a record: one trace event per real unit of work, with its
    exact args and result. Items carry no timestamps, so spans run from
    first sighting (wall clock) to completion, durations off the clock.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> Optional[dict]:
        if not isinstance(event, dict):
            return None
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        item_id = item.get("id")
        if not item_id or item.get("type") in MESSAGE_ITEM_TYPES:
            return None
        item_id = str(item_id)
        status = str(item.get("status") or "")
        if status not in TERMINAL_ITEM_STATES:
            known = self._open.get(item_id, {})
            self._open[item_id] = {
                "tool": str(item.get("type") or known.get("tool", "")),
                "args": _args_of(item) or known.get("args", {}),
                "started_at": known.get("started_at") or now_iso(),
                "clock": known.get("clock") or time.monotonic(),
            }
            return None
        opened = self._open.pop(item_id, {})
        tool = str(item.get("type") or opened.get("tool") or "tool")
        args = _args_of(item) or opened.get("args") or {}
        record = {
            "tool": tool,
            "tool_call_id": item_id,
            "args": {key: clip_text(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                     for key, value in args.items()},
            "ok": _succeeded(item, status),
            "label": tool_label(tool, args),
        }
        output = _output_text(item)
        if output:
            record["result_snippet"] = clip_text(output, RESULT_SNIPPET_CHARS)
        record["ended_at"] = now_iso()
        if opened.get("clock"):
            record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
        record["started_at"] = opened.get("started_at") or record["ended_at"]
        return record


def _args_of(item: dict) -> dict:
    """The call's identifying args, shaped per item type for the label."""
    if item.get("type") == "command_execution" and isinstance(item.get("command"), str):
        return {"command": item["command"]}
    if item.get("type") == "file_change" and isinstance(item.get("changes"), list):
        changes = [c for c in item["changes"] if isinstance(c, dict)]
        if len(changes) == 1:
            args = {}
            if isinstance(changes[0].get("path"), str):
                args["path"] = changes[0]["path"]
            if isinstance(changes[0].get("kind"), str):
                args["kind"] = changes[0]["kind"]
            return args
        paths = [c["path"] for c in changes if isinstance(c.get("path"), str)]
        if paths:
            return {"path": paths[0] if len(paths) == 1 else f"{len(paths)} files",
                    "paths": paths}
        return {}
    args = item.get("input")
    if isinstance(args, dict):
        return args
    return {key: item[key] for key in ("command", "path", "pattern", "query", "url")
            if isinstance(item.get(key), str)}


def _succeeded(item: dict, status: str) -> bool:
    if status != "completed":
        return False
    code = item.get("exit_code")
    return code in (None, 0)


def _output_text(item: dict) -> str:
    output = item.get("aggregated_output")
    if isinstance(output, str):
        return output
    if item.get("type") == "file_change" and isinstance(item.get("changes"), list):
        lines = [f"{c.get('kind', 'change')} {c.get('path', '')}"
                 for c in item["changes"] if isinstance(c, dict)]
        if lines:
            return "\n".join(lines)
    if isinstance(output, (dict, list)):
        try:
            return json.dumps(output)
        except (TypeError, ValueError):
            return str(output)
    return ""


def _fold_turn_usage(result: PiResult, usage: dict) -> None:
    """Fold one turn.completed into spend; occupancy is the turn's input size.

    `input_tokens` already includes the cached share, so spend is input +
    output while cached tracks the share (reported like reasoning: measured,
    never added).
    """
    if not isinstance(usage, dict):
        usage = {}
    inp = _safe_int(usage.get("input_tokens"))
    out = _safe_int(usage.get("output_tokens"))
    result.usage.input_tokens += inp
    result.usage.output_tokens += out
    result.usage.reasoning_tokens += _safe_int(usage.get("reasoning_output_tokens"))
    result.usage.cache_read_tokens += _safe_int(usage.get("cached_input_tokens"))
    result.usage.cache_write_tokens += _safe_int(usage.get("cache_write_input_tokens"))
    result.usage.total_tokens += inp + out
    result.tokens += inp + out
    result.context_tokens = inp   # the context as it stands after the turn


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _error_message(event: dict) -> str:
    message = event.get("message") if isinstance(event, dict) else None
    if isinstance(message, dict):
        message = message.get("message")
    if isinstance(message, str) and message.lstrip().startswith("{"):
        try:
            data = json.loads(message)
            inner = data.get("error") if isinstance(data, dict) else None
            if isinstance(inner, dict) and inner.get("message"):
                return str(inner["message"])
        except ValueError:
            pass
    if isinstance(event.get("error"), dict) and event["error"].get("message"):
        return _error_message({"message": event["error"]["message"]})
    return str(message or event.get("error") or event)


def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one non-interactive codex turn, tailing JSONL live like agent_pi."""
    cmd = build_command(request, lookup_session_id(request.session_dir,
                                                   request.session_id))

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = PiResult(session_id=request.session_id,
                      context_window=context_window(request.model))
    # stdin is DEVNULL, deliberately — the prompt travels in argv, and an
    # inherited stdin makes exec sit reading piped input that never arrives
    # (observed live: "Reading additional input from stdin..." then nothing).
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=request.cwd,
                               env=operator_env())
    if on_spawn:
        on_spawn(process.pid)
    texts = TextCollector()
    last_error = ""
    with raw_path.open("a") as raw:
        assert process.stdout is not None
        for line in process.stdout:
            raw.write(line)
            raw.flush()                      # events land on disk as they happen
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                record_session_id(request.session_dir, request.session_id,
                                  thread_id)
            if event.get("type") in ("error", "turn.failed"):
                message = _error_message(event)
                if message:
                    last_error = message
            if event.get("type") == "turn.completed":
                _fold_turn_usage(result, event.get("usage"))
            texts.observe(event)
            if on_event:
                on_event(event)
    stderr = process.stderr.read() if process.stderr else ""
    result.returncode = process.wait()
    if on_exit:
        on_exit(process.pid)
    result.text = texts.text
    if result.returncode != 0 and not result.text:
        detail = last_error or stderr.strip()
        raise RuntimeError(f"codex exited {result.returncode}: {detail[-2000:]}")
    return result
