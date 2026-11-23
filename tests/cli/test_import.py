import json

import pytest
from conftest import run_git
from import_support import add_docs, commit_file, transcript

from wscontext.capture import importer
from wscontext.cli.init import default_config_toml
from wscontext.config import load_config
from wscontext.store.paths import StorePaths
from wscontext.store.records import read_checkpoint, read_session

TOKEN_LINE = "ctx import --offer "


@pytest.fixture
def past(git_repo, monkeypatch, tmp_path):
    """A set-up repository with two importable sessions (the older one made a commit), one
    unreadable transcript and documents in both groups."""
    monkeypatch.chdir(git_repo)
    (git_repo / ".wscontext").mkdir()
    (git_repo / ".wscontext" / "config.toml").write_text(default_config_toml())
    add_docs(git_repo)
    sha, out = commit_file(git_repo, "b.txt", "add b")
    transcript("sess-old", git_repo, "add a retry to the fetcher", commit_output=out,
               start="2026-01-01T10:00:00+00:00")
    transcript("sess-new", git_repo, "rename the helper", start="2026-02-01T10:00:00+00:00")
    transcript("sess-future", git_repo, "from the future", version="9.0.0")
    return StorePaths.resolve(git_repo), sha


def ctx(capsys, *argv):
    from wscontext.cli.main import main
    capsys.readouterr()
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out.removesuffix("\n"), out.err.removesuffix("\n")


def token(text: str) -> str:
    return text.split(TOKEN_LINE)[1].split()[0]


def test_preview_text(past, capsys):
    _, sha = past
    code, out, _ = ctx(capsys, "import")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("RETRIEVED DATA from wscontext history (ctx import)")
    # sessions newest first, then documents by group, then unreadable without a number
    assert "[session 1] 2026-02-01 rename the helper" in out
    assert "[session 2] 2026-01-01 add a retry to the fetcher" in out
    assert f"  commit {sha[:7]} add b" in out
    assert "  2 prompts" not in out and "  1 prompt\n" in out
    assert out.index("[document 3] docs/adr.md") < out.index("[document 4] notes/Design-notes.MD")
    assert out.index("[document 4]") < out.index("[document 5] README.md")
    assert "  Suggested" in out and "  Other" in out
    assert "CLAUDE.md" not in out
    assert out.index("[session 2]") < out.index("[unreadable sess-future.jsonl]")
    assert "Commits on main with no session: 3 now, 2 after importing every session above." in out
    assert "Claude Code deletes transcripts after 30 days (the default)." in out
    assert TOKEN_LINE in out and "--all" in out and "--take 1,3,5-7" in out


def test_preview_json_has_the_same_content(past, capsys):
    _, sha = past
    _, text, _ = ctx(capsys, "import")
    code, raw, _ = ctx(capsys, "--json", "import")
    data = json.loads(raw)
    assert code == 0 and data["token"] == token(text)
    kinds = [(r["type"], r.get("number")) for r in data["results"]]
    assert kinds == [("session", 1), ("session", 2), ("document", 3), ("document", 4),
                     ("document", 5), ("unreadable", None)]
    old = data["results"][1]
    assert old["session_id"] == "sess-old" and old["commits"][0]["sha"] == sha
    assert data["results"][2]["group"] == "suggested" and data["results"][4]["group"] == "other"
    assert (data["unlinked_now"], data["unlinked_after"]) == (3, 2)
    assert data["cleanup"]["days"] == 30


def test_preview_writes_nothing_and_logs_one_ctx_call(past, capsys):
    store, _ = past
    ctx(capsys, "import")
    assert not (store.records / "sessions").exists() or not list((store.records / "sessions").glob("*"))
    assert not (store.state / "import.json").exists()
    log = [json.loads(line) for line in (store.logs / "ctx-calls.jsonl").read_text().splitlines()]
    assert [row["argv"] for row in log] == [["import"]]


def test_all_imports_sessions_documents_and_links(past, capsys, git_repo):
    store, sha = past
    _, text, _ = ctx(capsys, "import")
    code, out, _ = ctx(capsys, "import", "--offer", token(text), "--all")
    assert code == 0
    assert out.splitlines()[0] == "Imported 2 sessions, linked 1 commit, added 3 documents."
    assert ".wscontext/config.toml changed; commit it" in out
    assert "git push" not in out
    assert read_checkpoint(store, sha).session_ids == ["sess-old"]
    assert read_session(store, "sess-new").imported_at
    assert load_config(git_repo).documents.paths == ("docs/adr.md", "notes/Design-notes.MD", "README.md")
    code, out, _ = ctx(capsys, "import")
    # the unreadable transcript is left, but nothing can be taken
    assert code == 0 and out == "Nothing new to import. 1 transcript cannot be read by this version."
    code, out, _ = ctx(capsys, "--json", "import")
    assert json.loads(out)["results"] == [] and len(json.loads(out)["unreadable"]) == 1


