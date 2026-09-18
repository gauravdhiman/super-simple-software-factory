"""OpenCode coding-agent interface.

Runs `opencode run --format json` and tails its NDJSON stdout line by line,
forwarding each event to a callback WHILE the agent works. Every event carries
the server-minted session id (`ses_...`), so the adapter captures it off the
first event and persists the factory-id -> session-id mapping beside the
agent's session dir — first send omits `--session`, every later send resumes
it. Resumes stream only new events with context intact (verified live).

Three deliberate fallbacks, documented because they differ from pi:

* No system-prompt flag: the system text is composed ahead of the user text
  with a separator. Identity and output contract survive; only the channel
  changes.
* No tools allowlist: the agent runs as opencode's `build` agent with `--auto`
  (asks auto-approved, explicit denies still hold — approvals never block a
  headless run). The repo boundary stays enforced post-hoc by permissions.py
  either way.
* Thinking is provider-specific: the Pi ladder passes straight through
  `--variant`, except `off` which drops the flag and keeps the provider
  default. Unknown values are ignored by the CLI, never fail.

Usage and cost ride the stream: every `step_finish` carries that step's tokens
({input, output, reasoning, cache:{read, write}}) and cost. `total` is window
occupancy, not spend — spend per step is input + output + cache (reasoning is
the thinking share of output, never added), exactly pi's accounting.

Auth is the operator's: `opencode auth login`, provider keys, subscription or
keys — the factory never sees them. Model ids pass through opaquely
(`provider/model`, see `opencode models`); opencode itself reports an unknown
id clearly at spawn time.

Tested against opencode 1.18.30.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .data_types import PiRequest, PiResult
from .harness import ARG_VALUE_CHARS, RESULT_SNIPPET_CHARS, clip_text, tool_label
from .utils import now_iso, operator_env

BINARY = ("OPENCODE_PATH", "opencode")
IMPLEMENTED = True

OPENCODE_PATH = os.environ.get("OPENCODE_PATH", "opencode")
SESSION_MAP_FILENAME = "opencode_sessions.json"

SYSTEM_SEPARATOR = "\n\n---\n\n"

# opencode tool states that end a call. Anything else (pending, running) only
# stages the span — the record is emitted once, at the terminal state.
TERMINAL_TOOL_STATES = ("completed", "error")


def resolve_model(pattern: str) -> str:
    """OpenCode model ids are opaque to the factory — non-empty passes, and
    opencode itself reports an unknown id clearly at spawn time."""
    model = (pattern or "").strip()
    if not model:
        raise ValueError("opencode agent needs a model id (provider/model) — "
                         "set model: in sssf.config.yaml (see `opencode models`)")
    return model


def context_window(model: str) -> int:
    """OpenCode publishes no queryable ceiling outside operator config — 0
    reads as unknown in the UI."""
    return 0


def variant_for(thinking: str) -> Optional[str]:
    """Pi thinking ladder -> opencode --variant, verbatim.

    Variants are provider-specific and the CLI ignores unknown values (exit 0,
    verified live), so every rung passes through as-is — except `off`, which
    drops the flag so OpenCode keeps its provider default.
    """
    thinking = (thinking or "").strip()
    if not thinking or thinking == "off":
        return None
    return thinking


def compose_prompt(system_prompt: str, user_prompt: str) -> str:
    """System text ahead of user text — the fallback for no --system flag."""
    if system_prompt.strip():
        return f"{system_prompt.strip()}{SYSTEM_SEPARATOR}{user_prompt}"
    return user_prompt


def lookup_session_id(session_dir: str, our_session_id: str) -> Optional[str]:
    """The opencode `ses_...` id for this factory session, if one was captured
    on an earlier send. None = first send, omit --session and let the server
    mint one."""
    mapping = _read_map(session_dir)
    found = mapping.get(our_session_id)
    return found if isinstance(found, str) and found else None


def record_session_id(session_dir: str, our_session_id: str, harness_id: str) -> str:
    """Persist a captured session id. First write wins within a process pair —
    every event in a run carries the same id, so re-writes are no-ops."""
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
    """Argv for one headless opencode turn. Pure — unit-tested without spawning."""
    cmd = [
        OPENCODE_PATH, "run",
        "--format", "json",
        "-m", request.model,
        "--agent", "build",
        "--auto",
        "--dir", request.cwd,
    ]
    variant = variant_for(request.thinking)
    if variant is not None:
        cmd += ["--variant", variant]
    if harness_id:
        cmd += ["--session", harness_id]
    cmd.append(compose_prompt(request.system_prompt, request.prompt))
    return cmd


def _iso_ms(value) -> Optional[str]:
    """Epoch millis -> ISO instant. None when the value is not a number."""
    try:
        moment = float(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(moment / 1000, tz=timezone.utc).isoformat(
        timespec="milliseconds")


class TextCollector:
    """Folds opencode's text parts into the turn's reply.

    The stream emits each text part whole (one event per part in practice);
    providers that re-emit a part as it streams send growing snapshots of the
    same part id. Latest snapshot per part id, concatenated in first-seen
    order, is the reply under both behaviors.
    """

    def __init__(self) -> None:
        self._parts: dict[str, str] = {}
        self._order: list[str] = []

    def observe(self, event: dict) -> None:
        part = event.get("part") if isinstance(event, dict) else None
        if not isinstance(part, dict) or part.get("type") != "text":
            return
        part_id = part.get("id")
        text = part.get("text")
        if not isinstance(part_id, str) or not isinstance(text, str):
            return
        if part_id not in self._parts:
            self._order.append(part_id)
        self._parts[part_id] = text

    @property
    def text(self) -> str:
        return "".join(self._parts[part_id] for part_id in self._order)


class ToolCallTracker:
    """Folds opencode's tool stream into ONE normalized record per finished call.

    Each call arrives as a `tool_use` event keyed by callID, with its args in
    `state.input` and — at the terminal state — its result in `state.output`
    plus the real span in `state.time`. Only the terminal state emits a
    record: one trace event per real tool call, with its exact args, result,
    and span.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> Optional[dict]:
        if not isinstance(event, dict):
            return None
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != "tool":
            return None
        call_id = part.get("callID")
        if not call_id:
            return None
        call_id = str(call_id)
        state = part.get("state") or {}
        if not isinstance(state, dict):
            state = {}
        status = str(state.get("status") or "")
        if status not in TERMINAL_TOOL_STATES:
            known = self._open.get(call_id, {})
            self._open[call_id] = {
                "tool": part.get("tool") or known.get("tool", ""),
                "input": state.get("input") if isinstance(state.get("input"), dict)
                else known.get("input", {}),
                "started_ms": _span_ms(state, "start") or known.get("started_ms"),
            }
            return None
        opened = self._open.pop(call_id, {})
        tool = str(part.get("tool") or opened.get("tool") or "tool")
        args = state.get("input") if isinstance(state.get("input"), dict) \
            else opened.get("input") or {}
        record = {
            "tool": tool,
            "tool_call_id": call_id,
            "args": {key: clip_text(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                     for key, value in args.items()},
            "ok": _succeeded(state, status),
            "label": tool_label(tool, args),
        }
        output = _output_text(state)
        if output:
            record["result_snippet"] = clip_text(output, RESULT_SNIPPET_CHARS)
        started_ms = _span_ms(state, "start") or opened.get("started_ms")
        ended_ms = _span_ms(state, "end")
        if started_ms is not None:
            record["started_at"] = _iso_ms(started_ms)
        else:
            record["started_at"] = now_iso()
        if ended_ms is not None:
            record["ended_at"] = _iso_ms(ended_ms)
            if started_ms is not None:
                record["duration_ms"] = max(0, int(ended_ms - started_ms))
        else:
            record["ended_at"] = now_iso()
        return record


def _span_ms(state: dict, which: str):
    """The tool's real span, epoch millis — opencode hands it to us."""
    time = state.get("time") or {}
    value = time.get(which) if isinstance(time, dict) else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _succeeded(state: dict, status: str) -> bool:
    if status != "completed":
        return False
    metadata = state.get("metadata") or {}
    code = metadata.get("exit") if isinstance(metadata, dict) else None
    return code in (None, 0)


def _output_text(state: dict) -> str:
    output = state.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, (dict, list)):
        try:
            return json.dumps(output)
        except (TypeError, ValueError):
            return str(output)
    return ""


