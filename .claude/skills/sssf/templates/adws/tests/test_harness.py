"""Harness contract: every worker backend behind one interface, pi path unchanged."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import agents, harness  # noqa: E402

CFG = "adws/adw_sssf_config/sssf.config.yaml"


def _config_with_agent(tmp_text, coding_agent, model="google/gemini-3.6-flash"):
    from pathlib import Path as P

    P("adws/adw_sssf_config").mkdir(parents=True, exist_ok=True)
    P("adws/adw_data/prompt_engineering/x").mkdir(parents=True, exist_ok=True)
    (P("adws/adw_data/prompt_engineering/x/system.md")).write_text("sys")
    (P("adws/adw_data/prompt_engineering/x/user.md")).write_text("usr")
    P("adws/adw_sssf_config/sssf.config.yaml").write_text(tmp_text.format(
        coding_agent=coding_agent, model=model))
    return agents.load_config("adws/adw_sssf_config/sssf.config.yaml")


MINIMAL = """\
defaults:
  coding_agent: pi
  model: google/gemini-3.6-flash
  thinking: medium
  data_dir: adws/adw_data
agents:
  - name: scout
    coding_agent: {coding_agent}
    model: {model}
    writes: []
    purpose: probe
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/x/system.md
      user: adws/adw_data/prompt_engineering/x/user.md
"""


def test_known_harnesses_resolve(repo, monkeypatch):
    for name in ("pi", "muse", "opencode", "codex", "omp"):
        module = harness.load(name)
        assert module.BINARY and len(module.BINARY) == 2
        assert isinstance(module.IMPLEMENTED, bool)
        assert callable(module.resolve_model)
        assert callable(module.run)
        assert callable(module.context_window)
    # claude_code is schema-valid but stubbed: loads, refuses at validate
    assert harness.load("claude_code").IMPLEMENTED is False
    with pytest.raises(harness.UnknownHarness):
        harness.load("definitely-not-a-harness")


def test_binary_prefers_env_override_then_path(repo, monkeypatch):
    monkeypatch.setenv("PI_PATH", "/bin/ls")
    assert harness.binary("pi") == "/bin/ls"
    monkeypatch.setenv("PI_PATH", "/nonexistent-xyz")
    assert harness.binary("pi") is None
    monkeypatch.delenv("PI_PATH")
    assert harness.binary("pi") is None  # no pi binary on this machine


def test_shaping_helpers_behave(repo):
    assert harness.tool_label("bash", {"command": "ls -la src"}) == "bash: ls -la src"
    assert harness.tool_label("read", {}) == "read"
    assert harness.clip_text("abc", 2).endswith("…")
    # moved verbatim from agent_pi: same outputs
    from adw_modules import agent_pi
    assert agent_pi._label("bash", {"command": "ls"}) == harness.tool_label("bash", {"command": "ls"})


def test_unknown_harness_fails_at_load(repo):
    # pydantic's Literal rejects it before validate() ever runs — fail fast,
    # with the allowed values named.
    import pydantic
    with pytest.raises(pydantic.ValidationError, match="coding_agent"):
        _config_with_agent(MINIMAL, "nope")


def test_validate_rejects_missing_binary(repo, monkeypatch):
    monkeypatch.setenv("OMP_PATH", "/nonexistent-xyz")
    cfg = _config_with_agent(MINIMAL, "omp", "ollama/qwen3.8:27b-mlx")
    with pytest.raises(SystemExit, match="needs its CLI"):
        agents.validate(cfg, ["scout"])


def test_validate_rejects_unimplemented_harness(repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_PATH", "/bin/ls")
    cfg = _config_with_agent(MINIMAL, "claude_code")
    with pytest.raises(SystemExit, match="not implemented"):
        agents.validate(cfg, ["scout"])


def test_request_fields_are_harness_generic(repo):
    from adw_modules.data_types import PiRequest
    req = PiRequest(prompt="p", system_prompt="s", model="m",
                    thinking="medium", session_id="s1",
                    session_dir="/tmp/x", raw_output_path="/tmp/y", cwd="/tmp")
    assert req.extensions == [] and req.tools is None
