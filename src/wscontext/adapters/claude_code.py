"""Claude Code transcript adapter. The format is internal to Claude Code and unversioned
by contract, so only the `version` field of known major versions is trusted."""

import json
import re
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
# conversation: API request records, queue and file-history entries, listings and snapshots,
# account and cost state, and per-hook run records.
BOOKKEEPING_PREFIXES = ("api-request", "file-history-")
BOOKKEEPING_TYPES = frozenset({"queue-operation", "last-prompt", "atis-latch", "tools-used",
                               "session-owner", "cost-state"})
BOOKKEEPING_ATTACHMENTS = frozenset({
    "prompt_snapshot", "skill_listing", "deferred_tools_record", "deferred_tools_delta",
    "agent_listing_delta", "mcp_instructions_delta", "instructions", "nested_memory",
    "invoked_skills", "session_context", "environment", "model", "date", "auto_mode",
    "credential_org", "command_permissions", "remote_session_change", "silent_turn_reminder",
    "thinking_drop", "budget_usd", "hook_success"})
META_ATTACHMENTS = frozenset({"queued_command", "file", "edited_text_file",
                              "hook_additional_context"})
TITLE_TYPES = frozenset({"ai-title", "summary"})

# The stored shape (build plan WP1 gate). A field not named here is kept.
# Keep once: stored on the first line that carries the field and again when its value changes.
KEEP_ONCE = ("sessionId", "cwd", "gitBranch", "version", "entrypoint", "userType", "slug",
             "requestedModel", "advisorModel", "effort", "perTurnEffort", "thinkingDisplay",
             "permissionMode")
REDUCED_KINDS = frozenset({"prompt", "reply", "tool_result", "meta", "compaction"})
DROPPED_FIELDS = {
    "reply": ("wireToolInputs", "serverClassifierRequest", "requestRef", "requestId",
              "wireIngestContext"),
    "tool_result": ("serverClassifierContext", "permissionDecision", "sourceToolAssistantUUID"),
    "meta": ("rendered", "renderedInHumanTurn", "renderedRole"),
}
DROPPED_ORIGIN_KEYS = ("body", "handbackReportText")  # on meta lines
CUT_MARK = "\n[wscontext: tool result cut at {kept} of {total} characters]"
_CUT_MARK = re.compile(r"\n\[wscontext: tool result cut at \d+ of \d+ characters\]\Z")


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


def _cut(block: dict, max_chars: int) -> bool:
    """Cut a tool_result block's text to `max_chars`, ending it with a visible mark."""
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        texts = [b for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
        text = "\n".join(b["text"] for b in texts)
    else:
        return False
    if len(text) <= max_chars or _CUT_MARK.search(text):
        return False
    kept = text[:max_chars] + CUT_MARK.format(kept=max_chars, total=len(text))
    if isinstance(content, str):
        block["content"] = kept
    else:
        # the text blocks become one, where the first was; other blocks stay in place
        first = texts[0]
        block["content"] = [{**b, "text": kept} if b is first else b
                            for b in content if b is first or b not in texts]
    return True


def _reduce_entry(obj: dict, kind: str, max_chars: int) -> bool:
    """Drop the fields the stored shape leaves out of one line of `kind`; True when it changed."""
    changed = False
    for key in DROPPED_FIELDS.get(kind, ()):
        if key in obj:
            del obj[key]
            changed = True
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    if kind == "reply":
        for block in blocks:
            if block.get("type") == "thinking" and "signature" in block:
                del block["signature"]
                changed = True
        usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict) and set(usage) - set(TOKEN_KEYS):
            message["usage"] = {k: v for k, v in usage.items() if k in TOKEN_KEYS}
            changed = True
    elif kind == "tool_result":
        if "toolUseResult" in obj:
            result = obj["toolUseResult"]
            # The adapter reads only `agentId` here. An object is kept, even empty, on a line
            # with its own `agentId`: without it the line's own id would be read instead.
            if isinstance(result, dict) and ("agentId" in result or "agentId" in obj):
                slim = {"agentId": result["agentId"]} if "agentId" in result else {}
                if slim != result:
                    obj["toolUseResult"] = slim
                    changed = True
            else:
                del obj["toolUseResult"]
                changed = True
        if max_chars:
            for block in blocks:
                if block.get("type") == "tool_result" and _cut(block, max_chars):
                    changed = True
    elif kind == "meta":
        origin = obj.get("origin")
        for key in DROPPED_ORIGIN_KEYS if isinstance(origin, dict) else ():
            if key in origin:
                del origin[key]
                changed = True
    return changed


def reduce(data: bytes, tool_result_max_chars: int = 0) -> bytes:
    """The stored shape of a transcript, line by line: bookkeeping and repeated titles go,
    named fields go, keep-once fields stay where they first appear or change. Each tool
    result's text is cut at `tool_result_max_chars` unless it is 0. Unparseable lines,
    unknown kinds and unnamed fields pass through; unchanged lines keep their bytes.

    Parsing the result gives the same turns, items, tokens and subagents as the original.
    Each output line depends only on the lines before it, so a prefix of a transcript
    reduces to a prefix of its reduced copy. Sidechain lines are reduced by their shape."""
    out: list[bytes] = []
    stored: dict[str, object] = {}  # keep-once field -> the value last written
    titles: dict[object, dict] = {}  # title line type -> the title line last written
    for line in data.splitlines(keepends=True):
        body = line.rstrip(b"\r\n")
        try:
            obj = json.loads(body)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            out.append(line)
            continue
        kind = entry_kind(obj, sidechain=True)
        changed = False
        if kind == "bookkeeping":
            continue
        if kind == "title":
            title = {k: v for k, v in obj.items() if k not in KEEP_ONCE}
            if titles.get(obj["type"]) == title:
                continue
            titles[obj["type"]] = title
        elif kind in REDUCED_KINDS:
            changed = _reduce_entry(obj, kind, tool_result_max_chars)
            for key in KEEP_ONCE:
                if key in obj and key in stored and stored[key] == obj[key]:
                    del obj[key]
                    changed = True
        for key in KEEP_ONCE:
            if key in obj:
                stored[key] = obj[key]
        if changed:
            line = json.dumps(obj, ensure_ascii=False,
                              separators=(",", ":")).encode() + line[len(body):]
        out.append(line)
    return b"".join(out)


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
