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


def ctx_call_key(sub: str, argv: list[str]) -> str:
    """The `ctx_calls` key: a `why` on a name (`#name`, `path#name`) counts as `why#name`."""
    if sub != "why":
        return sub
    args, i = argv[1:], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("--") and "=" not in a and len(a) > 2 and "--limit".startswith(a):
            i += 2  # --limit takes a value
        elif a.startswith("-") and len(a) > 1:
            i += 1
        else:
            break
    return "why#name" if i < len(args) and "#" in args[i] else sub


def tool_kind(name: str, command: str | None = None) -> str:
    """The kind of a tool call: a Bash call that runs `ctx` is `ctx`, not `shell`."""
    if name == "Bash" and command and ctx_invocations(command):
        return "ctx"
    if name.startswith("mcp__"):
        return "mcp"
    return KINDS.get(name, "other")
