"""Claude Code transcript adapter. The format is internal to Claude Code and unversioned
by contract, so only the `version` field of known major versions is trusted."""

import json
from collections.abc import Iterable, Sequence
from pathlib import Path

from wscontext.adapters.base import Event, Item, ParsedTranscript, SubagentRef, Turn

KNOWN_MAJOR_VERSIONS = frozenset({1, 2})
PROMPT_PREVIEW = 200
RESULT_LIMIT = 2000  # characters of a tool result kept in an Item
INPUT_VALUE_LIMIT = 80
TOKEN_KEYS = {"input_tokens": "input", "output_tokens": "output",
              "cache_read_input_tokens": "cache_read",
              "cache_creation_input_tokens": "cache_creation"}

# Line types and attachment types that hold Claude Code's internal state, not the
# conversation: API request records, queue and file-history entries, listings and snapshots.
BOOKKEEPING_PREFIXES = ("api-request", "file-history-")
BOOKKEEPING_TYPES = frozenset({"queue-operation", "last-prompt", "atis-latch", "tools-used"})
BOOKKEEPING_ATTACHMENTS = frozenset({
    "prompt_snapshot", "skill_listing", "deferred_tools_record", "deferred_tools_delta",
    "agent_listing_delta", "mcp_instructions_delta", "instructions", "nested_memory",
    "invoked_skills", "session_context", "environment", "model", "date", "auto_mode",
    "credential_org", "command_permissions", "remote_session_change", "silent_turn_reminder",
    "thinking_drop"})
META_ATTACHMENTS = frozenset({"queued_command", "file", "edited_text_file",
                              "hook_additional_context"})
