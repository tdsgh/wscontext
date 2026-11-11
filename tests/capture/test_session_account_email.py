import json
import subprocess

from support import hook, transcript  # noqa: F401  (fixtures)

from wscontext.history.branch import ls_blobs, read_tip
from wscontext.redact import account
from wscontext.store.records import read_session

# Made-up address.
EMAIL = "jane.doe@mail.example.test"
OTHER = "someone.else@mail.example.test"


def _store_texts(hook, git_repo):
    store = hook.store
    texts = {str(p): p.read_bytes() for p in store.local.rglob("*") if p.is_file()}
    for rel, (_, sha, _) in ls_blobs(store, read_tip(store)).items():
        texts["branch:" + rel] = subprocess.run(["git", "cat-file", "blob", sha], cwd=git_repo,
                                                capture_output=True).stdout
    return texts


def test_the_account_email_is_masked_in_copies_records_and_logs(hook, git_repo, transcript,
                                                                tmp_path, monkeypatch):
    file = tmp_path / "account.json"
    file.write_text(json.dumps({"oauthAccount": {"emailAddress": EMAIL}}))
    monkeypatch.setattr(account, "account_file_path", lambda: file)
    lines = [
        {"type": "user", "version": "2.0.5", "timestamp": "2026-01-01T10:03:00Z",
         "message": {"role": "user", "content": f"mail {EMAIL.upper()} and {OTHER}"}},
        {"type": "user", "version": "2.0.5", "timestamp": "2026-01-01T10:04:00Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": f"Author: x <{EMAIL}>"}]}},
    ]
    with transcript.open("a") as f:
        f.write("".join(json.dumps(x) + "\n" for x in lines))
    subprocess.run(["git", "checkout", "-q", "-b", f"feat/{EMAIL}"], cwd=git_repo, check=True)
    for event, fixture in [("SessionStart", "session_start"), ("UserPromptSubmit", "user_prompt_submit"),
                           ("Stop", "stop"), ("SessionEnd", "session_end")]:
        assert hook(event, fixture)["ok"]
    texts = _store_texts(hook, git_repo)
    raw = next(v for k, v in texts.items() if "/raw/sessions/" in k)
    assert raw.count(b"[REDACTED:account-email]") == 2 and OTHER.encode() in raw
    assert read_session(hook.store, "sess-cap1").branch == "feat/[REDACTED:account-email]"
    for name, data in texts.items():
        assert EMAIL.encode() not in data.lower(), name
    assert b"jane.doe" not in (hook.store.logs / "hooks.jsonl").read_bytes().lower()
