"""Muse coding-agent interface.

Runs `muse exec --json` and tails its JSONL stdout line by line, forwarding
each event to a callback WHILE the agent works. Sessions continue by UUID, so
the adapter maps the factory's session id to a UUID persisted beside the
agent's session dir — same id in, same conversation back, across processes.

Muse reports no usage or cost on its stream: tokens and dollars read zero,
honestly. The trace still records every phase, event, envelope, and gate.

Two deliberate fallbacks, both documented because they differ from pi:

* No system-prompt flag: the system text is composed ahead of the user text
  with a separator. Identity and output contract survive; only the channel
  changes.
* No tools allowlist: the agent gets its workspace permissions
  (--trust-workspace, approvals never in headless runs). The repo boundary
  stays enforced post-hoc by permissions.py either way.

Auth is the operator's: `muse login` or provider credentials, subscription or
keys — the factory never sees them.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .data_types import PiRequest, PiResult, UsageBreakdown
from .harness import clip_text, tool_label
from .utils import now_iso, operator_env

BINARY = ("MUSE_PATH", "muse")
IMPLEMENTED = True

MUSE_PATH = os.environ.get("MUSE_PATH", "muse")
SESSION_MAP_FILENAME = "muse_sessions.json"

# Pi thinking ladder -> Muse reasoning effort. Names mostly coincide; pi's
# "off" is muse's "none". Unknown values fall back to medium, never fail.
THINKING = {
    "off": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

SYSTEM_SEPARATOR = "\n\n---\n\n"


def resolve_model(pattern: str) -> str:
    """Muse model ids are opaque to the factory — non-empty passes, and muse
    itself reports an unknown id clearly at spawn time."""
    model = (pattern or "").strip()
    if not model:
        raise ValueError("muse agent needs a model id — set model: in sssf.config.yaml")
    return model


def context_window(model: str) -> int:
    """Muse publishes no queryable ceiling — 0 reads as unknown in the UI."""
    return 0


def harness_session_id(session_dir: str, our_session_id: str) -> str:
    """Stable UUID per factory session id, persisted beside the agent's
    session dir. Muse demands UUIDs; the factory mints readable ids — the map
    is what lets both be true across processes and joins."""
    path = Path(session_dir).parent / SESSION_MAP_FILENAME
    mapping: dict = {}
    if path.is_file():
        try:
            mapping = json.loads(path.read_text())
        except ValueError:
            mapping = {}
    if not isinstance(mapping.get(our_session_id), str):
        mapping[our_session_id] = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mapping, indent=2))
    return mapping[our_session_id]


def compose_prompt(system_prompt: str, user_prompt: str) -> str:
    """System text ahead of user text — the fallback for no --system-prompt."""
    if system_prompt.strip():
        return f"{system_prompt.strip()}{SYSTEM_SEPARATOR}{user_prompt}"
    return user_prompt


def build_command(request: PiRequest, harness_id: str) -> list[str]:
    """Argv for one headless muse turn. Pure — unit-tested without spawning."""
    return [
        MUSE_PATH, "exec",
        "--json",
        "--model", request.model,
        "--reasoning-effort", THINKING.get(request.thinking, "medium"),
        "--session-id", harness_id,
        "--trust-workspace",
        "--approval-mode", "never",
        "--workspace", request.cwd,
        compose_prompt(request.system_prompt, request.prompt),
    ]


class ToolCallTracker:
    """Folds muse's task stream into ONE normalized record per finished task.

    Muse announces work as task lifecycles (proposed -> accepted -> scheduled
    -> started -> completed) keyed by task_id, with the operation naming what
    ran. Only completion emits a record — one trace event per real unit of
    work, with its span. Argument shapes vary by operation and are still being
    mapped; the record carries the operation and whatever args arrived.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> Optional[dict]:
        if not isinstance(event, dict):
            return None
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        task = payload.get("task_id") or (payload.get("task_stream") or {}).get("id")
        operation = _operation_of(payload)
        if event.get("payload_type") == "task.lifecycle.status":
            # Heartbeats carry no state change worth tracing.
            return None
        if not task:
            return None
        task_id = str(task)
        if _is_end(payload):
            opened = self._open.pop(task_id, {})
            tool = str(operation or opened.get("tool") or "task")
            record = {
                "tool": tool,
                "tool_call_id": task_id,
                "args": opened.get("args") or {},
                "ok": not _is_error(payload),
                "label": tool_label(tool, opened.get("args") or {}),
            }
            if opened.get("started_at"):
                record["started_at"] = opened["started_at"]
            record["ended_at"] = now_iso()
            if opened.get("clock"):
                record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
            return record
        if operation or task_id not in self._open:
            known = self._open.get(task_id, {})
            self._open[task_id] = {
                "tool": operation or known.get("tool", ""),
                "args": known.get("args") or _args_of(payload),
                "started_at": known.get("started_at") or now_iso(),
                "clock": known.get("clock") or time.monotonic(),
            }
        return None


def _operation_of(payload: dict) -> str:
    event = payload.get("event")
    if isinstance(event, dict) and event.get("operation"):
        return str(event["operation"])
    return ""


def _args_of(payload: dict) -> dict:
    event = payload.get("event")
    if isinstance(event, dict) and isinstance(event.get("args"), dict):
        return event["args"]
    return {}


def _is_end(payload: dict) -> bool:
    if payload.get("payload_type") == "task.lifecycle.completed":
        return True
    event = payload.get("event")
    return isinstance(event, dict) and event.get("kind") == "completed"


def _is_error(payload: dict) -> bool:
    event = payload.get("event")
    if isinstance(event, dict):
        reason = str(event.get("reason") or event.get("error") or "")
        return "error" in reason.lower() or "fail" in reason.lower()
    return False


def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one non-interactive muse turn, tailing JSONL live like agent_pi."""
    harness_id = harness_session_id(request.session_dir, request.session_id)
    cmd = build_command(request, harness_id)

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    result = PiResult(session_id=request.session_id,
                      context_window=context_window(request.model))
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=request.cwd,
                               env=operator_env())
    if on_spawn:
        on_spawn(process.pid)
    deltas: list[str] = []
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
            payload = event.get("payload")
            if isinstance(payload, dict):
                if event.get("payload_type") == "run.output.delta":
                    text = payload.get("text")
                    if isinstance(text, str) and text:
                        deltas.append(text)
                if event.get("payload_type") == "run.terminal.completed":
                    text = payload.get("text")
                    if isinstance(text, str):
                        result.text = text   # terminal text wins over deltas
            if on_event:
                on_event(event)
    _, stderr = process.communicate()
    result.returncode = process.returncode
    if not result.text and deltas:
        result.text = "".join(deltas)        # no terminal event — keep the stream
    if result.returncode != 0 and not result.text and stderr.strip():
        result.text = stderr.strip()[-2000:]
    # Usage and cost: muse's stream carries neither — zeros, honestly.
    result.usage = UsageBreakdown()
    return result
