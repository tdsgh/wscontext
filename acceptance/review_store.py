"""Stored-transcript review: sizes by entry kind and by field, never a value.

    .venv/bin/python acceptance/review_store.py [--store DIR] [--source=PROJECT ...]

Measures every session copy, PreCompact snapshot and subagent copy of a store (default: the
current repository's local store) with the adapter's `entry_kind` and `measure`. Source
transcripts under `~/.claude/projects/<PROJECT>/` are read only when `--source` names them.
Project directory names start with `-`, so pass them as `--source=PROJECT`.
Prints sizes, counts, field names, kind names and tool names; never transcript content or a
field value. Not part of the offline suite (pytest never collects `acceptance/`).
"""

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from wscontext.adapters.claude_code import TOKEN_KEYS, _result_text, entry_kind, measure
from wscontext.store.paths import StorePaths

PROJECTS = Path.home() / ".claude" / "projects"
KINDS = ("prompt", "reply", "tool_result", "meta", "subagent", "compaction", "title",
         "bookkeeping", "unknown")
CUTS = (2000, 8000, 32000)  # tool-result cut points, in characters
MIN_DUP = 16  # shortest line counted as a duplicate

# The WP1 proposal, applied only to project what a reduced copy would hold. WP3 moves the
# approved rule into the adapter.
DROP_FIELDS = frozenset({
    "wireToolInputs", "serverClassifierContext", "serverClassifierRequest", "requestRef",
    "requestId", "permissionDecision", "wireIngestContext", "sourceToolAssistantUUID",
    "rendered", "renderedInHumanTurn", "renderedRole"})
ONCE_FIELDS = frozenset({
    "sessionId", "cwd", "gitBranch", "version", "entrypoint", "userType", "slug",
    "requestedModel", "advisorModel", "effort", "perTurnEffort", "thinkingDisplay",
    "permissionMode"})
ORIGIN_DROP = ("body", "handbackReportText")


def files_of(store: Path, sources: list[Path]) -> dict[str, list[tuple[Path, bool]]]:
    """Group name -> [(path, sidechain)]."""
    groups: dict[str, list[tuple[Path, bool]]] = defaultdict(list)
    for p in sorted((store / "raw" / "sessions").glob("*.jsonl")):
        groups["snapshot" if "-pc" in p.name else "session"].append((p, False))
    for p in sorted((store / "raw" / "subagents").glob("*/*.jsonl")):
        groups["subagent copy"].append((p, True))
    for project in sources:
        for p in sorted(project.glob("*.jsonl")):
            groups["source session"].append((p, False))
        for p in sorted(project.glob("*/subagents/*.jsonl")):
            groups["source subagent"].append((p, True))
    return groups


def lines_of(path: Path):
    for line in path.read_bytes().splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield line, obj


def nbytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


def dup_bytes(value: object, reference: object) -> int:
    """Bytes of the lines of value's string leaves that occur verbatim in reference's."""
    text = "\0".join(strings(reference))
    return sum(len(line.encode()) for s in strings(value) for line in s.split("\n")
               if len(line) >= MIN_DUP and line in text)


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:5.1f}%" if whole else "    -"


def table(title: str, header: tuple, rows: list[tuple]) -> None:
    print(f"\n## {title}\n")
    print(" | ".join(str(h) for h in header))
    print(" | ".join("---" for _ in header))
    for row in rows:
        print(" | ".join(str(c) for c in row))


