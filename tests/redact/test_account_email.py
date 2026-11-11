import json

from wscontext.config import Config
from wscontext.logs import HOOKS_LOG, append_log
from wscontext.redact import account
from wscontext.redact.redactor import AccountConfig, redact_bytes, redact_text, redact_value
from wscontext.redact.rules import account_digest
from wscontext.store.paths import StorePaths

# Made-up address and identifiers.
EMAIL = "Jane.Doe+work@mail.example.test"
OTHER = "someone.else@mail.example.test"
ACCOUNT = "11111111-2222-4333-8444-555555555555"


def config_for(*tokens: str) -> Config:
    return AccountConfig(account_digests=frozenset(account_digest(t) for t in tokens))


def test_the_account_email_is_masked_and_another_address_is_not():
    config = config_for(EMAIL)
    text, hits = redact_text(f"from {EMAIL}, cc {OTHER}.", config)
    assert text == f"from [REDACTED:account-email], cc {OTHER}."
    assert hits == ["account-email"]


def test_matching_ignores_case_and_surroundings():
    config = config_for(EMAIL)
    for shape in (EMAIL.upper(), EMAIL.lower(), f"<{EMAIL}>", f'"{EMAIL}"', f"{EMAIL}.",
                  f"mailto:{EMAIL}", f"Author: x <{EMAIL}>\n"):
        assert "jane.doe" not in redact_text(shape, config)[0].lower(), shape


def test_a_longer_address_containing_it_is_not_masked():
    config = config_for(EMAIL)
    longer = f"x{EMAIL}"
    assert redact_text(longer, config)[0] == longer


def test_without_digests_nothing_is_masked():
    assert redact_text(EMAIL, Config())[0] == EMAIL


def test_an_email_digest_does_not_mask_uuids_and_the_reverse():
    config = config_for(EMAIL, ACCOUNT)
    text = redact_text(f"{EMAIL} {ACCOUNT}", config)[0]
    assert text == "[REDACTED:account-email] [REDACTED:account-id]"


def test_bytes_and_json_values_are_masked():
    config = config_for(EMAIL)
    assert EMAIL.encode() not in redact_bytes(f'{{"x": "{EMAIL}"}}'.encode(), config)[0]
    assert redact_value({EMAIL: [EMAIL, OTHER]}, config) == {
        "[REDACTED:account-email]": ["[REDACTED:account-email]", OTHER]}


def test_the_account_file_teaches_the_email_digest_only(git_repo, tmp_path, monkeypatch):
    store = StorePaths.resolve(git_repo)
    file = tmp_path / "claude.json"
    file.write_text(json.dumps({"oauthAccount": {"emailAddress": EMAIL}}))
    monkeypatch.setattr(account, "account_file_path", lambda: file)
    account.learn_account_file(store, Config())
    assert account.read_digests(store) == {account_digest(EMAIL)}
    state = (store.state / "account_ids.json").read_text()
    assert EMAIL.lower() not in state.lower() and "example.test" not in state


def test_a_non_email_value_is_not_learned(git_repo):
    store = StorePaths.resolve(git_repo)
    account.learn_account_ids(store, Config(), {"not an address", "a@b", 7})
    assert account.read_digests(store) == frozenset()


def test_a_log_line_holds_no_account_email(git_repo):
    store = StorePaths.resolve(git_repo)
    account.learn_account_ids(store, Config(), {EMAIL})
    config = account.with_account_ids(Config(), store)
    append_log(store, config, HOOKS_LOG, {"event": "x", "error": f"bad {EMAIL} and {OTHER}"})
    line = (store.logs / HOOKS_LOG).read_text()
    assert EMAIL not in line and "[REDACTED:account-email]" in line and OTHER in line
