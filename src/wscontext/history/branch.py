"""The shared history at `refs/wscontext/history`, written with plumbing only.

The ref sits outside `refs/heads/`, so a plain clone or fetch never downloads it.
Never touches the working tree, the index or `HEAD`. Every ref update is a
compare-and-swap against the tip the new commit was built on.
"""

import logging
import random
import time
from pathlib import Path

from wscontext.gitutil import GitError, git, git_bytes
from wscontext.store.paths import StorePaths, is_safe_name

log = logging.getLogger(__name__)

HISTORY_REF = "refs/wscontext/history"
# where stage 1 kept the history; every writer moves it into HISTORY_REF first
LEGACY_REF = "refs/heads/wscontext/history"
SYNCED_DIRS = ("raw", "records")
CAS_ATTEMPTS = 30

# path on the branch -> (mode, blob sha), or None to remove the entry
Changes = dict[str, tuple[str, str] | None]


def read_tip(store: StorePaths, ref: str = HISTORY_REF) -> str | None:
    try:
        return git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                   cwd=store.root).strip()
    except GitError:
        return None


def cas_update(store: StorePaths, new: str, old: str | None, message: str) -> bool:
    zero = "0" * len(new)
    try:
        git("update-ref", "-m", message, HISTORY_REF, new, old or zero, cwd=store.root)
        return True
    except GitError as e:
        log.debug("history ref CAS failed: %s", e)
        return False


def read_blob(store: StorePaths, path: str) -> bytes | None:
    """A file's content on the local tip, or None when there is no tip or no such file."""
    try:
        return git_bytes("cat-file", "blob", f"{HISTORY_REF}:{path}", cwd=store.root)
    except GitError:
        return None


def branch_path(store: StorePaths, local: Path) -> str:
    """The branch path of a file under the local store, e.g. `records/sessions/<id>.json`."""
    parts = Path(local).resolve().relative_to(store.local.resolve()).parts
    if len(parts) < 2 or parts[0] not in SYNCED_DIRS or not all(map(is_safe_name, parts)):
        raise ValueError(f"not a synced store path: {local}")
    return "/".join(parts)


def local_path(store: StorePaths, path: str) -> Path | None:
    """The local file for a branch path, or None when the path is not one the store writes."""
    parts = path.split("/")
    if len(parts) < 2 or parts[0] not in SYNCED_DIRS or not all(map(is_safe_name, parts)):
        return None
    return store.local.joinpath(*parts)


def ls_blobs(store: StorePaths, commit: str) -> dict[str, tuple[str, str, int]]:
    """Every blob in a commit's tree: path -> (mode, sha, size)."""
    out = {}
    for entry in git("ls-tree", "-r", "-l", "-z", commit, cwd=store.root).split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            mode, kind, sha, size = meta.split()
            if kind == "blob":
                out[path] = (mode, sha, int(size))
    return out


def _write_tree(store: StorePaths, base: str | None, changes: Changes) -> str | None:
    """Apply changes to the tree `base`, rebuilding only the subtrees they touch.

    Returns None when the resulting tree is empty.
    """
    entries: dict[str, tuple[str, str, str]] = {}
    if base:
        for entry in git("ls-tree", "-z", base, cwd=store.root).split("\0"):
            if entry:
                meta, name = entry.split("\t", 1)
                mode, kind, sha = meta.split()
                entries[name] = (mode, kind, sha)
    nested: dict[str, Changes] = {}
    for path, value in changes.items():
        head, sep, rest = path.partition("/")
        if sep:
            nested.setdefault(head, {})[rest] = value
        elif value is None:
            entries.pop(head, None)
        else:
            entries[head] = (value[0], "blob", value[1])
    for name, sub in nested.items():
        old = entries.get(name)
        tree = _write_tree(store, old[2] if old and old[1] == "tree" else None, sub)
        if tree is None:
            entries.pop(name, None)
        else:
            entries[name] = ("040000", "tree", tree)
    if not entries:
        return None
    listing = "".join(f"{m} {k} {s}\t{n}\0" for n, (m, k, s) in entries.items())
    return git("mktree", "-z", cwd=store.root, input=listing).strip()


def _commit(store: StorePaths, tree: str, parents: list[str], message: str) -> str:
    args = ["commit-tree", "--no-gpg-sign", tree]
    for p in parents:
        args += ["-p", p]
    return git(*args, "-m", message, cwd=store.root).strip()


def _apply(store: StorePaths, changes: Changes, message: str) -> str | None:
    """Commit `changes` on top of the current tip, retrying until the CAS lands."""
    migrate_legacy(store)
    for attempt in range(CAS_ATTEMPTS):
        tip = read_tip(store)
        base = git("rev-parse", f"{tip}^{{tree}}", cwd=store.root).strip() if tip else None
        tree = _write_tree(store, base, changes)
        if tree is None:
            if tip is None:
                return None
            tree = git("mktree", cwd=store.root, input="").strip()
        if tree == base:
            return tip
        new = _commit(store, tree, [tip] if tip else [], message)
        if cas_update(store, new, tip, message):
            return new
        time.sleep(random.uniform(0, 0.005 * (attempt + 1)))
    raise GitError(("update-ref", HISTORY_REF), 1, "compare-and-swap kept failing")


