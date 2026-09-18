"""OMP adapter: verified protocol shapes, replayed from a captured stream."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import agent_omp  # noqa: E402
from adw_modules.data_types import PiRequest  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "omp_probe.jsonl"


def _events():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def _request(**kw):
    args = {"prompt": "hi", "system_prompt": "sys", "model": "ollama/qwen3.8:27b-mlx",
            "thinking": "medium", "session_id": "s1", "session_dir": "/tmp/x",
            "raw_output_path": "/tmp/y", "cwd": "/tmp"}
    args.update(kw)
    return PiRequest(**args)


def test_session_event_carries_the_id_first():
    first = _events()[0]
    assert first["type"] == "session"
    assert isinstance(first["id"], str) and first["id"]


def test_last_assistant_message_wins():
    texts = []
    for event in _events():
        if event.get("type") == "message_end" \
                and event.get("message", {}).get("role") == "assistant":
            text = agent_omp._text_of(event["message"])
            if text:
                texts.append(text)
    assert texts and texts[-1] == "tools-ok"


def test_tracker_emits_one_record_per_finished_call():
    tracker = agent_omp.ToolCallTracker()
    records = [r for e in _events() if (r := tracker.observe(e)) is not None]
    assert len(records) == 1
    (record,) = records
    assert record["tool"] == "bash"
    assert record["args"] == {"command": "ls /tmp | head -5"}
    assert record["ok"] is True
    assert record["label"] == "bash: ls /tmp | head -5"
    assert "247ai-staging-backend-health.txt" in record["result_snippet"]
    assert record["started_at"] <= record["ended_at"]
    assert record["duration_ms"] >= 0


def test_tracker_ignores_noise_and_announces_without_id():
    tracker = agent_omp.ToolCallTracker()
    assert tracker.observe({}) is None
    assert tracker.observe("not-a-dict") is None
    assert tracker.observe({"type": "message_update"}) is None
    # announce without a call id stages nothing and emits nothing
    assert tracker.observe({"type": "tool_execution_start",
                            "toolName": "bash", "args": {}}) is None


def test_tracker_marks_errored_calls():
    tracker = agent_omp.ToolCallTracker()
    tracker.observe({"type": "tool_execution_start", "toolCallId": "c1",
                     "toolName": "bash", "args": {"command": "false"}})
    record = tracker.observe({"type": "tool_execution_end", "toolCallId": "c1",
                              "toolName": "bash", "isError": True,
                              "result": {"content": [{"type": "text", "text": "boom"}]}})
    assert record is not None and record["ok"] is False


def test_thinking_passes_through_omp_ladder():
    assert agent_omp.thinking_for("low") == "low"
    assert agent_omp.thinking_for("auto") == "auto"
    assert agent_omp.thinking_for("mystery") == "medium"
    assert agent_omp.thinking_for("") == "medium"


def test_resolve_model_needs_nonempty():
    with pytest.raises(ValueError, match="needs a model id"):
        agent_omp.resolve_model("   ")


def test_resolve_model_against_catalog(monkeypatch):
    monkeypatch.setattr(agent_omp, "_omp_catalog", lambda: [
        ("ollama", "qwen3.8:27b-mlx", 128000),
        ("openai", "gpt-5.6-luna", 400000),
    ])
    assert agent_omp.resolve_model("ollama/qwen3.8:27b-mlx") == "ollama/qwen3.8:27b-mlx"
    assert agent_omp.resolve_model("qwen3.8:27b-mlx") == "ollama/qwen3.8:27b-mlx"
    with pytest.raises(ValueError, match="not found"):
        agent_omp.resolve_model("ollama/nope")
    assert agent_omp.context_window("ollama/qwen3.8:27b-mlx") == 128000
    assert agent_omp.context_window("qwen3.8:27b-mlx") == 128000
    assert agent_omp.context_window("unknown/model") == 0


def test_resolve_model_ambiguous_pattern_names_candidates(monkeypatch):
    monkeypatch.setattr(agent_omp, "_omp_catalog", lambda: [
        ("a", "shared-x", 0),
        ("b", "shared-x", 0),
    ])
    with pytest.raises(ValueError, match="ambiguous"):
        agent_omp.resolve_model("shared-x")


def test_resolve_model_empty_catalog(monkeypatch):
    monkeypatch.setattr(agent_omp, "_omp_catalog", lambda: [])
    with pytest.raises(ValueError, match="catalog is empty"):
        agent_omp.resolve_model("anything")


def test_session_mapping_is_stable_and_persisted(tmp_path):
    session_dir = tmp_path / "agent" / "omp_sessions"
    session_dir.mkdir(parents=True)
    first = agent_omp.record_session_id(str(session_dir), "sssf-aaa-scout", "omp-id-1")
    assert agent_omp.lookup_session_id(str(session_dir), "sssf-aaa-scout") == "omp-id-1"
    # first write wins — a re-emitted id never overwrites
    assert agent_omp.record_session_id(str(session_dir), "sssf-aaa-scout", "omp-id-2") == "omp-id-1"
    assert agent_omp.lookup_session_id(str(session_dir), "unknown") is None
    assert (session_dir.parent / "omp_sessions.json").is_file()


def test_build_command_fresh_and_resume():
    fresh = agent_omp.build_command(_request(), None)
    assert fresh[0].endswith("omp") and "-p" in fresh and "--mode" in fresh
    assert "--model" in fresh and "ollama/qwen3.8:27b-mlx" in fresh
    assert "--system-prompt" in fresh and "sys" in fresh
    assert "--session-dir" in fresh and "--cwd" in fresh
    assert "-r" not in fresh
    assert fresh[-1] == "hi"  # prompt travels last, like pi
    resumed = agent_omp.build_command(_request(), "omp-id-9")
    assert "-r" in resumed and "omp-id-9" in resumed
    assert resumed.index("-r") < len(resumed) - 1


def test_build_command_tools_and_extensions():
    cmd = agent_omp.build_command(_request(tools=["read", "bash"],
                                           extensions=["/abs/ext.ts"]), None)
    assert "--tools" in cmd and "read,bash" in cmd
    assert "-e" in cmd and "/abs/ext.ts" in cmd
    bare = agent_omp.build_command(_request(tools=None, extensions=[]), None)
    assert "--tools" not in bare and "-e" not in bare


def test_tools_for_translates_pi_vocabulary():
    assert agent_omp.tools_for(["read", "bash", "edit", "write", "grep"]) == \
        ["read", "bash", "edit", "write", "grep"]
    assert agent_omp.tools_for(["find"]) == ["glob"]
    assert agent_omp.tools_for(["glob"]) == ["glob"]
    # pi-only and foreign-extension tools are dropped, never passed through
    assert agent_omp.tools_for(["read", "ls", "find", "subagent_create"]) == \
        ["read", "glob"]
    assert agent_omp.tools_for(["ls"]) == []
    assert agent_omp.tools_for(None) == []
    assert agent_omp.tools_for([]) == []


def test_build_command_drops_unmapped_tools_flag():
    cmd = agent_omp.build_command(_request(tools=["read", "ls", "find"]), None)
    assert "--tools" in cmd and "read,glob" in cmd
    cmd = agent_omp.build_command(_request(tools=["ls"]), None)
    assert "--tools" not in cmd
