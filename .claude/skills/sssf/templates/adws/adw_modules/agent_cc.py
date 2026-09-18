"""Claude Code interface — STUB in v1. The factory is Pi-only for now.

The config schema accepts `coding_agent: claude_code` so nothing breaks at the
schema level, but selecting it raises until v2 implements this interface
(`claude -p --output-format stream-json --resume <session_id>`).
"""

from __future__ import annotations

BINARY = ("CLAUDE_PATH", "claude")
IMPLEMENTED = False


def resolve_model(pattern: str) -> str:
    raise NotImplementedError(
        "coding_agent 'claude_code' is not implemented in v1 — SSSF v1 runs the "
        "Pi coding agent only. Set coding_agent: pi (or omit it) in sssf.config.yaml."
    )


def context_window(model: str) -> int:
    return 0


class ToolCallTracker:
    """Unreachable while IMPLEMENTED is False — present for contract checks."""

    def observe(self, event: dict):
        raise NotImplementedError("coding_agent 'claude_code' is not implemented in v1")


def run(*args, **kwargs):
    raise NotImplementedError(
        "coding_agent 'claude_code' is not implemented in v1 — SSSF v1 runs the "
        "Pi coding agent only. Set coding_agent: pi (or omit it) in sssf.config.yaml."
    )
