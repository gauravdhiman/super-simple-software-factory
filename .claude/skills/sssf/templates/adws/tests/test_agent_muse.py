"""Muse adapter: verified protocol shapes, replayed from a captured stream."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import agent_muse  # noqa: E402
from adw_modules.data_types import PiRequest  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "muse_probe.jsonl"


def _events():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def _request(**kw):
    args = {"prompt": "hi", "system_prompt": "sys", "model": "muse-spark-1.3-contributor",
            "thinking": "medium", "session_id": "s1", "session_dir": "/tmp/x",
            "raw_output_path": "/tmp/y", "cwd": "/tmp"}
    args.update(kw)
    return PiRequest(**args)


def test_terminal_text_wins_over_deltas():
    text, deltas = "", []
    for event in _events():
        payload = event.get("payload") or {}
        if event.get("payload_type") == "run.output.delta" and payload.get("text"):
            deltas.append(payload["text"])
        if event.get("payload_type") == "run.terminal.completed":
            text = payload.get("text")
    assert text == "probe-ok" and "".join(deltas) == "probe-ok"


def test_tracker_folds_task_lifecycles_into_records():
    tracker = agent_muse.ToolCallTracker()
    records = [r for e in _events() if (r := tracker.observe(e)) is not None]
    assert len(records) == 3  # two reminder children + the model turn
    by_tool = {r["tool"] for r in records}
    assert "model.meta.response" in by_tool and "reminder.child_run" in by_tool
    for record in records:
        assert record["tool_call_id"] and record["ended_at"] and record["label"]


def test_tracker_ignores_heartbeat_and_noise():
    tracker = agent_muse.ToolCallTracker()
    assert tracker.observe({"payload_type": "task.lifecycle.status", "payload": {}}) is None
    assert tracker.observe({}) is None
    assert tracker.observe("not-a-dict") is None


def test_thinking_maps_pi_ladder():
    assert agent_muse.THINKING["off"] == "none"
    assert agent_muse.THINKING["medium"] == "medium"
    assert agent_muse.THINKING["max"] == "max"
    cmd = agent_muse.build_command(_request(thinking="mystery"), "u")
    assert "--reasoning-effort" in cmd
    assert cmd[cmd.index("--reasoning-effort") + 1] == "medium"


def test_resolve_model_passes_through_and_rejects_blank():
    assert agent_muse.resolve_model("muse-spark-1.3-contributor") == "muse-spark-1.3-contributor"
    with pytest.raises(ValueError):
        agent_muse.resolve_model("   ")
    assert agent_muse.context_window("anything") == 0


def test_compose_prompt_keeps_system_ahead():
    composed = agent_muse.compose_prompt("who you are", "do the thing")
    assert composed.index("who you are") < composed.index("do the thing")
    assert agent_muse.compose_prompt("  ", "do the thing") == "do the thing"


def test_session_mapping_is_stable_and_persisted(tmp_path):
    session_dir = tmp_path / "agent" / "pi_sessions"
    session_dir.mkdir(parents=True)
    first = agent_muse.harness_session_id(str(session_dir), "sssf-aaa-scout-x1")
    second = agent_muse.harness_session_id(str(session_dir), "sssf-aaa-scout-x1")
    other = agent_muse.harness_session_id(str(session_dir), "sssf-aaa-builder-x1")
    assert first == second and first != other
    assert (session_dir.parent / "muse_sessions.json").is_file()


def test_build_command_shape():
    cmd = agent_muse.build_command(_request(), "uuid-here")
    assert cmd[0].endswith("muse") and "exec" in cmd and "--json" in cmd
    assert "--session-id" in cmd and "uuid-here" in cmd
    assert "--trust-workspace" in cmd and "--approval-mode" in cmd
    assert cmd[-1].startswith("sys")  # composed prompt travels last, like pi
