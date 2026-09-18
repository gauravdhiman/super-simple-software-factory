"""Session read-boundary: every agent call states what is off-limits for reading."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import agents  # noqa: E402


def test_boundary_names_repo_and_own_session():
    text = agents.boundary_text("/repo", "/data/sessions/abc123")
    assert "/repo" in text
    assert "/data/sessions/abc123" in text


def test_boundary_forbids_sibling_runs_without_naming_any():
    text = agents.boundary_text("/repo", "/data/sessions/abc123")
    assert "/data/sessions/" in text and "<another-adw-id>" in text
    assert "never list, open, or read" in text
    # no real run id is named — the prohibition is general, not a pointer
    assert "8401f23f" not in text


def test_boundary_appends_cleanly_to_system_text():
    text = agents.boundary_text("/repo", "/data/sessions/abc123")
    assert text.startswith("\n\n")
    assert "identity" not in text  # task-invariant: nothing about who the agent is
