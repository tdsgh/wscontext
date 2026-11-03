"""Installs the wscontext Git hooks as `sh` shims chained to any existing hook.

Git runs every hook file through `sh`, Git for Windows included, so the shim is
`sh` on every platform; only the launcher path it embeds differs.
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from wscontext.gitutil import git
from wscontext.store.records import atomic_write

HOOKS = ("prepare-commit-msg", "commit-msg", "post-commit", "post-rewrite", "pre-push",
         "post-checkout", "post-merge")
STDIN_HOOKS = frozenset({"post-rewrite", "pre-push"})
CHAINED_SUFFIX = ".pre-wscontext"
MARKER = b"wscontext Git hook shim"
SHIMS = Path(__file__).resolve().parent / "shims"


@dataclass
class InstallReport:
    hooks_dir: Path
    installed: list[str] = field(default_factory=list)
    chained: list[str] = field(default_factory=list)
    # hook -> the line to add by hand, when the hooks directory is not wscontext's to edit
    manual: dict[str, str] = field(default_factory=dict)
    # an original is already parked as `<hook>.pre-wscontext` and another hook replaced the shim
    conflicts: list[str] = field(default_factory=list)


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _launcher(launcher: str | Path, platform: str) -> str:
    # Git for Windows' sh takes `C:/...` paths; backslashes would need escaping.
    path = str(launcher)
    return _quote(path.replace("\\", "/") if platform == "win32" else path)


def shim_text(hook: str, launcher: str | Path, platform: str) -> str:
    template = "hook-stdin.sh" if hook in STDIN_HOOKS else "hook.sh"
    text = (SHIMS / template).read_text(encoding="utf-8")
    # A failed pre-push means the user's push is not happening: history must not go out either.
    when = "[ $r -eq 0 ] && " if hook == "pre-push" else ""
    return (text.replace("@WHEN@", when).replace("@HOOK@", hook)
            .replace("@LAUNCHER@", _launcher(launcher, platform)))


def manual_line(hook: str, launcher: str | Path, platform: str) -> str:
    path = _launcher(launcher, platform)
    return f'[ -x {path} ] && {path} git-hook {hook} "$@" >/dev/null 2>&1 || true'


def hooks_dir(repo: Path) -> tuple[Path, bool]:
    """The directory Git reads hooks from, and whether wscontext may edit it.

    Only a hooks directory inside the Git common dir is edited; a `core.hooksPath`
    in the working tree (husky) or shared between repositories is left alone.
    """
    out = git("rev-parse", "--path-format=absolute", "--git-path", "hooks", "--git-common-dir",
              cwd=repo).splitlines()
    hooks, common = Path(out[0]).resolve(), Path(out[1]).resolve()
    return hooks, hooks.is_relative_to(common)


def _is_shim(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return MARKER in f.read(512)
    except OSError:
        return False


def install(repo: Path, launcher: str | Path, platform: str = sys.platform) -> InstallReport:
    directory, managed = hooks_dir(repo)
    report = InstallReport(directory)
    if not managed:
        report.manual = {hook: manual_line(hook, launcher, platform) for hook in HOOKS}
        return report
    directory.mkdir(parents=True, exist_ok=True)
    for hook in HOOKS:
        path = directory / hook
        chained = directory / (hook + CHAINED_SUFFIX)
        if not _is_shim(path) and (path.exists() or path.is_symlink()):
            if chained.exists() or chained.is_symlink():
                report.conflicts.append(hook)
                continue
            os.replace(path, chained)
            report.chained.append(hook)
        atomic_write(path, shim_text(hook, launcher, platform).encode("utf-8"))
        os.chmod(path, 0o755)
        report.installed.append(hook)
    return report


def uninstall(repo: Path) -> dict[str, str]:
    """Remove the shims and put chained originals back; returns hook -> `removed` or `restored`."""
    directory, managed = hooks_dir(repo)
    done: dict[str, str] = {}
    if not managed:
        return done
    for hook in HOOKS:
        path = directory / hook
        if not _is_shim(path):
            continue
        path.unlink()
        done[hook] = "removed"
        chained = directory / (hook + CHAINED_SUFFIX)
        if chained.exists() or chained.is_symlink():
            os.replace(chained, path)
            done[hook] = "restored"
    return done