def percentile(values: list[int], p: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, -(-p * len(ordered) // 100) - 1))]


def review_group(name: str, files: list[tuple[Path, bool]]) -> None:
    sizes: Counter = Counter()
    for path, sidechain in files:
        sizes.update(measure(path, sidechain))
    total = sum(sizes[k] for k in KINDS)
    print(f"\n# {name}: {len(files)} files, {total:,} bytes")
    table(f"{name}: bytes by kind", ("kind", "lines", "bytes", "share"),
          [(k, sizes[f"{k} lines"], f"{sizes[k]:,}", pct(sizes[k], total))
           for k in KINDS if sizes[f"{k} lines"]])

    # distinct values per field and per file; the most any one file holds
    distinct: dict[tuple[str, str], int] = Counter()
    present: Counter = Counter()
    attachments: Counter = Counter()
    for path, sidechain in files:
        seen: dict[tuple[str, str], set] = defaultdict(set)
        for line, obj in lines_of(path):
            kind = entry_kind(obj, sidechain)
            if obj.get("type") == "attachment" and isinstance(obj.get("attachment"), dict):
                attachments[(kind, str(obj["attachment"].get("type")))] += len(line) + 1
            if kind == "bookkeeping":
                continue
            for key, value in obj.items():
                present[(kind, key)] += 1
                seen[(kind, key)].add(hashlib.sha1(
                    json.dumps(value, sort_keys=True).encode()).digest())
        for k, v in seen.items():
            distinct[k] = max(distinct[k], len(v))
    rows = []
    for kind in KINDS:
        if kind == "bookkeeping" or not sizes[kind]:
            continue
        fields = sorted(((k.split(".", 1)[1], v) for k, v in sizes.items()
                         if k.startswith(f"{kind}.")), key=lambda kv: -kv[1])
        rows += [(kind, f, f"{v:,}", pct(v, sizes[kind]), pct(v, total),
                  present[(kind, f)], distinct[(kind, f)]) for f, v in fields]
    table(f"{name}: top-level fields of every kind but bookkeeping",
          ("kind", "field", "bytes", "of kind", "of all", "lines", "max distinct per file"),
          rows)
    if attachments:
        table(f"{name}: attachment lines by attachment type", ("kind", "type", "bytes"),
              [(k, t, f"{v:,}") for (k, t), v in attachments.most_common()])
    review_details(name, files, total)


def review_details(name: str, files: list[tuple[Path, bool]], total: int) -> None:
    message: Counter = Counter()
    usage_keys: Counter = Counter()
    pairs: Counter = Counter()
    results: list[tuple[str, int, int]] = []  # (tool, characters read, bytes stored)
    tur_by_tool: Counter = Counter()
    usage_lines: list[tuple[str, int, int]] = []  # (message id, usage bytes, token-key bytes)
    for path, sidechain in files:
        queued: set[str] = set()
        tool_uses: dict[str, dict] = {}  # tool_use id -> its block, for tool_result lines
        for _, obj in lines_of(path):
            kind = entry_kind(obj, sidechain)
            msg = obj.get("message")
            if kind in ("reply", "tool_result", "prompt", "meta", "compaction") \
                    and isinstance(msg, dict):
                for key, value in msg.items():
                    message[(kind, f"message.{key}")] += nbytes(value)
                content = msg.get("content")
                for block in content if isinstance(content, list) else ():
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    for key, value in block.items():
                        message[(kind, f"content[{btype}].{key}")] += nbytes(value)
                    if btype == "tool_use" and isinstance(block.get("id"), str):
                        tool_uses[block["id"]] = block
            if kind == "reply" and isinstance(msg, dict):
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    for key, value in usage.items():
                        usage_keys[key] += nbytes(value)
                    mid = str(msg.get("id"))
                    usage_lines.append((mid, nbytes(usage),
                                        nbytes({k: usage[k] for k in TOKEN_KEYS if k in usage})))
                wire = obj.get("wireToolInputs")
                if isinstance(wire, dict):
                    inputs = {b.get("id"): b.get("input") for b in msg.get("content") or ()
                              if isinstance(b, dict) and b.get("type") == "tool_use"}
                    for tid, value in wire.items():
                        n = nbytes(value)
                        pairs["wireToolInputs bytes"] += n
                        if inputs.get(tid) == value:
                            pairs["wireToolInputs equal to tool_use.input"] += n
                        else:
                            pairs["wireToolInputs contained in tool_use.input"] += dup_bytes(
                                value, inputs.get(tid))
            if kind == "tool_result" and isinstance(msg, dict):
                text = ""
                tool = "unknown"
                calls = []
                for block in msg.get("content") or ():
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        body = _result_text(block.get("content"))
                        text += body
                        call = tool_uses.get(block.get("tool_use_id"), {})
                        calls.append(call.get("input"))
                        tool = str(call.get("name", "unknown"))
                        results.append((tool, len(body), nbytes(block.get("content"))))
                result = obj.get("toolUseResult")
                if result is not None:
                    n = nbytes(result)
                    tur_by_tool[tool] += n
                    pairs["toolUseResult bytes"] += n
                    pairs["toolUseResult contained in tool_result"] += dup_bytes(result, text)
                    pairs["toolUseResult contained in tool_result or tool_use.input"] += \
                        dup_bytes(result, [text, calls])
                    pairs["tool_result bytes on those lines"] += sum(
                        nbytes(b.get("content")) for b in msg.get("content") or ()
                        if isinstance(b, dict) and b.get("type") == "tool_result")
            if obj.get("type") == "queue-operation" and obj.get("content"):
                queued.add(json.dumps(obj["content"]))
            if kind == "meta" and isinstance(obj.get("origin"), dict) and isinstance(msg, dict):
                pairs["origin bytes"] += nbytes(obj["origin"])
                pairs["origin contained in message"] += dup_bytes(obj["origin"], msg)
                origin = obj["origin"]
                pairs["origin: handbackReportText contained in body"] += dup_bytes(
                    origin.get("handbackReportText"), origin.get("body"))
            if obj.get("type") == "attachment" and "rendered" in obj and kind != "bookkeeping":
                rendered = obj["rendered"]
                pairs["kept attachments: rendered bytes"] += nbytes(rendered)
                pairs["kept attachments: rendered contained in attachment"] += dup_bytes(
                    rendered, obj.get("attachment"))
                pairs["kept attachments: attachment contained in rendered"] += dup_bytes(
                    obj.get("attachment"), rendered)
                if "renderedInHumanTurn" in obj:
                    pairs["kept attachments: renderedInHumanTurn bytes"] += nbytes(
                        obj["renderedInHumanTurn"])
                    pairs["kept attachments: renderedInHumanTurn contained in rendered"] += \
                        dup_bytes(obj["renderedInHumanTurn"], [rendered, obj.get("attachment")])
        if queued:
            texts = [json.loads(q) for q in queued]
            kept = [o for _, o in lines_of(path) if entry_kind(o, sidechain) != "bookkeeping"]
            pairs["queue-operation content, distinct"] += sum(
                len(t.encode()) for t in strings(texts))
            pairs["queue-operation content, distinct, contained in kept lines"] += dup_bytes(
                texts, kept)
    if message:
        table(f"{name}: inside `message` and its content blocks",
              ("kind", "path", "bytes", "of all"),
              [(k, p, f"{v:,}", pct(v, total)) for (k, p), v in message.most_common()
               if v >= 1000])
    if usage_lines:
        last: dict[str, int] = {mid: i for i, (mid, _, _) in enumerate(usage_lines)}
        repeated = sum(u for i, (mid, u, _) in enumerate(usage_lines) if last[mid] != i)
        table(f"{name}: reply usage", ("measure", "value"), [
            ("reply lines with usage", len(usage_lines)),
            ("distinct message ids", len(last)),
            ("usage bytes", f"{sum(u for _, u, _ in usage_lines):,}"),
            ("usage bytes on lines that are not the last of their message",
             f"{repeated:,}"),
            ("bytes of the four token keys the adapter reads",
             f"{sum(t for _, _, t in usage_lines):,}"),
        ] + [(f"usage key {k}", f"{v:,}") for k, v in usage_keys.most_common()])
    if pairs:
        table(f"{name}: duplicated content", ("measure", "bytes"),
              [(k, f"{v:,}") for k, v in sorted(pairs.items())])
    if results:
        chars = [c for _, c, _ in results]
        stored = sum(b for _, _, b in results)
        rows = [("tool_result blocks", len(results)),
                ("bytes of their content", f"{stored:,}"),
                ("characters read: p50 / p90 / p99 / max",
                 " / ".join(f"{percentile(chars, p):,}" for p in (50, 90, 99, 100)))]
        for cut in CUTS:
            over = [c for c in chars if c > cut]
            rows.append((f"blocks over {cut:,} characters / characters beyond the cut",
                         f"{len(over)} / {sum(c - cut for c in over):,}"))
        table(f"{name}: tool results", ("measure", "value"), rows)
        by_tool: dict[str, list[int]] = defaultdict(list)
        for tool, c, b in results:
            by_tool[tool].append(b)
        table(f"{name}: tool results by tool",
              ("tool", "blocks", "content bytes", "max", "toolUseResult bytes"),
              [(t, len(v), f"{sum(v):,}", f"{max(v):,}", f"{tur_by_tool[t]:,}")
               for t, v in sorted(by_tool.items(), key=lambda kv: -sum(kv[1]))])


def reduce(obj: dict, kind: str, last: dict) -> dict | None:
    """The proposed reduced line, or None when it is dropped. `last` carries the values
    already kept in this file: a keep-once field is written again only when it changes."""
    if kind == "bookkeeping":
        return None
    if kind == "title":
        title = obj.get("aiTitle", obj.get("summary"))
        if "\0title" in last and last["\0title"] == title:
            return None
        last["\0title"] = title
    out = {}
    for key, value in obj.items():
        if key in DROP_FIELDS or (key in ONCE_FIELDS and key in last and last[key] == value):
            continue
        if key in ONCE_FIELDS:
            last[key] = value
        out[key] = value
    result = out.pop("toolUseResult", None)
    if isinstance(result, dict) and "agentId" in result:
        out["toolUseResult"] = {"agentId": result["agentId"]}
    if kind == "meta" and isinstance(out.get("origin"), dict):
        out["origin"] = {k: v for k, v in out["origin"].items() if k not in ORIGIN_DROP}
    if isinstance(out.get("message"), dict):
        msg = out["message"] = dict(out["message"])
        if isinstance(msg.get("usage"), dict):
            msg["usage"] = {k: v for k, v in msg["usage"].items() if k in TOKEN_KEYS}
        if isinstance(msg.get("content"), list):
            msg["content"] = [{k: v for k, v in b.items() if k != "signature"}
                              if isinstance(b, dict) and b.get("type") == "thinking" else b
                              for b in msg["content"]]
    return out


def review_projection(name: str, files: list[tuple[Path, bool]]) -> None:
    by_kind: Counter = Counter()
    rows = []
    for path, sidechain in files:
        last: dict = {}
        now = path.stat().st_size
        kept = 0
        for line, obj in lines_of(path):
            kind = entry_kind(obj, sidechain)
            reduced = reduce(obj, kind, last)
            if reduced is not None:
                n = len(line) + 1 - nbytes(obj) + nbytes(reduced)
                kept += n
                by_kind[kind] += n
        rows.append((path.name[:19] + "...", f"{now:,}", f"{kept:,}", pct(kept, now)))
    current = sum(path.stat().st_size for path, _ in files)
    projected = sum(by_kind.values())
    rows.append(("all", f"{current:,}", f"{projected:,}", pct(projected, current)))
    table(f"{name}: projected under the WP1 proposal",
          ("file", "bytes now", "bytes projected", "share"), rows)
    table(f"{name}: projected bytes by kind", ("kind", "bytes", "share of projected"),
          [(k, f"{v:,}", pct(v, projected)) for k, v in by_kind.most_common()])


def review_snapshots(store: Path) -> None:
    rows = []
    for snap in sorted((store / "raw" / "sessions").glob("*-pc*-*.jsonl")):
        sid = snap.name.split("-pc", 1)[1].split("-", 1)[1].removesuffix(".jsonl")
        copies = sorted((store / "raw" / "sessions").glob(f"????-??-??-{sid}.jsonl"))
        if not copies:
            rows.append((snap.name, "no session copy", "", "", ""))
            continue
        copy_lines = set(copies[0].read_bytes().splitlines())
        copy_uuids = {obj.get("uuid") for _, obj in lines_of(copies[0]) if obj.get("uuid")}
        lines = snap.read_bytes().splitlines()
        exact = sum(1 for line in lines if line in copy_lines)
        by_uuid = sum(1 for line, obj in lines_of(snap)
                      if line not in copy_lines and obj.get("uuid") in copy_uuids)
        rows.append((snap.name[:24] + "...", len(lines), exact, by_uuid,
                     len(lines) - exact - by_uuid))
    if rows:
        table("PreCompact snapshots against the session copy",
              ("snapshot", "lines", "identical line in copy", "same uuid, line differs",
               "missing from copy"), rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--store", type=Path, help="local store directory (default: this repo's)")
    parser.add_argument("--source", action="append", default=[], metavar="PROJECT",
                        help=f"also read source transcripts under {PROJECTS}/PROJECT/; "
                             "write --source=PROJECT, since project names start with '-'")
    args = parser.parse_args(argv)
    store = args.store or StorePaths.resolve(Path.cwd()).local
    sources = [p if p.is_absolute() else PROJECTS / p for p in map(Path, args.source)]
    for p in sources:
        if not p.is_dir():
            parser.error(f"no such project directory: {p}")
    groups = files_of(store, sources)
    for name in ("session", "subagent copy", "snapshot", "source session", "source subagent"):
        if groups.get(name):
            review_group(name, groups[name])
    for name in ("session", "subagent copy", "source session", "source subagent"):
        if groups.get(name):
            review_projection(name, groups[name])
    review_snapshots(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