TITLE_TYPES = frozenset({"ai-title", "summary"})


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


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _input_summary(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    parts = []
    for key, val in value.items():
        shown = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        shown = shown.replace("\n", " ")
        if len(shown) > INPUT_VALUE_LIMIT:
            shown = shown[:INPUT_VALUE_LIMIT] + "..."
        parts.append(f"{key}={shown}")
    return ", ".join(parts)


def _major(version: object) -> int | None:
    if isinstance(version, str) and version.split(".")[0].isdigit():
        return int(version.split(".")[0])
    return None


def entry_kind(obj: dict, sidechain: bool = False) -> str:
    """The kind of one transcript line: prompt, reply, tool_result, meta, subagent,
    compaction, title, bookkeeping or unknown. A type or attachment type not named here is
    unknown. sidechain=True classifies a subagent file's lines by shape, as `parse` does;
    in a main transcript a sidechain line is `subagent`."""
    kind = obj.get("type")
    if not isinstance(kind, str):
        return "unknown"
    if kind in BOOKKEEPING_TYPES or kind.startswith(BOOKKEEPING_PREFIXES):
        return "bookkeeping"
    if kind in TITLE_TYPES:
        return "title"
    if obj.get("isSidechain") and not sidechain:
        return "subagent"
    if kind == "assistant":
        return "reply"
    if kind == "user":
        message = obj.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if obj.get("isCompactSummary"):
            return "compaction"
        if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return "tool_result"
        return "meta" if obj.get("isMeta") else "prompt"
    if kind == "system":
        return "compaction" if obj.get("subtype") == "compact_boundary" else "meta"
    if kind == "attachment":
        attachment = obj.get("attachment")
        sub = attachment.get("type") if isinstance(attachment, dict) else None
        if sub in BOOKKEEPING_ATTACHMENTS:
            return "bookkeeping"
        if sub in META_ATTACHMENTS:
            return "meta"
    return "unknown"


def measure(path: Path, sidechain: bool = False) -> dict[str, int]:
    """Sizes of a transcript file by entry kind; reads only and returns no content.

    `<kind>` is the bytes of its lines (newline included), `<kind> lines` their count and
    `<kind>.<field>` the bytes of one top-level field as `"field":value`, for every kind
    but bookkeeping. A line that is not a JSON object counts as unknown."""
    sizes: dict[str, int] = {}
    for line in path.read_bytes().splitlines(keepends=True):
        obj = _loads(line.decode("utf-8", errors="replace"))
        kind = "unknown" if obj is None else entry_kind(obj, sidechain)
        sizes[kind] = sizes.get(kind, 0) + len(line)
        sizes[f"{kind} lines"] = sizes.get(f"{kind} lines", 0) + 1
        if obj is None or kind == "bookkeeping":
            continue
        for key, value in obj.items():
            n = len(json.dumps({key: value}, ensure_ascii=False,
                               separators=(",", ":")).encode()) - 2
            sizes[f"{kind}.{key}"] = sizes.get(f"{kind}.{key}", 0) + n
    return sizes


class ClaudeCodeAdapter:
    """Parses a Claude Code transcript from the store's raw copy, which is already
    redacted. Reads only, writes nothing: callers render `Turn.items`, never persist them."""

    def detect(self, lines: Sequence[str]) -> bool:
        for line in lines:
            obj = _loads(line)
            if obj and obj.get("type") in {"user", "assistant", "summary", "system"} \
                    and ("message" in obj or "sessionId" in obj or "leafUuid" in obj):
                return True
        return False

    def parse(self, path: Path, sidechain: bool = False) -> ParsedTranscript:
        """sidechain=True parses a subagent file: every line belongs to it, so none is
        skipped as a sidechain line. The main session (default) skips sidechain lines."""
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
        out.turns = self._turns(objs, sidechain)
        out.subagents = self._subagents(path, objs, sidechain)
        return out

    def _turns(self, objs: Iterable[dict], sidechain: bool = False) -> list[Turn]:
        turns: list[Turn] = []
        usage: dict[str, dict[str, int]] = {}  # message key -> usage, last write wins
        usages: list[dict[str, dict[str, int]]] = []
        for n, obj in enumerate(objs):
            if (obj.get("isSidechain") and not sidechain) or not isinstance(obj.get("message"), dict):
                continue
            message = obj["message"]
            if obj.get("type") == "user" and not obj.get("isMeta"):
                prompt = _prompt_text(message)
                if prompt is not None:
                    usage = {}
                    usages.append(usage)
                    turns.append(Turn(index=len(turns), prompt=prompt[:PROMPT_PREVIEW],
                                      started_at=obj.get("timestamp"),
                                      items=[Item("prompt", prompt)]))
                    continue
            if obj.get("type") == "user" and turns:
                self._result_items(obj, message, turns[-1])
            if obj.get("type") == "assistant" and turns:
                turn = turns[-1]
                turn.ended_at = obj.get("timestamp") or turn.ended_at
                content = message.get("content")
                for block in content if isinstance(content, list) else ():
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        turn.items.append(Item("text", block["text"]))
                    elif block.get("type") == "tool_use":
                        name = str(block.get("name", "unknown"))
                        turn.tool_calls[name] = turn.tool_calls.get(name, 0) + 1
                        turn.items.append(Item("tool_call", _input_summary(block.get("input")),
                                               name=name, tool_id=block.get("id")))
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

    def _result_items(self, obj: dict, message: dict, turn: Turn) -> None:
        content = message.get("content")
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and block.get("type") == "tool_result":
                body = _result_text(block.get("content"))
                turn.items.append(Item("tool_result", body[:RESULT_LIMIT],
                                       tool_id=block.get("tool_use_id"), size=len(body),
                                       truncated=len(body) > RESULT_LIMIT))
        result = obj.get("toolUseResult")
        if isinstance(result, dict) and isinstance(result.get("agentId"), str):
            turn.items.append(Item("subagent", agent_id=result["agentId"]))

    def _subagents(self, path: Path, objs: Iterable[dict],
                   sidechain: bool = False) -> list[SubagentRef]:
        ids: list[str] = []
        for obj in objs:
            result = obj.get("toolUseResult")
            if isinstance(result, dict):
                agent_id = result.get("agentId")
            else:  # a subagent file carries its own id on every line
                agent_id = None if sidechain else obj.get("agentId")
            if isinstance(agent_id, str) and agent_id and agent_id not in ids:
                ids.append(agent_id)
        refs = []
        for agent_id in ids:
            candidates = [path.parent / path.stem / "subagents" / f"agent-{agent_id}.jsonl",
                          path.parent / f"agent-{agent_id}.jsonl"]
            found = next((c for c in candidates if c.is_file()), None)
            refs.append(SubagentRef(agent_id, found))
        return refs
