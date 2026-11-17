"""`ctx eval`: score search against a fixed corpus and question set.

The corpus is indexed into a temporary store; the repository's own store is never touched. A
`repo.bundle` beside the corpus is cloned into that store first, so linked commit messages are
indexed and questions whose answer is a commit can be scored. `[[prompt]]` tables, when present,
are scored against the prompt brief instead: the expected session must be among the sessions it
names, and a prompt without `expect` must get none.
"""

import json
import shutil
import tempfile
import tomllib
from pathlib import Path

from wscontext.cli.output import Block, Reply, error, render
from wscontext.config import Config
from wscontext.gitutil import GitError, git
from wscontext.index import build
from wscontext.index.db import connect
from wscontext.index.search import prompt_hits, search
from wscontext.store.paths import StorePaths

RANK_DEPTH = 20
TOP = 3
DEFAULT_DIR = Path("eval") / "stage2"
BUNDLE = "repo.bundle"


def add_arguments(parser) -> None:
    parser.add_argument("--corpus", help="directory holding raw/ and records/, with an optional "
                                         f"{BUNDLE} beside it for commit messages "
                                         f"(default: {(DEFAULT_DIR / 'corpus').as_posix()})")
    parser.add_argument("--questions", help="TOML file of [[question]] tables with `text` and "
                                            f"`expect` (default: {(DEFAULT_DIR / 'questions.toml').as_posix()})")
    parser.add_argument("--min", type=int, dest="minimum", help="exit 1 when fewer questions land in the top 3")


def _temp_store(root: Path) -> StorePaths:
    local = root / "wscontext"
    return StorePaths(root=root, committed=root / ".wscontext", local=local, raw=local / "raw",
                      records=local / "records", index=local / "index", state=local / "state",
                      logs=local / "logs")


def _stored_share(records: Path) -> tuple[int, int]:
    """(stored, source) bytes summed over the session records that carry the accounting."""
    stored = source = 0
    for path in records.glob("sessions/*.json"):
        try:
            sizes = json.loads(path.read_text(encoding="utf-8")).get("transcript_bytes")
        except (OSError, ValueError):
            continue
        if isinstance(sizes, dict) and sizes.get("source"):
            stored += int(sizes.get("stored", 0))
            source += int(sizes["source"])
    return stored, source


def run(args, store: StorePaths, config: Config) -> Reply:
    corpus = Path(args.corpus) if args.corpus else store.root / DEFAULT_DIR / "corpus"
    questions_file = Path(args.questions) if args.questions else store.root / DEFAULT_DIR / "questions.toml"
    if not (corpus / "records").is_dir():
        return error(f"no corpus at {corpus} (expected raw/ and records/ inside)", args.json)
    try:
        tables = tomllib.loads(questions_file.read_text(encoding="utf-8"))
        pairs = [(str(q["text"]), str(q["expect"])) for q in tables.get("question", [])]
        prompts = [(str(p["text"]), str(p["expect"]) if "expect" in p else None)
                   for p in tables.get("prompt", [])]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as e:
        return error(f"cannot read questions from {questions_file}: {e}", args.json)
    if not pairs:
        return error(f"{questions_file} holds no questions", args.json)

    bundle = corpus.parent / BUNDLE
    with tempfile.TemporaryDirectory(prefix="wscontext-eval-") as tmp:
        temp = _temp_store(Path(tmp))
        if bundle.is_file():
            try:
                git("clone", "--quiet", "--no-checkout", str(bundle.resolve()), ".", cwd=temp.root)
            except GitError as e:
                return error(f"cannot clone {bundle}: {e}", args.json)
        for name in ("raw", "records"):
            if (corpus / name).is_dir():
                shutil.copytree(corpus / name, temp.local / name)
        counts = build.refresh(temp)
        conn = connect(temp, readonly=True)
        try:
            ranks = []
            for text, expect in pairs:
                hits = search(conn, text, limit=RANK_DEPTH)
                ranks.append(next((n for n, h in enumerate(hits, 1)
                                   if h.ref == expect or (h.kind == "commit" and h.ref.startswith(expect))),
                                  None))
        finally:
            conn.close()
        briefs = [[h.ref for h in prompt_hits(temp, text)] for text, _ in prompts]
        stored, source = _stored_share(temp.records)

    top = sum(1 for r in ranks if r is not None and r <= TOP)
    share = round(100 * stored / source, 1) if source else None
    body = [f"stored-to-source share: {share}%" if share is not None
            else "stored-to-source share: not recorded in this corpus"]
    if bundle.is_file():
        body.append(f"commits indexed from {BUNDLE}: {counts['commits_indexed']}")
    related = [(expect, named) for (_, expect), named in zip(prompts, briefs) if expect]
    found = sum(1 for expect, named in related if expect in named)
    unrelated = [named for (_, expect), named in zip(prompts, briefs) if not expect]
    quiet = sum(1 for named in unrelated if not named)
    if prompts:
        body.append(f"prompt brief: {found} of {len(related)} prompts name the expected session; "
                    f"{quiet} of {len(unrelated)} unrelated prompts name none")
    summary = Block("eval", "summary", f"{top} of {len(pairs)} expected answers in the top {TOP}",
                    body=body,
                    extra={"top3": top, "questions": len(pairs), "min": args.minimum,
                           "stored_bytes": stored, "source_bytes": source, "share_percent": share,
                           "commits_indexed": counts["commits_indexed"],
                           "prompts_found": found, "prompts_related": len(related),
                           "prompts_quiet": quiet, "prompts_unrelated": len(unrelated),
                           "bundle": str(bundle) if bundle.is_file() else None})
    blocks = [summary]
    for n, ((text, expect), rank) in enumerate(zip(pairs, ranks), 1):
        blocks.append(Block("question", f"q{n}", text,
                            body=[f"expected: {expect}",
                                  f"rank: {rank}" if rank else f"rank: not in the top {RANK_DEPTH}"],
                            extra={"expected": expect, "rank": rank}))
    for n, ((text, expect), named) in enumerate(zip(prompts, briefs), 1):
        rank = named.index(expect) + 1 if expect in named else None
        body = [f"expected: {expect or 'none'}",
                f"named: {', '.join(r[:8] for r in named) or 'none'}"]
        if expect:
            body.append(f"rank: {rank}" if rank else "rank: not in the brief")
        blocks.append(Block("prompt", f"p{n}", text, body=body,
                            extra={"expected": expect, "rank": rank, "named": named}))
    passed = args.minimum is None or top >= args.minimum
    note = "" if passed else f"Below the minimum: {top} of {len(pairs)} in the top {TOP}, need {args.minimum}."
    text = render("ctx eval", blocks, full=True, as_json=args.json, note=note,
                  meta={"corpus": str(corpus), "questions_file": str(questions_file)})
    return Reply(text, 0 if passed else 1, results=len(pairs), report=True)
