"""Transcript adapter contract and the parsed shape every adapter returns."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Event:
    type: str
    timestamp: str | None = None


@dataclass
class Item:
    """One entry of a turn's full content, in transcript order.

    kind is one of "prompt", "text", "tool_call", "tool_result", "subagent".
    text: the prompt, assistant text, tool input summary or tool result body.
    name: the tool name (tool_call); tool_id links a tool_call to its tool_result.
    size: original length in characters of a tool_result body; truncated says it was cut.
    agent_id: the subagent a "subagent" item refers to.
    """
    kind: str
    text: str = ""
    name: str | None = None
    tool_id: str | None = None
    size: int | None = None
    truncated: bool = False
    agent_id: str | None = None


@dataclass
class Turn:
    index: int
    prompt: str  # a short preview; redact before writing anywhere
    started_at: str | None = None
    ended_at: str | None = None
    tool_calls: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, int] | None = None
    # full content in order; the first item is the whole prompt. Built from the store's
    # already-redacted raw copy. Callers render it, never persist it.
    items: list[Item] = field(default_factory=list)


@dataclass
class SubagentRef:
    agent_id: str
    path: Path | None  # None when the transcript file was not found


@dataclass
class ParsedTranscript:
    format_version: str | None = None
    supported: bool = False
    events: list[Event] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    subagents: list[SubagentRef] = field(default_factory=list)
    skipped_lines: int = 0


class TranscriptAdapter(Protocol):
    def detect(self, lines: Sequence[str]) -> bool: ...

    def parse(self, path: Path) -> ParsedTranscript: ...
