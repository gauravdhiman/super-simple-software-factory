"""Codex adapter: verified protocol shapes, replayed from a captured stream."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import agent_codex  # noqa: E402
from adw_modules.data_types import PiRequest, PiResult  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "codex_probe.jsonl"


def _events():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def _request(**kw):
    args = {"prompt": "hi", "system_prompt": "sys", "model": "gpt-5.6-luna",
            "thinking": "medium", "session_id": "s1", "session_dir": "/tmp/x",
            "raw_output_path": "/tmp/y", "cwd": "/tmp"}
    args.update(kw)
    return PiRequest(**args)


def _message(text="hello"):
    return {"type": "item.completed",
            "item": {"id": "m1", "type": "agent_message", "text": text}}


def _command_event(item_id="item_0", status="completed", exit_code=0,
                   command="/bin/zsh -lc 'echo hi'", output="hi\n"):
    return {"type": "item.completed" if status in ("completed", "failed") else "item.started",
            "item": {"id": item_id, "type": "command_execution", "command": command,
                     "aggregated_output": output if status == "completed" else "",
                     "exit_code": exit_code, "status": status}}


def test_text_collector_assembles_reply_from_fixture():
    collector = agent_codex.TextCollector()
    for event in _events():
        collector.observe(event)
    assert collector.text == "tool-probe-hi"


def test_text_collector_concatenates_messages_in_order():
    collector = agent_codex.TextCollector()
    collector.observe(_message("first "))
    collector.observe({"type": "item.completed",
                       "item": {"id": "r1", "type": "reasoning", "text": "thinking"}})
    collector.observe(_message("second"))
    assert collector.text == "first second"


def test_tracker_emits_one_record_per_finished_item():
    tracker = agent_codex.ToolCallTracker()
    records = [r for e in _events() if (r := tracker.observe(e)) is not None]
    assert len(records) == 1
    (record,) = records
    assert record["tool"] == "command_execution"
    assert record["tool_call_id"] == "item_0"
    assert record["args"] == {"command": "/bin/zsh -lc 'echo tool-probe-hi'"}
    assert record["ok"] is True
    assert record["label"] == "command_execution: /bin/zsh -lc 'echo tool-probe-hi'"
    assert record["result_snippet"] == "tool-probe-hi\n"
    assert record["started_at"] <= record["ended_at"]
    assert record["duration_ms"] >= 0


def test_tracker_stages_started_and_marks_failures():
    tracker = agent_codex.ToolCallTracker()
    assert tracker.observe(_command_event(status="in_progress")) is None
    record = tracker.observe(_command_event(status="completed"))
    assert record is not None and record["ok"] is True
    failed = agent_codex.ToolCallTracker().observe(
        _command_event(status="failed", exit_code=1))
    assert failed is not None and failed["ok"] is False
    nonzero = agent_codex.ToolCallTracker().observe(
        _command_event(status="completed", exit_code=3))
    assert nonzero["ok"] is False
    assert tracker.observe(_message()) is None
    assert tracker.observe({}) is None
    assert tracker.observe("not-a-dict") is None


def test_tracker_folds_file_change():
    tracker = agent_codex.ToolCallTracker()
    event = {"type": "item.completed",
             "item": {"id": "item_0", "type": "file_change",
                      "changes": [{"path": "/tmp/notes.txt", "kind": "add"}],
                      "status": "completed"}}
    (record,) = [r for r in [tracker.observe(event)] if r is not None]
    assert record["tool"] == "file_change"
    assert record["args"] == {"path": "/tmp/notes.txt", "kind": "add"}
    assert record["label"] == "file_change: /tmp/notes.txt"
    assert record["ok"] is True
    assert "add /tmp/notes.txt" in record["result_snippet"]


def test_effort_maps_ladder_to_codex_vocabulary():
    assert agent_codex.effort_for("off") == "none"
    assert agent_codex.effort_for("minimal") == "low"
    assert agent_codex.effort_for("low") == "low"
    assert agent_codex.effort_for("medium") == "medium"
    assert agent_codex.effort_for("high") == "high"
    assert agent_codex.effort_for("xhigh") == "xhigh"
    assert agent_codex.effort_for("max") == "max"
    # codex 400-fails unknown values, so they fall back instead of passing through
    assert agent_codex.effort_for("mystery") == "medium"
    assert agent_codex.effort_for("") == "medium"


def test_resolve_model_passes_through_and_rejects_blank():
    assert agent_codex.resolve_model("gpt-5.6-luna") == "gpt-5.6-luna"
    with pytest.raises(ValueError):
        agent_codex.resolve_model("   ")
    assert agent_codex.context_window("anything") == 0


def test_compose_prompt_keeps_system_ahead():
    composed = agent_codex.compose_prompt("who you are", "do the thing")
    assert composed.index("who you are") < composed.index("do the thing")
    assert agent_codex.compose_prompt("  ", "do the thing") == "do the thing"


def test_session_lookup_misses_then_records(tmp_path):
    session_dir = tmp_path / "agent" / "pi_sessions"
    session_dir.mkdir(parents=True)
    assert agent_codex.lookup_session_id(str(session_dir), "sssf-aaa-scout-x1") is None
    agent_codex.record_session_id(str(session_dir), "sssf-aaa-scout-x1", "thr-abc")
    assert agent_codex.lookup_session_id(str(session_dir), "sssf-aaa-scout-x1") == "thr-abc"
    assert agent_codex.lookup_session_id(str(session_dir), "sssf-aaa-other-x1") is None
    assert (session_dir.parent / "codex_sessions.json").is_file()


def test_build_command_fresh_sets_everything():
    cmd = agent_codex.build_command(_request(), None)
    assert cmd[:2] == [cmd[0], "exec"] and cmd[0].endswith("codex")
    assert "--json" in cmd and "--skip-git-repo-check" in cmd
    assert cmd[cmd.index("-C") + 1] == "/tmp"
    assert cmd[cmd.index("-m") + 1] == "gpt-5.6-luna"
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert "approval_policy=never" in cmd[cmd.index("-c") + 1]
    assert "model_reasoning_effort=medium" in " ".join(cmd)
    assert "resume" not in cmd
    assert cmd[-1].startswith("sys")  # composed prompt travels last, like pi


def test_build_command_resume_continues_only():
    resumed = agent_codex.build_command(_request(), "thr-abc")
    assert "resume" in resumed
    assert resumed[-2] == "thr-abc"  # thread id travels just ahead of the prompt
    assert "-C" not in resumed and "-s" not in resumed  # resume accepts neither
    assert "--json" in resumed and "--skip-git-repo-check" in resumed
    off = agent_codex.build_command(_request(thinking="off"), None)
    assert "model_reasoning_effort=none" in " ".join(off)


def test_turn_usage_folds_spend_with_cached_as_share():
    result = PiResult(session_id="s1")
    for event in _events():
        if event.get("type") == "turn.completed":
            agent_codex._fold_turn_usage(result, event.get("usage"))
    assert result.usage.input_tokens == 47567
    assert result.usage.output_tokens == 162
    assert result.usage.reasoning_tokens == 44
    assert result.usage.cache_read_tokens == 40192
    # input already includes the cached share: spend is input + output
    assert result.usage.total_tokens == 47567 + 162
    assert result.tokens == result.usage.total_tokens
    assert result.cost == 0.0  # the stream carries no cost
    assert result.context_tokens == 47567  # occupancy: the context as it stands


def test_error_message_unwraps_provider_json():
    inner = json.dumps({"type": "error", "status": 400,
                        "error": {"type": "invalid_request_error",
                                  "message": "bad model"}})
    assert agent_codex._error_message({"type": "error", "message": inner}) == "bad model"
    assert agent_codex._error_message(
        {"type": "turn.failed", "error": {"message": inner}}) == "bad model"
