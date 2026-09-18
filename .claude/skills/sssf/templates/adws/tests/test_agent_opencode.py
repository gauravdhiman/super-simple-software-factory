"""OpenCode adapter: verified protocol shapes, replayed from a captured stream."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import agent_opencode  # noqa: E402
from adw_modules.data_types import PiRequest, PiResult  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "opencode_probe.jsonl"


def _events():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def _request(**kw):
    args = {"prompt": "hi", "system_prompt": "sys", "model": "meta/muse-spark-1.3-contributor",
            "thinking": "medium", "session_id": "s1", "session_dir": "/tmp/x",
            "raw_output_path": "/tmp/y", "cwd": "/tmp"}
    args.update(kw)
    return PiRequest(**args)


def _text_event(part_id="prt-1", text="hello", message_id="msg-1"):
    return {"type": "text", "timestamp": 1, "sessionID": "ses-1",
            "part": {"id": part_id, "messageID": message_id, "sessionID": "ses-1",
                     "type": "text", "text": text}}


def _tool_event(call_id="call-1", tool="bash", status="completed",
                args=None, output="out\n", start=1000, end=1025):
    return {"type": "tool_use", "timestamp": end, "sessionID": "ses-1",
            "part": {"id": "prt-t", "messageID": "msg-1", "sessionID": "ses-1",
                     "type": "tool", "tool": tool, "callID": call_id,
                     "state": {"status": status,
                               "input": args if args is not None else {"command": "echo hi"},
                               "output": output,
                               "metadata": {"exit": 0},
                               "time": {"start": start, "end": end}}}}


def test_text_collector_assembles_reply_from_fixture():
    collector = agent_opencode.TextCollector()
    for event in _events():
        collector.observe(event)
    assert collector.text == "tool-probe-hi"


def test_text_collector_keeps_latest_snapshot_per_part():
    collector = agent_opencode.TextCollector()
    collector.observe(_text_event(part_id="p1", text="hel"))
    collector.observe(_text_event(part_id="p1", text="hello"))
    collector.observe(_text_event(part_id="p2", text=" world"))
    collector.observe({"type": "step_start", "part": {"type": "step-start"}})
    collector.observe({})
    assert collector.text == "hello world"


def test_tracker_emits_one_record_per_finished_call():
    tracker = agent_opencode.ToolCallTracker()
    records = [r for e in _events() if (r := tracker.observe(e)) is not None]
    assert len(records) == 1
    (record,) = records
    assert record["tool"] == "bash"
    assert record["tool_call_id"].startswith("call_")
    assert record["args"] == {"command": "echo tool-probe-hi"}
    assert record["ok"] is True
    assert record["label"] == "bash: echo tool-probe-hi"
    assert record["result_snippet"] == "tool-probe-hi\n"
    assert record["started_at"] < record["ended_at"]
    assert record["duration_ms"] == 25  # the stream's own span, end - start


def test_tracker_stages_pending_and_marks_errors():
    tracker = agent_opencode.ToolCallTracker()
    assert tracker.observe(_tool_event(status="running")) is None
    assert tracker.observe(_tool_event(status="pending")) is None
    record = tracker.observe(_tool_event(status="error"))
    assert record is not None and record["ok"] is False
    assert tracker.observe({"type": "tool_use", "part": {"type": "tool"}}) is None
    assert tracker.observe({}) is None
    assert tracker.observe("not-a-dict") is None


def test_variant_maps_ladder_verbatim_except_off():
    assert agent_opencode.variant_for("off") is None
    assert agent_opencode.variant_for("") is None
    for rung in ("minimal", "low", "medium", "high", "xhigh", "max"):
        assert agent_opencode.variant_for(rung) == rung
    # provider-specific and ignored when unknown — passthrough never fails
    assert agent_opencode.variant_for("mystery") == "mystery"


def test_resolve_model_passes_through_and_rejects_blank():
    assert agent_opencode.resolve_model("meta/muse-spark-1.3-contributor") == \
        "meta/muse-spark-1.3-contributor"
    with pytest.raises(ValueError):
        agent_opencode.resolve_model("   ")
    assert agent_opencode.context_window("anything") == 0


def test_compose_prompt_keeps_system_ahead():
    composed = agent_opencode.compose_prompt("who you are", "do the thing")
    assert composed.index("who you are") < composed.index("do the thing")
    assert agent_opencode.compose_prompt("  ", "do the thing") == "do the thing"


def test_session_lookup_misses_then_records(tmp_path):
    session_dir = tmp_path / "agent" / "pi_sessions"
    session_dir.mkdir(parents=True)
    assert agent_opencode.lookup_session_id(str(session_dir), "sssf-aaa-scout-x1") is None
    agent_opencode.record_session_id(str(session_dir), "sssf-aaa-scout-x1", "ses-abc")
    assert agent_opencode.lookup_session_id(str(session_dir), "sssf-aaa-scout-x1") == "ses-abc"
    assert agent_opencode.lookup_session_id(str(session_dir), "sssf-aaa-other-x1") is None
    assert (session_dir.parent / "opencode_sessions.json").is_file()


def test_build_command_shape():
    cmd = agent_opencode.build_command(_request(), None)
    assert cmd[:2] == [cmd[0], "run"] and cmd[0].endswith("opencode")
    assert "--format" in cmd and cmd[cmd.index("--format") + 1] == "json"
    assert cmd[cmd.index("-m") + 1] == "meta/muse-spark-1.3-contributor"
    assert "--agent" in cmd and cmd[cmd.index("--agent") + 1] == "build"
    assert "--auto" in cmd
    assert cmd[cmd.index("--dir") + 1] == "/tmp"
    assert cmd[cmd.index("--variant") + 1] == "medium"
    assert "--session" not in cmd
    assert cmd[-1].startswith("sys")  # composed prompt travels last, like pi


def test_build_command_resume_and_off():
    resumed = agent_opencode.build_command(_request(), "ses-abc")
    assert resumed[resumed.index("--session") + 1] == "ses-abc"
    off = agent_opencode.build_command(_request(thinking="off"), None)
    assert "--variant" not in off  # off keeps the provider default


def test_step_usage_folds_spend_and_occupancy():
    result = PiResult(session_id="s1")
    for event in _events():
        part = event.get("part") or {}
        if event.get("type") == "step_finish":
            agent_opencode._fold_step_usage(result, part)
    assert result.usage.input_tokens == 10399 + 177
    assert result.usage.output_tokens == 70 + 15
    assert result.usage.reasoning_tokens == 33 + 17
    assert result.usage.cache_read_tokens == 10353
    assert result.usage.total_tokens == (10399 + 70) + (177 + 15 + 10353)
    assert result.tokens == result.usage.total_tokens
    assert result.cost == pytest.approx(0.0010605 + 4.4806e-05)
    # total is window occupancy, not spend: the last step's total wins
    assert result.context_tokens == 10562


def test_error_message_prefers_provider_text():
    event = {"type": "error", "sessionID": "ses-1",
             "error": {"name": "APIError", "data": {"message": "not eligible"}}}
    assert agent_opencode._error_message(event) == "not eligible"
