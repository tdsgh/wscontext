"""Grouping of raw tool names into the kinds the turn and tool-call statistics report."""

import re

KINDS = {
    "Grep": "search", "Glob": "search", "WebSearch": "search", "ToolSearch": "search",
    "Read": "read", "NotebookRead": "read", "LS": "read", "WebFetch": "read",
    "Edit": "edit", "MultiEdit": "edit", "Write": "edit", "NotebookEdit": "edit",
    "Bash": "shell", "BashOutput": "shell",
    "Task": "agent", "Agent": "agent",
}

# `ctx <subcommand>` at the start of a shell segment, optionally behind a path.
_CTX = re.compile(r"(?:^|[;&|(\n])\s*(?:\S*[/\\])?ctx(?:\.exe)?\s+([A-Za-z][\w-]*)([^;&|\n]*)")


def ctx_invocations(command: str) -> list[tuple[str, list[str]]]:
    """Every `ctx` call in a shell command, as (subcommand, argv after `ctx`)."""
    return [(m.group(1), [m.group(1), *m.group(2).split()]) for m in _CTX.finditer(command)]


def tool_kind(name: str, command: str | None = None) -> str:
    """The kind of a tool call: a Bash call that runs `ctx` is `ctx`, not `shell`."""
    if name == "Bash" and command and ctx_invocations(command):
        return "ctx"
    if name.startswith("mcp__"):
        return "mcp"
    return KINDS.get(name, "other")
