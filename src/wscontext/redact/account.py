"""The operator's own account IDs, known to the store only as SHA-256 digests. Stdlib only.

The digests live in the store's local state (`state/account_ids.json`), never in history.
No ID is written anywhere in clear, and no function here puts one in an error or a log.
"""

import json
from pathlib import Path

from wscontext.config import Config
from wscontext.redact.redactor import add_digests
from wscontext.redact.rules import UUID, account_digest
from wscontext.store.locks import FileLock
from wscontext.store.paths import StorePaths
from wscontext.store.records import write_json

ACCOUNT_KEYS = ("accountUuid", "organizationUuid")


def account_file_path() -> Path:
    return Path.home() / ".claude.json"


def digests_path(store: StorePaths) -> Path:
    return store.state / "account_ids.json"


def read_digests(store: StorePaths) -> frozenset[str]:
    try:
        data = json.loads(digests_path(store).read_bytes())
        return frozenset(d for d in data["sha256"] if isinstance(d, str))
    except (OSError, ValueError, KeyError, TypeError):
        return frozenset()


def with_account_ids(config: Config, store: StorePaths) -> Config:
    """The config with the store's known account digests added to its redaction rules."""
    digests = read_digests(store)
    if not digests:
        return config
    return add_digests(config, digests)


def learn_account_ids(store: StorePaths, config: Config, ids: set[str]) -> None:
    """Add the digest of every UUID-shaped ID in `ids` to the store's local state."""
    new = {account_digest(i) for i in ids if isinstance(i, str) and UUID.fullmatch(i)}
    if new <= read_digests(store):
        return
    path = digests_path(store)
    with FileLock(path.with_name(path.name + ".lock"), stale_after=10, timeout=1.0):
        write_json(path, {"sha256": sorted(new | read_digests(store))}, config)


def learn_account_file(store: StorePaths, config: Config) -> None:
    """Learn `oauthAccount.accountUuid` and `.organizationUuid` from Claude Code's account
    file. A missing, unreadable or differently shaped file teaches nothing and is not an error."""
    try:
        account = json.loads(account_file_path().read_bytes())["oauthAccount"]
        ids = {account[k] for k in ACCOUNT_KEYS if k in account}
        learn_account_ids(store, config, ids)
    except (OSError, ValueError, KeyError, TypeError):
        pass
