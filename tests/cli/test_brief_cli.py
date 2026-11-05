import json

from cli_support import add_session, checkpoint, run_json, spawned, world  # noqa: F401

from wscontext.cli.output import estimate_tokens


def _config(world, text):
    (world.repo / ".wscontext").mkdir(exist_ok=True)
    (world.repo / ".wscontext" / "config.toml").write_text(text)


def test_brief_prints_what_a_session_starting_now_gets(world, capsys):
    checkpoint(world.store, world.c1, ["sess-one"])
    code, out, _ = world.ctx(capsys, "brief")
    assert code == 0
    assert out.startswith("RETRIEVED DATA from wscontext history (session brief).")
    assert "Recent sessions on branch main:" in out and "[session sess-one]" in out
    assert "touched: src/app.py" not in out  # already listed as a recent session
    assert out.endswith("END OF RETRIEVED DATA.") and estimate_tokens(out) <= 500
    data = run_json(world, capsys, "brief")
    assert data["brief"] == out and data["reason"] is None and data["tokens"] == estimate_tokens(out)


def test_brief_says_why_there_is_none(world, capsys):
    _config(world, "[retrieval]\nbrief = false\n")
    code, out, _ = world.ctx(capsys, "brief")
    assert code == 0 and out == "No brief: the brief is switched off ([retrieval] brief = false)."
    _config(world, "[retrieval]\nenabled = false\n")
    data = run_json(world, capsys, "brief")
    assert data["brief"] is None and "switched off" in data["reason"]


def test_brief_with_nothing_relevant_says_so(git_repo, monkeypatch, capsys, spawned):
    from cli_support import World  # noqa: F401
    from wscontext.cli.main import main
    monkeypatch.chdir(git_repo)
    capsys.readouterr()
    assert main(["brief"]) == 0
    assert capsys.readouterr().out.startswith("No brief: no relevant prior work")