def commit_paths(store: StorePaths, paths: list[Path], message: str) -> str | None:
    """Commit local store files to the history branch; returns the new tip.

    Unchanged content makes no commit and returns the current tip.
    """
    rel = [branch_path(store, p) for p in paths]
    if not rel:
        return read_tip(store)
    listing = "".join(f"{Path(p).resolve()}\n" for p in paths)
    shas = git("hash-object", "-w", "--no-filters", "--stdin-paths",
               cwd=store.root, input=listing).split()
    return _apply(store, {r: ("100644", s) for r, s in zip(rel, shas)}, message)


def remove_paths(store: StorePaths, paths: list[Path],
                 message: str = "wscontext: forget") -> str | None:
    """Commit a new tip without these files or directories, for local forget."""
    return _apply(store, {branch_path(store, p): None for p in paths}, message)


def squash_unshared(store: StorePaths, shared: list[str],
                    message: str = "wscontext: forget") -> str | None:
    """Replace every commit not yet on a remote with one commit holding the current tree.

    The new commit's parents are the `shared` tips the local tip contains, so nothing a
    remote already has changes, and blobs that only earlier local commits held are no
    longer reachable from the branch. Returns the new tip.
    """
    migrate_legacy(store)
    for attempt in range(CAS_ATTEMPTS):
        tip = read_tip(store)
        if tip is None:
            return None
        parents = [t for t in dict.fromkeys(shared) if is_ancestor(store, t, tip)]
        parents = [p for p in parents
                   if not any(q != p and is_ancestor(store, p, q) for q in parents)]
        if parents == [tip]:
            return tip
        tree = git("rev-parse", f"{tip}^{{tree}}", cwd=store.root).strip()
        new = _commit(store, tree, parents, message)
        if cas_update(store, new, tip, message):
            return new
        time.sleep(random.uniform(0, 0.005 * (attempt + 1)))
    raise GitError(("update-ref", HISTORY_REF), 1, "compare-and-swap kept failing")


def is_ancestor(store: StorePaths, a: str, b: str) -> bool:
    try:
        git("merge-base", "--is-ancestor", a, b, cwd=store.root)
        return True
    except GitError as e:
        if e.returncode == 1:
            return False
        raise


def union_merge(store: StorePaths, local: str | None, remote: str) -> str:
    """The commit holding the union of both histories.

    Fast-forwards when one side contains the other. Otherwise makes a two-parent
    commit; where both sides hold different blobs at one path, the larger blob
    wins (ties go to the larger SHA, so every host picks the same one).
    """
    if local is None or local == remote or is_ancestor(store, local, remote):
        return remote
    if is_ancestor(store, remote, local):
        return local
    ours = ls_blobs(store, local)
    changes: Changes = {}
    for path, (mode, sha, size) in ls_blobs(store, remote).items():
        mine = ours.get(path)
        if mine is None:
            changes[path] = (mode, sha)
        elif mine[1] != sha:
            take_remote = (size, sha) > (mine[2], mine[1])
            log.warning("history merge conflict at %s: kept the %s blob", path,
                        "remote" if take_remote else "local")
            if take_remote:
                changes[path] = (mode, sha)
    base = git("rev-parse", f"{local}^{{tree}}", cwd=store.root).strip()
    tree = _write_tree(store, base, changes) or git("mktree", cwd=store.root, input="").strip()
    return _commit(store, tree, [local, remote], "wscontext: merge remote history")


def merge_into_local(store: StorePaths, remote: str) -> str:
    """Union-merge `remote` into the local history ref; returns the new local tip."""
    migrate_legacy(store)
    for attempt in range(CAS_ATTEMPTS):
        local = read_tip(store)
        merged = union_merge(store, local, remote)
        if merged == local or cas_update(store, merged, local, "wscontext: merge remote history"):
            return merged
        time.sleep(random.uniform(0, 0.005 * (attempt + 1)))
    raise GitError(("update-ref", HISTORY_REF), 1, "compare-and-swap kept failing")


def migrate_legacy(store: StorePaths) -> bool:
    """Move a local `LEGACY_REF` into `HISTORY_REF`: union-merge it, then delete it.

    Both steps are compare-and-swap, so concurrent writers and migrations lose nothing.
    Returns True when this call deleted the legacy ref.
    """
    for attempt in range(CAS_ATTEMPTS):
        legacy = read_tip(store, LEGACY_REF)
        if legacy is None:
            return False
        tip = read_tip(store)
        merged = union_merge(store, tip, legacy)
        if merged == tip or cas_update(store, merged, tip, "wscontext: move history"):
            try:
                git("update-ref", "-d", LEGACY_REF, legacy, cwd=store.root)
                log.info("moved local history from %s to %s", LEGACY_REF, HISTORY_REF)
                return True
            except GitError as e:
                log.debug("legacy history ref CAS delete failed: %s", e)
        time.sleep(random.uniform(0, 0.005 * (attempt + 1)))
    raise GitError(("update-ref", HISTORY_REF), 1, "compare-and-swap kept failing")
