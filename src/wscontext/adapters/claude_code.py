"""Claude Code transcript adapter. The format is internal to Claude Code and unversioned
by contract, so only the `version` field of known major versions is trusted."""

import json
from collections.abc import Iterable, Sequence
from pathlib import Path

from wscontext.adapters.base import Event, ParsedTranscript, SubagentRef, Turn

KNOWN_MAJOR_VERSIONS = frozenset({1, 2})
PROMPT_PREVIEW = 200
TOKEN_KEYS = {"input_tokens": "input", "output_tokens": "output",
              "cache_read_input_tokens": "cache_read",
              "cache_creation_input_tokens": "cache_creation"}


def _loads(line: str) -> dict | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _prompt_text(message: dict) -> str | None:
    """The text of a human prompt, or None when the message is a tool result or not text."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts) if texts else None
    return None


def _major(version: object) -> int | None:
    if isinstance(version, str) and version.split(".")[0].isdigit():
        return int(version.split(".")[0])
    return None


class ClaudeCodeAdapter:
    def detect(self, lines: Sequence[str]) -> bool:
        for line in lines:
            obj = _loads(line)
            if obj and obj.get("type") in {"user", "assistant", "summary", "system"} \
                    and ("message" in obj or "sessionId" in obj or "leafUuid" in obj):
                return True
        return False

    def parse(self, path: Path) -> ParsedTranscript:
        text = path.read_bytes().decode("utf-8", errors="replace")
        out = ParsedTranscript()
        objs: list[dict] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            obj = _loads(line)
            if obj is None:
                out.skipped_lines += 1
            else:
                objs.append(obj)
        for obj in objs:
            if isinstance(obj.get("version"), str):
                out.format_version = obj["version"]
                break
        out.supported = _major(out.format_version) in KNOWN_MAJOR_VERSIONS
        if not out.supported:
            return out
        out.events = [Event(str(o.get("type", "")), o.get("timestamp")) for o in objs]
        out.turns = self._turns(objs)
        out.subagents = self._subagents(path, objs)
        return out

    def _turns(self, objs: Iterable[dict]) -> list[Turn]:
        turns: list[Turn] = []
        usage: dict[str, dict[str, int]] = {}  # message key -> usage, last write wins
        usages: list[dict[str, dict[str, int]]] = []
        for n, obj in enumerate(objs):
            if obj.get("isSidechain") or not isinstance(obj.get("message"), dict):
                continue
            message = obj["message"]
            if obj.get("type") == "user" and not obj.get("isMeta"):
                prompt = _prompt_text(message)
                if prompt is not None:
                    usage = {}
                    usages.append(usage)
                    turns.append(Turn(index=len(turns), prompt=prompt[:PROMPT_PREVIEW],
                                      started_at=obj.get("timestamp")))
                    continue
            if obj.get("type") == "assistant" and turns:
                turn = turns[-1]
                turn.ended_at = obj.get("timestamp") or turn.ended_at
                content = message.get("content")
                for block in content if isinstance(content, list) else ():
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = str(block.get("name", "unknown"))
                        turn.tool_calls[name] = turn.tool_calls.get(name, 0) + 1
                raw = message.get("usage")
                if isinstance(raw, dict):
                    # a message split over several lines repeats its usage; count it once
                    usage[str(message.get("id", f"line-{n}"))] = {
                        short: raw[key] for key, short in TOKEN_KEYS.items()
                        if isinstance(raw.get(key), int)}
        for turn, per_message in zip(turns, usages):
            if per_message:
                total: dict[str, int] = {}
                for counts in per_message.values():
                    for key, value in counts.items():
                        total[key] = total.get(key, 0) + value
                turn.tokens = total
        return turns

    def _subagents(self, path: Path, objs: Iterable[dict]) -> list[SubagentRef]:
        ids: list[str] = []
        for obj in objs:
            result = obj.get("toolUseResult")
            agent_id = result.get("agentId") if isinstance(result, dict) else obj.get("agentId")
            if isinstance(agent_id, str) and agent_id and agent_id not in ids:
                ids.append(agent_id)
        refs = []
        for agent_id in ids:
            candidates = [path.parent / path.stem / "subagents" / f"agent-{agent_id}.jsonl",
                          path.parent / f"agent-{agent_id}.jsonl"]
            found = next((c for c in candidates if c.is_file()), None)
            refs.append(SubagentRef(agent_id, found))
        return refs
