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
class Turn:
    index: int
    prompt: str  # a short preview; redact before writing anywhere
    started_at: str | None = None
    ended_at: str | None = None
    tool_calls: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, int] | None = None


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