def _fold_step_usage(result: PiResult, part: dict) -> None:
    """Fold one step_finish into spend; window occupancy is the last total."""
    tokens = part.get("tokens") or {}
    if not isinstance(tokens, dict):
        tokens = {}
    cache = tokens.get("cache") or {}
    if not isinstance(cache, dict):
        cache = {}
    try:
        cost = float(part.get("cost") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    try:
        total = int(tokens.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    try:
        spend = (int(tokens.get("input") or 0) + int(tokens.get("output") or 0)
                 + int(cache.get("read") or 0) + int(cache.get("write") or 0))
    except (TypeError, ValueError):
        spend = 0
    result.usage.input_tokens += _safe_int(tokens.get("input"))
    result.usage.output_tokens += _safe_int(tokens.get("output"))
    result.usage.reasoning_tokens += _safe_int(tokens.get("reasoning"))
    result.usage.cache_read_tokens += _safe_int(cache.get("read"))
    result.usage.cache_write_tokens += _safe_int(cache.get("write"))
    result.usage.total_tokens += spend
    result.tokens += spend
    result.usage.total_cost += cost
    result.cost += cost
    result.context_tokens = total or spend   # occupancy: the window as it stands


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _error_message(event: dict) -> str:
    error = event.get("error") or {}
    if not isinstance(error, dict):
        return str(error)
    data = error.get("data")
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])
    return str(error.get("message") or error)


def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one non-interactive opencode turn, tailing NDJSON live like agent_pi."""
    cmd = build_command(request, lookup_session_id(request.session_dir,
                                                   request.session_id))

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = PiResult(session_id=request.session_id,
                      context_window=context_window(request.model))
    # stdin is DEVNULL, deliberately — the prompt travels in argv, so the
    # child never needs stdin, and inheriting the parent's risks a silent sit
    # on a non-TTY read (the failure agent_pi documents at length).
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
            harness_id = event.get("sessionID")
            if isinstance(harness_id, str) and harness_id:
                record_session_id(request.session_dir, request.session_id,
                                  harness_id)
            if event.get("type") == "error":
                message = _error_message(event)
                if message:
                    last_error = message
            part = event.get("part")
            if isinstance(part, dict) and event.get("type") == "step_finish":
                _fold_step_usage(result, part)
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
        raise RuntimeError(f"opencode exited {result.returncode}: {detail[-2000:]}")
    return result
