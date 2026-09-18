"""OMP coding-agent interface.

Runs `omp -p --mode json` and tails its JSONL stdout line by line, forwarding
each event to a callback WHILE the agent works. OMP speaks the pi protocol
family: assistant turns arrive as `message_end` events carrying content blocks
and a pi-shaped `usage` object, and tool calls announce as `toolCall` blocks
followed by `tool_execution_start/_update/_end` — so the run loop, usage
folding, and tool tracking mirror agent_pi, with OMP's argv and session flow.

Sessions continue by server-minted id: the first `session` event's id is
captured and persisted in the factory-id -> omp-id mapping beside the agent's
session dir. First send runs plain `omp -p`; later sends add `-r <id>` with
the same `--session-dir`, `--model`, thinking, tools, and flags (verified:
resumes stream only new events with context intact, and accept the full flag
set). `--session-dir` namespaces factory sessions; the spawn cwd carries the
worktree, and the system prompt travels on `--system-prompt` directly.

Three deliberate choices, documented because they differ from pi:

* No `--session-id` create-with-id: the server mints the id, so the mapping
  file (not the flag) is what makes "same id in, same conversation back" true
  across processes and `--adw-id` joins.
* Thinking passes through OMP's own ladder (`off | minimal | low | medium |
  high | xhigh | max`, plus `auto`), which already matches pi's — unknown
  values fall back to `medium`, never passthrough.
* Headless runs never block on approvals: the factory relies on the
  operator's configured approval policy being non-interactive-safe, and the
  repo boundary stays enforced post-hoc by permissions.py either way.
* Tool names are translated (`tools_for`): OMP rejects unknown `--tools`
  values outright and speaks a smaller vocabulary (`find` -> `glob`, no `ls`
  — bash covers it). Unmapped names are dropped, never passed through, so a
  pi-vocabulary roster cannot fail an OMP run.

Model ids resolve against `omp models --json` (provider/id selectors, pi's
ambiguity errors); usage and cost fold turn by turn from the stream, and the
context ceiling comes from the catalog's `contextWindow` (0 when absent).

Auth is the operator's: OMP profiles, subscription logins, or provider keys —
the factory passes none of it and never sees it.

Tested against the installed OMP CLI (`-p --mode json`, resume, session-dir,
thinking, tools, system-prompt) with a local ollama model.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from .data_types import PiRequest, PiResult, UsageBreakdown
from .harness import ARG_VALUE_CHARS, RESULT_SNIPPET_CHARS, clip_text, tool_label
from .utils import now_iso, operator_env

BINARY = ("OMP_PATH", "omp")
IMPLEMENTED = True

OMP_PATH = os.environ.get("OMP_PATH", "omp")
SESSION_MAP_FILENAME = "omp_sessions.json"

# Pi tool vocabulary -> OMP's. OMP rejects unknown --tools values outright
# (the whole run fails), and its list differs: no `ls` or `find`, but a
# `glob` that covers finding. Names with no OMP equivalent are dropped — bash
# still covers the capability — and the repo boundary stays enforced
# post-hoc by permissions.py either way, so a dropped name never widens what
# an agent may change, only which dedicated tool it gets.
TOOLS_MAP = {
    "read": "read",
    "bash": "bash",
    "edit": "edit",
    "write": "write",
    "grep": "grep",
    "find": "glob",
    "glob": "glob",
}


def _count(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@lru_cache(maxsize=1)
def _omp_catalog() -> list[tuple[str, str, int]]:
    """(provider, id, contextWindow) rows from `omp models --json`.

    Cached per process like pi's catalog: one CLI call per ADW process, and a
    failing CLI reads as an empty catalog so resolve_model can say so clearly.
    """
    try:
        result = subprocess.run(
            [OMP_PATH, "models", "--json"], capture_output=True, text=True,
            timeout=60, env=operator_env(), check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return []
    models = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(models, list):
        return []
    rows = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "")
        model_id = str(entry.get("id") or "")
        if provider and model_id:
            rows.append((provider, model_id, _count(entry.get("contextWindow"))))
    return rows


def resolve_model(pattern: str) -> str:
    """Resolve a model pattern to the `provider/id` selector OMP's --model
    takes. Same contract as pi: qualified patterns match exactly, bare ids
    match uniquely, anything else raises naming the candidates."""
    pattern = (pattern or "").strip()
    if not pattern:
        raise ValueError("omp agent needs a model id — set model: in sssf.config.yaml")
    catalog = [(provider, model_id) for provider, model_id, _ in _omp_catalog()]
    if not catalog:
        raise ValueError("omp model catalog is empty — is `omp models` working?")
    if "/" in pattern:
        provider, model_id = pattern.split("/", 1)
        if (provider, model_id) in catalog:
            return f"{provider}/{model_id}"
        raise ValueError(f"model pattern {pattern!r} not found in omp models — "
                         "fix the config")
    matches = [(provider, model_id) for provider, model_id in catalog
               if pattern == model_id or pattern in model_id]
    exact = [match for match in matches
             if match[1] == pattern or match[1].endswith("/" + pattern)]
    if len(exact) == 1:
        return f"{exact[0][0]}/{exact[0][1]}"
    if len(matches) == 1:
        return f"{matches[0][0]}/{matches[0][1]}"
    if not matches:
        raise ValueError(f"model pattern {pattern!r} not found in omp models — "
                         "fix the config")
    raise ValueError(f"model pattern {pattern!r} is ambiguous: {matches}")


def context_window(model: str) -> int:
    """The model's context ceiling from omp's catalog. 0 reads as unknown."""
    selector = (model or "").strip()
    provider, _, model_id = selector.partition("/")
    for listed_provider, listed_model, window in _omp_catalog():
        if model_id and listed_provider == provider and listed_model == model_id:
            return window
        if not model_id and listed_model == selector:
            return window
    return 0


def thinking_for(thinking: str) -> str:
    """OMP's ladder already matches pi's — pass through, medium on unknown."""
    thinking = (thinking or "").strip()
    if thinking in ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"):
        return thinking
    return "medium"


def lookup_session_id(session_dir: str, our_session_id: str) -> Optional[str]:
    """The omp session id for this factory session, if one was captured on an
    earlier send. None = first send, run plain `omp -p` and capture the id off
    the opening `session` event."""
    mapping = _read_map(session_dir)
    found = mapping.get(our_session_id)
    return found if isinstance(found, str) and found else None


def record_session_id(session_dir: str, our_session_id: str, harness_id: str) -> str:
    """Persist a captured session id. First write wins — every stream opens
    with the same id, including resumes."""
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


def tools_for(tools: list[str] | None) -> list[str]:
    """Translate a roster tools list into OMP's vocabulary.

    Returns the mapped names, deduplicated in first-seen order. Unknown names
    (pi-only tools like `ls`, another harness's extension tools) are dropped:
    OMP fails the entire run on an unknown --tools value, and bash still
    covers the lost capabilities. None/empty in means no flag out.
    """
    if not tools:
        return []
    seen: list[str] = []
    for name in tools:
        mapped = TOOLS_MAP.get(name)
        if mapped and mapped not in seen:
            seen.append(mapped)
    return seen


def build_command(request: PiRequest, harness_id: Optional[str]) -> list[str]:
    """Argv for one headless omp turn. Pure — unit-tested without spawning.

    Fresh and resume sends take the same flags (verified live): the model,
    thinking, tools, session dir, and working root all ride every send, and
    only the resume adds `-r <id>`.
    """
    cmd = [
        OMP_PATH, "-p",
        "--mode", "json",
        "--model", request.model,
        "--thinking", thinking_for(request.thinking),
        "--system-prompt", request.system_prompt,
        "--session-dir", request.session_dir,
        "--cwd", request.cwd,
    ]
    if request.tools:
        mapped = tools_for(request.tools)
        if mapped:
            cmd += ["--tools", ",".join(mapped)]
        # else: every name was unmapped — omit the flag (OMP default is all
        # tools) rather than fail the run; permissions.py still bounds writes.
    for extension in request.extensions:
        cmd += ["-e", extension]
    if harness_id:
        cmd += ["-r", harness_id]
    cmd.append(request.prompt)
    return cmd


def _text_of(container: dict) -> str:
    """Join the text blocks of anything shaped as {content: [...]} — a message
    or a tool result."""
    return "".join(part.get("text", "") for part in container.get("content", []) or []
                   if isinstance(part, dict) and part.get("type") == "text")


def _context_tokens(usage: dict) -> int:
    """Tokens occupying the window after a turn: the provider's totalTokens,
    else the sum of the parts. Cache reads count — cached prompt is prompt."""
    total = usage.get("totalTokens") or 0
    if total:
        return int(total)
    return int(sum(usage.get(part) or 0
                   for part in ("input", "output", "cacheRead", "cacheWrite")))


class ToolCallTracker:
    """Folds OMP's tool stream into ONE normalized record per completed call.

    Same shapes as pi: a `toolCall` content block announces the call, then
    `tool_execution_start/_update/_end` bracket it keyed by toolCallId. Only
    the end carries the result, so that is where a record is emitted — one
    trace event per real tool call, the moment it returns, with its real span.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> Optional[dict]:
        """Returns the record for a finished tool call, else None."""
        if not isinstance(event, dict):
            return None
        etype = event.get("type", "")
        if etype == "message_end":
            message = event.get("message")
            blocks = message.get("content", []) if isinstance(message, dict) else []
            for block in blocks or []:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    self._announce(block.get("id"), block.get("name"),
                                   block.get("arguments"))
            return None
        if etype == "tool_execution_start":
            self._announce(event.get("toolCallId"), event.get("toolName"),
                           event.get("args"))
            return None
        if etype != "tool_execution_end":
            return None

        call_id = str(event.get("toolCallId") or "")
        opened = self._open.pop(call_id, {})
        tool = str(event.get("toolName") or opened.get("tool") or "tool")
        args = event.get("args") or opened.get("args") or {}
        record = {
            "tool": tool,
            "tool_call_id": call_id,
            "args": {key: clip_text(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                     for key, value in args.items()},
            "ok": not event.get("isError", False),
            "label": tool_label(tool, args),
        }
        result_text = _text_of(event.get("result") or {})
        if result_text:
            record["result_snippet"] = clip_text(result_text, RESULT_SNIPPET_CHARS)
        record["ended_at"] = now_iso()
        if opened.get("clock"):
            record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
        if opened.get("started_at"):
            record["started_at"] = opened["started_at"]
        return record

    def _announce(self, call_id, tool, args) -> None:
        """First sighting starts the clock; a later sighting only fills gaps."""
        if not call_id:
            return
        known = self._open.get(str(call_id), {})
        self._open[str(call_id)] = {
            "tool": tool or known.get("tool", ""),
            "args": args or known.get("args", {}),
            "started_at": known.get("started_at") or now_iso(),
            "clock": known.get("clock") or time.monotonic(),
        }


def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one non-interactive omp turn, tailing JSONL live like agent_pi."""
    cmd = build_command(request, lookup_session_id(request.session_dir,
                                                   request.session_id))

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = PiResult(session_id=request.session_id,
                      context_window=context_window(request.model))
    # stdin is DEVNULL, deliberately. The prompt travels in argv, so the child
    # never needs stdin — an inherited stdin risks the child waiting on piped
    # input that never arrives while the ADW blocks on an empty read loop.
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=request.cwd,
                               env=operator_env())
    if on_spawn:
        on_spawn(process.pid)
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
            if event.get("type") == "session":
                session_id = event.get("id")
                if isinstance(session_id, str) and session_id:
                    record_session_id(request.session_dir, request.session_id,
                                      session_id)
            if event.get("type") == "message_end":
                message = event.get("message", {})
                if message.get("role") == "assistant":
                    text = _text_of(message)
                    if text:
                        result.text = text   # last assistant message wins
                    usage = message.get("usage", {}) or {}
                    turn = _context_tokens(usage)
                    result.tokens += turn
                    result.usage.add_turn(usage, turn)
                    # Occupancy is read off the last VALID assistant turn — an
                    # aborted or errored turn reports usage you can't trust.
                    if turn and message.get("stopReason") not in ("aborted", "error"):
                        result.context_tokens = turn
                    result.cost += (usage.get("cost", {}) or {}).get("total", 0.0) or 0.0
            if on_event:
                on_event(event)
    stderr = process.stderr.read() if process.stderr else ""
    result.returncode = process.wait()
    if on_exit:
        on_exit(process.pid)
    if result.returncode != 0 and not result.text:
        detail = stderr.strip()
        raise RuntimeError(f"omp exited {result.returncode}: {detail[-2000:]}")
    return result
