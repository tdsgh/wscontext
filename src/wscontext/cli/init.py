"""`ctx init`: wire a repository for wscontext. Runs only when the user asks for it.

Creates `.wscontext/config.toml`, chains the Git hooks and adds the CLAUDE.md block.
The Git hooks call a stable launcher in the plugin data directory, not the versioned
plugin cache: `<data>/plugin-root` always names the newest plugin root, because every
launcher run refreshes it, so an update does not strand the hooks.
"""

import dataclasses
import os
import sys
from pathlib import Path

from wscontext.cli.output import Reply, error
from wscontext.config import Config, load_config
from wscontext.githooks.install import install
from wscontext.gitutil import GitError
from wscontext.history.sync import fetch
from wscontext.store.paths import StorePaths
from wscontext.store.records import atomic_write

BLOCK_BEGIN = "<!-- wscontext:begin -->"
BLOCK_END = "<!-- wscontext:end -->"
CLAUDE_BLOCK = f"""{BLOCK_BEGIN}
## wscontext

Past agent sessions in this repository are captured and linked to commits.
Before changing code you did not write, run `ctx why <file>:<line>` to see the
session that produced it.
{BLOCK_END}
"""


def launcher_path(data: str | Path) -> Path:
    return Path(data) / "bin" / "ctx"


def stable_launcher_text(platform: str = sys.platform) -> str:
    target = "ctx.cmd" if platform == "win32" else "ctx"
    return ('#!/bin/sh\n'
            '# wscontext stable launcher: forwards to the plugin launcher named in ../plugin-root, which every launcher run refreshes.\n'
            f'd=$(cd "$(dirname "$0")/.." && pwd); exec "$(cat "$d/plugin-root")/bin/{target}" --plugin-data "$d" "$@"\n')


def write_stable_launcher(data: str | Path, platform: str = sys.platform) -> Path:
    path = launcher_path(data)
    atomic_write(path, stable_launcher_text(platform).encode("utf-8"))
    os.chmod(path, 0o755)
    return path


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(value)


def default_config_toml() -> str:
    """The config file for the defaults in `wscontext.config`; unset values appear as comments."""
    lines: list[str] = []
    for section in dataclasses.fields(Config):
        defaults = section.default_factory()
        lines.append(f"[{section.name}]")
        for f in dataclasses.fields(defaults):
            value = getattr(defaults, f.name)
            if value is None:
                lines.append(f"# {f.name} is unset by default" + (
                    ": nothing is pushed until a Git remote name is set (ctx sync --setup)"
                    if section.name == "sync" else ""))
            else:
                lines.append(f"{f.name} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def set_block(text: str) -> str:
    """`text` with the wscontext block added, or replaced in place when already there."""
    start, end = text.find(BLOCK_BEGIN), text.find(BLOCK_END)
    if start != -1 and end > start:
        return text[:start] + CLAUDE_BLOCK + text[end + len(BLOCK_END):].removeprefix("\n")
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text else "") + CLAUDE_BLOCK


def remove_block(text: str) -> str:
    start, end = text.find(BLOCK_BEGIN), text.find(BLOCK_END)
    if start == -1 or end < start:
        return text
    parts = [text[:start].strip("\n"), text[end + len(BLOCK_END):].strip("\n")]
    return "\n\n".join(p for p in parts if p) + ("\n" if any(parts) else "")


def add_arguments(parser) -> None:
    parser.add_argument("--launcher", help="launcher path for the Git hooks (default: the stable "
                                           "launcher in the plugin data directory)")


def run(args, store: StorePaths, config: Config) -> Reply:
    if args.launcher:
        launcher = Path(args.launcher).absolute()
    elif args.plugin_data:
        launcher = write_stable_launcher(args.plugin_data)
    else:
        return error("run `ctx init` through the plugin launcher (bin/ctx), "
                     "or pass --launcher <path>", args.json)
    lines = []
    path = store.committed / "config.toml"
    if path.exists():
        lines.append(f"kept {path.relative_to(store.root).as_posix()}")
    else:
        atomic_write(path, default_config_toml().encode("utf-8"))
        lines.append(f"created {path.relative_to(store.root).as_posix()}")
    report = install(store.root, launcher)
    if report.installed:
        lines.append(f"Git hooks installed in {report.hooks_dir}: {', '.join(report.installed)}")
    if report.chained:
        lines.append(f"existing hooks kept and chained: {', '.join(report.chained)}")
    code = 0
    if report.manual:
        lines.append(f"{report.hooks_dir} is not managed by wscontext. Add each line to the "
                     "matching hook by hand:")
        lines += [f"  {hook}: {line}" for hook, line in report.manual.items()]
    if report.conflicts:
        code = 1
        lines.append("not installed, an original is already parked as <hook>.pre-wscontext: "
                     + ", ".join(report.conflicts))
    claude_md = store.root / "CLAUDE.md"
    before = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    after = set_block(before)
    if after != before:
        atomic_write(claude_md, after.encode("utf-8"))
    lines.append("CLAUDE.md block " + ("added" if BLOCK_BEGIN not in before else
                                       "updated" if after != before else "already present"))
    configured = load_config(store.root)
    if configured.sync.remote:
        try:
            changed = fetch(store, configured, force=True)
            lines.append(f"history fetched from {configured.sync.remote}"
                         + (" and materialized" if changed else ": nothing new"))
        except GitError as e:
            lines.append(f"history fetch from {configured.sync.remote} failed: {e}")
    lines.append("Run `ctx doctor` to check the setup.")
    return Reply("\n".join(lines), code, report=True)
