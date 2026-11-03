"""`ctx uninstall`: the reverse of `ctx init` for this repository. Captured data is kept."""

from wscontext.cli.init import remove_block
from wscontext.cli.output import Reply
from wscontext.config import Config
from wscontext.githooks.install import uninstall
from wscontext.store.paths import StorePaths
from wscontext.store.records import atomic_write


def add_arguments(parser) -> None:
    pass


def run(args, store: StorePaths, config: Config) -> Reply:
    lines = []
    done = uninstall(store.root)
    for hook, what in done.items():
        lines.append(f"Git hook {hook}: {what}")
    if not done:
        lines.append("no wscontext Git hooks found")
    claude_md = store.root / "CLAUDE.md"
    if claude_md.exists():
        before = claude_md.read_text(encoding="utf-8")
        after = remove_block(before)
        if after != before:
            if after:
                atomic_write(claude_md, after.encode("utf-8"))
            else:
                claude_md.unlink()
            lines.append("CLAUDE.md block removed")
    lines.append(f"kept: captured data in {store.local}, and .wscontext/config.toml "
                 "(committed, shared with the team)")
    return Reply("\n".join(lines))