def test_take_and_decline_in_one_call(past, capsys):
    store, _ = past
    _, text, _ = ctx(capsys, "import")
    code, out, _ = ctx(capsys, "import", "--offer", token(text), "--take", "1,3-4", "--decline", "5")
    assert code == 0
    assert out.splitlines()[0] == "Imported 1 session, linked 0 commits, added 2 documents."
    assert "Declined 1 item; it is not offered again." in out
    assert read_session(store, "sess-new")
    state = json.loads((store.state / "import.json").read_text())
    assert list(state["declined_documents"]) == ["README.md"]
    _, shown, _ = ctx(capsys, "import")
    assert "README.md" not in shown and "[session 1] 2026-01-01" in shown
    _, shown, _ = ctx(capsys, "import", "--declined")
    assert "README.md" in shown and "declined earlier" in shown


def test_sessions_only_and_documents_only(past, capsys, git_repo):
    store, _ = past
    _, text, _ = ctx(capsys, "import")
    _, out, _ = ctx(capsys, "import", "--offer", token(text), "--sessions")
    assert out.splitlines()[0] == "Imported 2 sessions, linked 1 commit, added 0 documents."
    assert load_config(git_repo).documents.paths == ()
    _, text, _ = ctx(capsys, "import")
    _, out, _ = ctx(capsys, "import", "--offer", token(text), "--documents")
    assert out.splitlines()[0] == "Imported 0 sessions, linked 0 commits, added 3 documents."


def test_a_stale_token_is_refused_and_nothing_is_written(past, capsys, git_repo):
    store, _ = past
    _, text, _ = ctx(capsys, "import")
    transcript("sess-later", git_repo, "one more", start="2026-03-01T10:00:00+00:00")
    code, out, err = ctx(capsys, "import", "--offer", token(text), "--all")
    assert code == 1 and err == "ctx: the list changed; run `ctx import` again" and out == ""
    assert not (store.records / "sessions").exists() or not list((store.records / "sessions").glob("*"))
    code, out, _ = ctx(capsys, "--json", "import", "--offer", token(text), "--all")
    assert code == 1 and json.loads(out)["error"].startswith("the list changed")


@pytest.mark.parametrize("flags, message", [
    (["--take", "9"], "no item 9"),
    (["--take", "1,1"], "listed twice"),
    (["--take", "2-3,3"], "listed twice"),
    (["--take", "1", "--decline", "1"], "both taken and declined"),
    (["--take", "x"], "not a number"),
])
def test_bad_numbers_are_refused(past, capsys, flags, message):
    _, text, _ = ctx(capsys, "import")
    code, _, err = ctx(capsys, "import", "--offer", token(text), *flags)
    assert code == 1 and message in err


def test_choosing_needs_an_offer_token(past, capsys):
    code, _, err = ctx(capsys, "import", "--take", "1")
    assert code == 1 and "needs --offer TOKEN" in err
    code, _, err = ctx(capsys, "import", "--offer", "abcd1234")
    assert code == 1 and "--offer needs" in err


def test_not_set_up(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    code, out, err = ctx(capsys, "import")
    assert code == 1 and err == "ctx: wscontext is not set up here; run `ctx init` first"


def test_nothing_new_with_no_past_work(git_repo, monkeypatch, capsys):
    monkeypatch.chdir(git_repo)
    (git_repo / ".wscontext").mkdir()
    (git_repo / ".wscontext" / "config.toml").write_text(default_config_toml())
    code, out, _ = ctx(capsys, "import")
    assert code == 0 and out == "Nothing new to import."


def test_summary_names_the_sync_hint_when_sharing(past, capsys, git_repo):
    (git_repo / ".wscontext" / "config.toml").write_text(
        default_config_toml().replace("[sync]\n", '[sync]\nremote = "origin"\n'))
    _, text, _ = ctx(capsys, "import")
    _, out, _ = ctx(capsys, "import", "--offer", token(text), "--sessions")
    assert "Sessions and links go out with the next `git push` or `ctx sync`." in out


def test_a_failed_session_is_reported_with_exit_code_1(past, capsys, monkeypatch):
    _, text, _ = ctx(capsys, "import")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(importer, "_import_session", boom)
    code, out, err = ctx(capsys, "import", "--offer", token(text), "--sessions")
    assert code == 1 and err == "" and "error: sess-new: OSError: disk full" in out
