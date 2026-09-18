"""Coding-harness interface: every worker backend behind one contract.

An ADW names agents, never harnesses — `agents.execute()` dispatches through
here. Each `agent_<name>` module implements the contract:

    BINARY: (env_var, default_bin)  how to find the CLI
    IMPLEMENTED: bool               False = schema-valid but stubbed
    resolve_model(pattern) -> str   harness-native model id (raises on miss)
    run(request, on_event, on_spawn, on_exit) -> PiResult-compatible
    ToolCallTracker                 observe(event) -> normalized record | None
    context_window(model) -> int    0 when the harness reports no ceiling

`PiRequest`/`PiResult` keep their names but are harness-generic: every adapter
reads the fields it understands and ignores the rest (`extensions` is pi-only;
`tools` is best-effort where the harness has no allowlist — the repo boundary
stays enforced post-hoc by permissions.py either way). Usage and cost ride
`UsageBreakdown` where the harness reports them, else zeros with the run still
fully traced. Auth is never the factory's business: each CLI uses whatever the
operator configured (subscription or keys).
"""

from __future__ import annotations

import importlib
import os
import shutil

RESULT_SNIPPET_CHARS = 20_000   # tool output rides along whole; clip only guards pathological cases
ARG_VALUE_CHARS = 20_000        # args too — the UI scrolls, it must not be handed cut-off data
LABEL_CHARS = 80                # "bash: <command>" shown as the event name

# The arg that identifies a call at a glance, in the order tools tend to use.
PRIMARY_ARGS = ("command", "path", "file_path", "pattern", "query", "url")

CODING_AGENTS = ("pi", "claude_code", "opencode", "codex", "muse", "omp")

# Roster value -> adapter module. Explicit, not derived: history already gave
# us agent_cc for coding_agent "claude_code".
_MODULES = {
    "pi": "agent_pi",
    "claude_code": "agent_cc",
    "opencode": "agent_opencode",
    "codex": "agent_codex",
    "muse": "agent_muse",
    "omp": "agent_omp",
}

_BINARIES = {
    "pi": ("PI_PATH", "pi"),
    "claude_code": ("CLAUDE_PATH", "claude"),
    "opencode": ("OPENCODE_PATH", "opencode"),
    "codex": ("CODEX_PATH", "codex"),
    "muse": ("MUSE_PATH", "muse"),
    "omp": ("OMP_PATH", "omp"),
}


class UnknownHarness(ValueError):
    """No agent_<name> module exists for this coding_agent value."""


def load(harness: str):
    """Import and return the adapter module.

    Unknown names raise; known names whose module does not exist yet raise too
    — a harness becomes runnable the moment its adapter lands, with no registry
    edit beyond CODING_AGENTS.
    """
    if harness not in CODING_AGENTS:
        raise UnknownHarness(
            f"coding_agent {harness!r} is unknown — choose from {', '.join(CODING_AGENTS)}")
    module_name = _MODULES[harness]
    try:
        return importlib.import_module(f".{module_name}", package=__name__.rpartition(".")[0])
    except ModuleNotFoundError as e:
        raise UnknownHarness(
            f"coding_agent {harness!r} has no adapter module yet "
            f"(expected adw_modules/{module_name}.py)") from e


def binary(harness: str) -> str | None:
    """The CLI to spawn: $ENV override when set to an executable, else PATH."""
    env_var, default = _BINARIES[harness]
    override = (os.environ.get(env_var) or "").strip()
    if override:
        return override if _executable(override) else None
    return shutil.which(default)


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def clip_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def tool_label(tool: str, args: dict) -> str:
    """One-line human name for a tool call: `bash: ls -la src`."""
    value = next((args[key] for key in PRIMARY_ARGS
                  if isinstance(args.get(key), str) and args[key].strip()), "")
    if not value:
        value = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
    value = " ".join(str(value).split())
    return f"{tool}: {clip_text(value, LABEL_CHARS)}" if value else tool
