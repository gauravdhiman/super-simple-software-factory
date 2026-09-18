"""Claude Code coding-agent interface.

Runs `claude -p --verbose --output-format stream-json` and tails its JSONL
stdout line by line, forwarding each event to a callback WHILE the agent
works. Every event is an envelope (`type` + optional `subtype`); the opening
`system/init` event carries the server-minted `session_id`, which the adapter
captures into the factory-id -> claude-id mapping beside the agent's session
dir. First send runs plain `claude -p`; later sends add
`--resume <session-id>` with the stored id.

Four deliberate choices, documented because they differ from pi:

* Prompt travels as argv, never stdin: a piped-stdin probe completed
  instantly with a synthetic empty turn (verified), so stdin is DEVNULL and
  the prompt is the final argv, like pi.
* `--verbose` is mandatory: without it, `-p --output-format=stream-json`
  exits 1 refusing to start (verified).
* Tool names are translated (`tools_for`): Claude Code capitalizes its tools
  (`Bash`, `Read`, `Edit`...) and fails unknown `--allowedTools` values, so
  pi-vocabulary names map (`bash` -> `Bash`, `find` -> `Glob`) and unmapped
  ones drop instead of failing the run. Extension/subagent tools from other
  harnesses drop too — `harness_engineering` .ts extensions are pi-only. The
  repo boundary stays enforced post-hoc by permissions.py either way.
* Headless runs use `--permission-mode bypassPermissions`: prompts must never
  block automation. This disables Claude Code's own gating, so permissions.py
  is the enforcement boundary, not a backstop.

Thinking maps onto `--effort` (`low | medium | high | xhigh | max`); `off`
and `minimal` floor to `low` (there is no quieter rung) and unknown values
fall back to `medium`, never passthrough.

Usage and cost are real: the terminal `result` event carries `usage`
(input/output/cache tokens) and `total_cost_usd`, folded into the run. Final
text is the last assistant `text` block, with the result event's text as the
fallback. Tool calls fold from `tool_use` blocks (announce) and `tool_result`
blocks (emit one record).

Auth is the operator's: `claude login`, subscription or `ANTHROPIC_API_KEY` —
the factory passes none of it and never sees it. Model ids pass through
opaquely (non-empty is the only check); claude names its own models
(`sonnet`, `opus`, `haiku`, full ids) and reports an unknown id at spawn.

NOTE — live-verification status: this adapter was built against the real CLI
(mapped envelope, session, init, error, and result shapes from captured
output on 2.1.277) plus the documented stream-json shapes for tool turns,
with full unit tests. No live model turn has run through it yet — this
machine's Claude OAuth is expired. Before trusting it on real work, run one
`adw_prompt` with a claude worker (see update_config.md) and confirm green.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .data_types import PiRequest, PiResult, UsageBreakdown
from .harness import ARG_VALUE_CHARS, RESULT_SNIPPET_CHARS, clip_text, tool_label
from .utils import now_iso, operator_env

BINARY = ("CLAUDE_PATH", "claude")
IMPLEMENTED = True

CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")
SESSION_MAP_FILENAME = "claude_sessions.json"

# Pi tool vocabulary -> Claude Code's. Unknown --allowedTools values fail the
# run, and Claude capitalizes its tools; names with no equivalent drop (Read
# covers listing, Bash covers the rest) rather than failing the run.
TOOLS_MAP = {
    "read": "Read",
    "bash": "Bash",
    "edit": "Edit",
    "write": "Write",
    "grep": "Grep",
    "find": "Glob",
    "glob": "Glob",
}

# Pi thinking ladder -> claude --effort. No quieter rung than low exists, so
# off/minimal floor to it; unknown values settle on medium, never passthrough.
EFFORT = {
    "off": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


def resolve_model(pattern: str) -> str:
    """Claude model ids are opaque to the factory — non-empty passes, and
    claude itself reports an unknown id clearly at spawn time."""
    model = (pattern or "").strip()
    if not model:
        raise ValueError("claude_code agent needs a model id — "
                         "set model: in sssf.config.yaml")
    return model


def context_window(model: str) -> int:
    """Claude publishes no queryable ceiling here — 0 reads as unknown."""
    return 0


def effort_for(thinking: str) -> str:
    """Pi thinking ladder -> claude effort. Always a claude-vocabulary value."""
    return EFFORT.get((thinking or "").strip(), "medium")


def tools_for(tools: list[str] | None) -> list[str]:
    """Translate a roster tools list into Claude Code's vocabulary.

    Mapped names pass through deduplicated in first-seen order; anything else
    (pi-only tools, another harness's extension tools) drops — an unknown
    --allowedTools value fails the whole run. None/empty in means no flag out.
    """
    if not tools:
        return []
    seen: list[str] = []
    for name in tools:
        mapped = TOOLS_MAP.get(name)
        if mapped and mapped not in seen:
            seen.append(mapped)
    return seen


def lookup_session_id(session_dir: str, our_session_id: str) -> Optional[str]:
    """The claude session id for this factory session, if one was captured on
    an earlier send. None = first send, run plain `claude -p` and capture the
    id off the opening `system/init` event."""
    mapping = _read_map(session_dir)
    found = mapping.get(our_session_id)
    return found if isinstance(found, str) and found else None


def record_session_id(session_dir: str, our_session_id: str, harness_id: str) -> str:
    """Persist a captured session id. First write wins — every stream carries
    the same id, including resumes."""
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
    """Argv for one headless claude turn. Pure — unit-tested without spawning.

    The prompt travels last as argv (stdin piping produces empty synthetic
    turns); resumes add only `--resume <id>`, everything else rides every
    send. `--verbose` is mandatory for stream-json under `-p`.
    """
    cmd = [
        CLAUDE_PATH, "-p",
        "--verbose",
        "--output-format", "stream-json",
        "--permission-mode", "bypassPermissions",
        "--model", request.model,
        "--effort", effort_for(request.thinking),
    ]
    if request.system_prompt.strip():
        cmd += ["--system-prompt", request.system_prompt]
    mapped = tools_for(request.tools)
    if mapped:
        # else: every name was unmapped — omit the flag (claude default is all
        # tools) rather than fail the run; permissions.py still bounds writes.
        cmd += ["--allowedTools", ",".join(mapped)]
    if harness_id:
        cmd += ["--resume", harness_id]
    cmd.append(request.prompt)
    return cmd


def _text_of(blocks) -> str:
    """Join the text blocks of an assistant message's content list."""
    return "".join(part.get("text", "") for part in blocks or []
                   if isinstance(part, dict) and part.get("type") == "text")


class ToolCallTracker:
    """Folds claude's turn stream into ONE normalized record per tool call.

    An assistant `tool_use` block announces the call (id, name, input); the
    matching `tool_result` block on a later user message finishes it. Only
    the result emits a record: one trace event per real tool call, with its
    exact args and result. Spans run from first sighting (wall clock) to the
    result, durations off the clock.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> Optional[dict]:
        if not isinstance(event, dict):
            return None
        etype = event.get("type", "")
        message = event.get("message")
        blocks = message.get("content", []) if isinstance(message, dict) else []
        if etype not in ("assistant", "user") or not isinstance(blocks, list):
            return None
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if etype == "assistant" and block.get("type") == "tool_use":
                self._stage(str(block.get("id") or ""),
                            str(block.get("name") or ""),
                            block.get("input"))
            elif etype == "user" and block.get("type") == "tool_result":
                record = self._finish(str(block.get("tool_use_id") or ""), block)
                if record is not None:
                    return record
        return None

    def _stage(self, call_id: str, tool: str, args) -> None:
        if not call_id:
            return
        known = self._open.get(call_id, {})
        self._open[call_id] = {
            "tool": tool or known.get("tool", ""),
            "args": args if isinstance(args, dict) else known.get("args", {}),
            "started_at": known.get("started_at") or now_iso(),
            "clock": known.get("clock") or time.monotonic(),
        }

    def _finish(self, call_id: str, block: dict) -> Optional[dict]:
        if not call_id:
            return None
        opened = self._open.pop(call_id, {})
        tool = opened.get("tool") or "tool"
        args = opened.get("args") or {}
        record = {
            "tool": tool,
            "tool_call_id": call_id,
            "args": {key: clip_text(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                     for key, value in args.items()},
            "ok": not block.get("is_error", False),
            "label": tool_label(tool, args),
        }
        content = block.get("content")
        result_text = content if isinstance(content, str) else _text_of(
            [content] if isinstance(content, dict) else content)
        if result_text:
            record["result_snippet"] = clip_text(result_text, RESULT_SNIPPET_CHARS)
        record["ended_at"] = now_iso()
        if opened.get("clock"):
            record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
        record["started_at"] = opened.get("started_at") or record["ended_at"]
        return record


def _fold_result_usage(result: PiResult, event: dict) -> None:
    """Fold the terminal result event's usage and cost into the run.

    Claude's usage is flat input/output/cache counters plus thinking inside
    output_tokens_details; cost rides total_cost_usd as real dollars.
    """
    usage = event.get("usage") if isinstance(event, dict) else None
    if not isinstance(usage, dict):
        return
    breakdown = UsageBreakdown(
        input_tokens=_safe_int(usage.get("input_tokens")),
        output_tokens=_safe_int(usage.get("output_tokens")),
        cache_read_tokens=_safe_int(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_safe_int(usage.get("cache_creation_input_tokens")),
        total_tokens=(_safe_int(usage.get("input_tokens"))
                      + _safe_int(usage.get("output_tokens"))),
    )
    result.usage.merge(breakdown)
    result.tokens += breakdown.total_tokens
    result.context_tokens = breakdown.total_tokens or result.context_tokens
    try:
        result.cost += float(event.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        pass


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _error_message(event: dict) -> str:
    """The human-readable failure from an error result or stderr-shaped text."""
    for key in ("result", "error", "message"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    errors = event.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(e) for e in errors)
    return ""


def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one non-interactive claude turn, tailing stream-json live."""
    cmd = build_command(request, lookup_session_id(request.session_dir,
                                                   request.session_id))

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = PiResult(session_id=request.session_id,
                      context_window=context_window(request.model))
    # stdin is DEVNULL, deliberately. The prompt travels as argv — a piped
    # stdin produced an instant synthetic empty turn (verified), and an
    # inherited stdin risks the child waiting on input that never arrives.
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
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                record_session_id(request.session_dir, request.session_id,
                                  session_id)
            if event.get("type") == "assistant":
                message = event.get("message", {})
                text = _text_of(message.get("content") if isinstance(message, dict) else [])
                if text:
                    result.text = text       # last assistant message wins
            if event.get("type") == "result":
                _fold_result_usage(result, event)
                if event.get("is_error"):
                    result.returncode = 1
                elif isinstance(event.get("result"), str) and event["result"].strip() \
                        and not result.text:
                    result.text = event["result"]   # terminal text as fallback
            if on_event:
                on_event(event)
    stderr = process.stderr.read() if process.stderr else ""
    result.returncode = process.wait() or result.returncode
    if on_exit:
        on_exit(process.pid)
    if result.returncode != 0 and not result.text:
        detail = _error_message({"message": stderr.strip()})
        raise RuntimeError(f"claude exited {result.returncode}: {detail[-2000:]}")
    return result
