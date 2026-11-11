import json

from wscontext.config import Config
from wscontext.logs import HOOKS_LOG, append_log
from wscontext.redact import account
from wscontext.redact.redactor import AccountConfig, redact_bytes, redact_text, redact_value
from wscontext.redact.rules import account_digest
from wscontext.store.paths import StorePaths

# Made-up identifiers.
ACCOUNT = "11111111-2222-4333-8444-555555555555"
ORG = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "99999999-8888-4777-8666-555555555555"


def with_digests(*ids: str) -> Config:
    return redact_config(frozenset(account_digest(i) for i in ids))


def redact_config(digests):
    return AccountConfig(account_digests=digests)


def test_a_known_uuid_is_masked_and_another_is_not():
    config = with_digests(ACCOUNT, ORG)
    text, hits = redact_text(f"a={ACCOUNT} o={ORG} session={OTHER}", config)
    assert text == f"a=[REDACTED:account-id] o=[REDACTED:account-id] session={OTHER}"
    assert hits == ["account-id", "account-id"]


def test_matching_ignores_case_and_surroundings():
    config = with_digests(ACCOUNT)
    for shape in (ACCOUNT.upper(), f"acct_{ACCOUNT}", f'"{ACCOUNT}"', f"{ACCOUNT}.json"):
        assert ACCOUNT.lower() not in redact_text(shape, config)[0].lower(), shape


def test_without_digests_nothing_is_masked():
    assert redact_text(ACCOUNT, Config())[0] == ACCOUNT


def test_bytes_and_json_values_are_masked():
    config = with_digests(ACCOUNT)
    assert ACCOUNT.encode() not in redact_bytes(f'{{"x": "{ACCOUNT}"}}'.encode(), config)[0]
    assert redact_value({ACCOUNT: [ACCOUNT]}, config) == {
        "[REDACTED:account-id]": ["[REDACTED:account-id]"]}


def test_with_account_ids_adds_the_stored_digests(git_repo):
    store = StorePaths.resolve(git_repo)
    assert account.with_account_ids(Config(), store) == Config()
    account.learn_account_ids(store, Config(), {ACCOUNT, "not-a-uuid"})
    config = account.with_account_ids(Config(), store)
    assert config.account_digests == {account_digest(ACCOUNT)}
    state = (store.state / "account_ids.json").read_text()
    assert json.loads(state) == {"sha256": [account_digest(ACCOUNT)]}
    assert ACCOUNT not in state


def test_learning_is_additive(git_repo):
    store = StorePaths.resolve(git_repo)
    account.learn_account_ids(store, Config(), {ACCOUNT})
    account.learn_account_ids(store, Config(), {ORG})
    assert account.read_digests(store) == {account_digest(ACCOUNT), account_digest(ORG)}


def test_the_account_file_is_read_for_the_two_ids(git_repo, tmp_path, monkeypatch):
    store = StorePaths.resolve(git_repo)
    file = tmp_path / "claude.json"
    file.write_text(json.dumps({"oauthAccount": {"accountUuid": ACCOUNT, "organizationUuid": ORG,
                                                 "emailAddress": "x@example.com"}}))
    monkeypatch.setattr(account, "account_file_path", lambda: file)
    account.learn_account_file(store, Config())
    assert account.read_digests(store) == {account_digest(ACCOUNT), account_digest(ORG)}


def test_a_missing_or_odd_account_file_is_not_an_error(git_repo, tmp_path, monkeypatch):
    store = StorePaths.resolve(git_repo)
    file = tmp_path / "claude.json"
    monkeypatch.setattr(account, "account_file_path", lambda: file)
    account.learn_account_file(store, Config())  # missing
    for body in ("not json", "[]", "{}", '{"oauthAccount": null}', '{"oauthAccount": {"accountUuid": 7}}'):
        file.write_text(body)
        account.learn_account_file(store, Config())
    assert account.read_digests(store) == frozenset()
    assert not (store.state / "account_ids.json").exists()


def test_a_log_line_holds_no_account_id(git_repo):
    store = StorePaths.resolve(git_repo)
    account.learn_account_ids(store, Config(), {ACCOUNT})
    config = account.with_account_ids(Config(), store)
    append_log(store, config, HOOKS_LOG, {"event": "x", "error": f"bad {ACCOUNT} and {OTHER}"})
    line = (store.logs / HOOKS_LOG).read_text()
    assert ACCOUNT not in line and "[REDACTED:account-id]" in line and OTHER in line


# --- every entry point that loads config also adds the account digests ---

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "wscontext"

# (module path under src/wscontext, function) -> why it may use the bare config
UNWRAPPED_ALLOWED = {
    ("cli/init.py", "run"): "reads only the retrieval switch and writes no stored content",
}


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def test_every_function_that_loads_config_adds_the_account_digests():
    missing = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "load_config"]
            if calls and "with_account_ids" not in _names(fn):
                key = (path.relative_to(SRC).as_posix(), fn.name)
                if key not in UNWRAPPED_ALLOWED:
                    missing.append(f"{key[0]}:{calls[0].lineno} {fn.name}")
    assert not missing, f"load_config without with_account_ids: {missing}"
