import json

from wscontext.config import Config, LogsConfig
from wscontext.logs import append_log


def test_lines_are_redacted(git_repo):
    from wscontext.store.paths import StorePaths
    store = StorePaths.resolve(git_repo)
    append_log(store, Config(), "x.jsonl", {"argv": ["AWS_SECRET_ACCESS_KEY=abcdEFGH1234abcdEFGH1234abcdEFGH12345678"],
                                            "n": 1})
    text = (store.logs / "x.jsonl").read_text()
    assert "abcdEFGH1234" not in text and json.loads(text)["n"] == 1


def test_rotation_by_size(git_repo):
    from wscontext.store.paths import StorePaths
    store = StorePaths.resolve(git_repo)
    cfg = Config(logs=LogsConfig(max_bytes=100))
    for i in range(10):
        append_log(store, cfg, "x.jsonl", {"i": i, "pad": "x" * 40})
    assert (store.logs / "x.jsonl.1").exists()
    assert (store.logs / "x.jsonl").stat().st_size < 300
