"""Claude Code adapter: real envelope shapes replayed, tool turns synthetic.

The fixture is captured live output (system/init envelope, session id,
assistant message, error result with usage skeleton). Tool-turn cases below
are hand-built from the documented stream-json shapes — tool_use blocks on
assistant messages, tool_result blocks on user messages — and are marked
accordingly: they verify the adapter's folding logic, not the wire shapes.
Re-verify against a live tool turn once auth works (see the NOTE in
agent_cc.py), especially block field names.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import agent_cc  # noqa: E402
from adw_modules.data_types import PiRequest  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude_probe.jsonl"


def _events():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def _request(**kw):
    args = {"prompt": "hi", "system_prompt": "sys", "model": "sonnet",
            "thinking": "medium", "session_id": "s1", "session_dir": "/tmp/x",
            "raw_output_path": "/tmp/y", "cwd": "/tmp"}
    args.update(kw)
    return PiRequest(**args)


def _assistant(*blocks):
    return {"type": "assistant",
            "message": {"role": "assistant", "content": list(blocks)}}


def _user(*blocks):
    return {"type": "user",
            "message": {"role": "user", "content": list(blocks)}}


# ── real captured shapes ──────────────────────────────────────────────────

def test_init_event_carries_session_id_and_cwd():
    init = [e for e in _events()
            if e.get("type") == "system" and e.get("subtype") == "init"]
    assert len(init) == 1
    assert isinstance(init[0]["session_id"], str) and init[0]["session_id"]
    assert isinstance(init[0]["tools"], list) and "Bash" in init[0]["tools"]


def test_error_result_marks_is_error_with_usage_skeleton():
    results = [e for e in _events() if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0]["is_error"] is True
    assert results[0]["session_id"]
    assert results[0]["usage"]["input_tokens"] == 0


def test_auth_failure_text_is_extractable():
    assistants = [e for e in _events() if e.get("type") == "assistant"]
    assert len(assistants) == 1
    text = agent_cc._text_of(assistants[0]["message"]["content"])
    assert "Failed to authenticate" in text


# ── documented tool-turn shapes (synthetic; re-verify live) ───────────────

def test_tracker_folds_tool_use_into_one_record():
    tracker = agent_cc.ToolCallTracker()
    assert tracker.observe(_assistant(
        {"type": "tool_use", "id": "tu_1", "name": "Bash",
         "input": {"command": "ls /tmp"}})) is None
    record = tracker.observe(_user(
        {"type": "tool_result", "tool_use_id": "tu_1",
         "content": [{"type": "text", "text": "a\nb\n"}]}))
    assert record is not None
    assert record["tool"] == "Bash"
    assert record["tool_call_id"] == "tu_1"
    assert record["args"] == {"command": "ls /tmp"}
    assert record["ok"] is True
    assert record["label"] == "Bash: ls /tmp"
    assert record["result_snippet"] == "a\nb\n"
    assert record["started_at"] <= record["ended_at"]
    assert record["duration_ms"] >= 0


def test_tracker_marks_errored_results():
    tracker = agent_cc.ToolCallTracker()
    tracker.observe(_assistant({"type": "tool_use", "id": "tu_9", "name": "Bash",
                                "input": {"command": "false"}}))
    record = tracker.observe(_user({"type": "tool_result", "tool_use_id": "tu_9",
                                    "is_error": True, "content": "boom"}))
    assert record is not None and record["ok"] is False
    assert record["result_snippet"] == "boom"


def test_tracker_ignores_text_and_noise():
    tracker = agent_cc.ToolCallTracker()
    assert tracker.observe(_assistant({"type": "text", "text": "hi"})) is None
    assert tracker.observe({}) is None
    assert tracker.observe("not-a-dict") is None
    assert tracker.observe({"type": "system", "subtype": "init"}) is None
    # result without a matching announce still records under the tool id
    record = tracker.observe(_user({"type": "tool_result", "tool_use_id": "zz",
                                    "content": "x"}))
    assert record is not None and record["tool"] == "tool"


# ── pure helpers ──────────────────────────────────────────────────────────

def test_effort_maps_pi_ladder():
    assert agent_cc.effort_for("low") == "low"
    assert agent_cc.effort_for("xhigh") == "xhigh"
    assert agent_cc.effort_for("max") == "max"
    assert agent_cc.effort_for("off") == "low"
    assert agent_cc.effort_for("minimal") == "low"
    assert agent_cc.effort_for("mystery") == "medium"
    assert agent_cc.effort_for("") == "medium"


def test_resolve_model_passes_through_and_rejects_blank():
    assert agent_cc.resolve_model("sonnet") == "sonnet"
    assert agent_cc.resolve_model("claude-opus-4-6") == "claude-opus-4-6"
    with pytest.raises(ValueError, match="needs a model id"):
        agent_cc.resolve_model("   ")
    assert agent_cc.context_window("anything") == 0


def test_tools_for_translates_claude_vocabulary():
    assert agent_cc.tools_for(["read", "bash", "edit", "write", "grep"]) == \
        ["Read", "Bash", "Edit", "Write", "Grep"]
    assert agent_cc.tools_for(["find"]) == ["Glob"]
    assert agent_cc.tools_for(["read", "ls", "subagent_create"]) == ["Read"]
    assert agent_cc.tools_for(["ls"]) == []
    assert agent_cc.tools_for(None) == []
    assert agent_cc.tools_for([]) == []


def test_session_mapping_is_stable_and_first_write_wins(tmp_path):
    session_dir = tmp_path / "agent" / "claude_sessions"
    session_dir.mkdir(parents=True)
    agent_cc.record_session_id(str(session_dir), "sssf-aaa-builder", "cc-id-1")
    assert agent_cc.lookup_session_id(str(session_dir), "sssf-aaa-builder") == "cc-id-1"
    assert agent_cc.record_session_id(str(session_dir), "sssf-aaa-builder", "cc-id-2") == "cc-id-1"
    assert agent_cc.lookup_session_id(str(session_dir), "unknown") is None
    assert (session_dir.parent / "claude_sessions.json").is_file()


def test_build_command_shape():
    cmd = agent_cc.build_command(_request(), None)
    assert cmd[0].endswith("claude") and "-p" in cmd
    assert "--verbose" in cmd and "--output-format" in cmd
    assert "--permission-mode" in cmd and "bypassPermissions" in cmd
    assert "--model" in cmd and "sonnet" in cmd
    assert "--effort" in cmd and "medium" in cmd
    assert "--system-prompt" in cmd and "sys" in cmd
    assert "--resume" not in cmd
    assert cmd[-1] == "hi"  # prompt travels last as argv, never stdin
    resumed = agent_cc.build_command(_request(), "cc-id-9")
    assert "--resume" in resumed and "cc-id-9" in resumed


def test_build_command_tools_and_blank_system():
    cmd = agent_cc.build_command(_request(tools=["read", "ls"]), None)
    assert "--allowedTools" in cmd and "Read" in cmd
    assert "ls" not in cmd
    cmd = agent_cc.build_command(_request(tools=["ls"]), None)
    assert "--allowedTools" not in cmd
    cmd = agent_cc.build_command(_request(system_prompt="  "), None)
    assert "--system-prompt" not in cmd


def test_fold_result_usage_counts_real_tokens_and_dollars():
    from adw_modules.data_types import PiResult
    result = PiResult(session_id="s", context_window=0)
    agent_cc._fold_result_usage(result, {
        "usage": {"input_tokens": 100, "output_tokens": 25,
                  "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10,
                  "output_tokens_details": {"thinking_tokens": 5}},
        "total_cost_usd": 0.0123,
    })
    assert result.tokens == 125
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 25
    assert result.usage.cache_read_tokens == 40
    assert result.usage.cache_write_tokens == 10
    assert result.context_tokens == 125
    assert abs(result.cost - 0.0123) < 1e-9


def test_error_message_prefers_result_text():
    assert agent_cc._error_message({"result": "boom"}) == "boom"
    assert agent_cc._error_message({"errors": ["a", "b"]}) == "a; b"
    assert agent_cc._error_message({}) == ""
