import json

from cli_support import checkpoint, run_json, spawned, world  # noqa: F401
from conftest import run_git


def _config(world, text):
    (world.repo / ".wscontext").mkdir(exist_ok=True)
    (world.repo / ".wscontext" / "config.toml").write_text(text)


def _callers(world):
    w = world.repo
    (w / "src" / "use.py").write_text(
        "from app import alpha\n\n\ndef run_it():\n    return alpha()\n\n\ndef run_twice():\n    return run_it()\n")
    (w / "src" / "top.py").write_text("from use import run_twice\n\n\ndef main():\n    run_twice()\n")
    (w / "tests").mkdir(exist_ok=True)
    (w / "tests" / "test_app.py").write_text(
        "from app import alpha\n\n\ndef test_alpha():\n    assert alpha() == 11\n")
    (w / "src" / "other.py").write_text("class Other:\n    def alpha(self):\n        return 0\n")


def test_impact_lists_callers_importers_tests_and_sessions(world, capsys):
    checkpoint(world.store, world.c1, ["sess-one"])
    _callers(world)
    data = run_json(world, capsys, "impact", "src/app.py#alpha")
    by_type: dict[str, list] = {}
    for r in data["results"]:
        by_type.setdefault(r["type"], []).append(r)
    assert [r["qualname"] for r in by_type["caller"]] == ["run_it"]
    caller = by_type["caller"][0]
    assert caller["match"] == "name" and caller["shared"] == 2 and caller["depth"] == 1
    assert "name match" in "\n".join(caller["body"]) and "2 definitions share" in "\n".join(caller["body"])
    assert [r["id"] for r in by_type["test"] if r["kind"] == "caller"] == ["tests/test_app.py:4-5"]
    assert [r["path"] for r in by_type["importer"]] == ["src/use.py"]
    assert by_type["session"][0]["id"] == "sess-one"
    assert by_type["session"][0]["links"] == ["ctx show sess-one"]
    assert data["counts"]["callers"] == 1 and data["depth"] == 1
    assert "matched by name only" in data["note"]
    _, text, _ = world.ctx(capsys, "impact", "src/app.py#alpha")
    for r in data["results"]:
        assert r["id"] in text
    assert "name match" in text and "matched by name only" in text


def test_impact_depth_follows_callers_of_callers(world, capsys):
    _callers(world)
    one = run_json(world, capsys, "impact", "src/app.py#alpha")["counts"]["callers"]
    deep = run_json(world, capsys, "impact", "src/app.py#alpha", "--depth", "3")
    names = {r["qualname"]: r["depth"] for r in deep["results"] if r["type"] == "caller"}
    assert one == 1 and names == {"run_it": 1, "run_twice": 2, "main": 3}


def test_impact_caller_line_agrees_with_a_single_shared_definition(world, capsys):
    _callers(world)
    data = run_json(world, capsys, "impact", "src/use.py#run_it")
    caller = next(r for r in data["results"] if r["type"] == "caller")
    assert caller["shared"] == 1 and "(1 definition shares this name)" in caller["body"][0]


def test_impact_on_a_file_uses_its_definitions_and_skips_its_own_calls(world, capsys):
    _callers(world)
    data = run_json(world, capsys, "impact", "src/app.py")
    assert data["qualname"] is None
    assert {r["qualname"] for r in data["results"] if r["type"] == "caller"} == {"run_it"}


def test_impact_cochange(world, capsys):
    (world.repo / "src" / "b.py").write_text("x = 1\n")
    (world.repo / "src" / "app.py").write_text("def alpha():\n    return 1\n")
    for i in range(2):
        (world.repo / "src" / "b.py").write_text(f"x = {i + 5}\n")
        (world.repo / "src" / "app.py").write_text(f"def alpha():\n    return {i + 7}\n")
        run_git(world.repo, "add", "src")
        run_git(world.repo, "commit", "-q", "-m", f"pair {i}")
    data = run_json(world, capsys, "impact", "src/app.py")
    co = [r for r in data["results"] if r["type"] == "cochange"]
    assert [(r["path"], r["count"]) for r in co] == [("src/b.py", 2)]
    assert "2 commits" in co[0]["body"][0]


def test_impact_sessions_follow_the_definition_after_its_lines_moved(world, capsys):
    checkpoint(world.store, world.c1, ["sess-one"])
    (world.repo / "src" / "app.py").write_text("# moved\n\n\n" + (world.repo / "src" / "app.py").read_text())
    data = run_json(world, capsys, "impact", "src/app.py#beta")
    assert [r["id"] for r in data["results"] if r["type"] == "session"] == ["sess-one"]


def test_impact_ambiguous_target_lists_candidates_and_exits_1(world, capsys):
    _callers(world)
    code, out, err = world.ctx(capsys, "impact", "alpha")
    assert code == 1 and err == "" and "Several definitions match" in out
    assert "[definition src/app.py:1-2]" in out and "[definition src/other.py:2-3]" in out
    code, out, _ = world.ctx(capsys, "impact", "alpha", "--json")
    data = json.loads(out)
    assert code == 1 and data["ambiguous"] is True and len(data["results"]) == 2
    assert world.ctx(capsys, "impact", "Other.alpha")[0] == 0


def test_impact_unknown_target_is_an_error(world, capsys):
    code, out, err = world.ctx(capsys, "impact", "nothing_like_this")
    assert code == 1 and "no definition matches" in err
    code, _, err = world.ctx(capsys, "impact", "src/app.py#nothing")
    assert code == 1 and "no definition matches" in err


def test_impact_limit_applies_per_section(world, capsys):
    _callers(world)
    (world.repo / "src" / "more.py").write_text(
        "from app import alpha\n\n\ndef a1():\n    alpha()\n\n\ndef a2():\n    alpha()\n")
    data = run_json(world, capsys, "--limit", "1", "impact", "src/app.py#alpha")
    assert len([r for r in data["results"] if r["type"] == "caller"]) == 1
    assert data["omitted"] >= 2 and data["counts"]["callers"] == 3


def test_impact_flags_a_parse_error(world, capsys):
    (world.repo / "src" / "bad.py").write_text("def broken(:\n    pass\n")
    assert "parse-errors" in run_json(world, capsys, "impact", "src/app.py#alpha")["flags"]


def test_impact_switched_off_says_so_in_one_line(world, capsys):
    _config(world, "[retrieval]\nenabled = false\n")
    code, out, _ = world.ctx(capsys, "impact", "alpha")
    assert code == 0 and "\n" not in out and out.startswith("ctx impact:") and "switched off" in out
    assert json.loads(world.ctx(capsys, "impact", "alpha", "--json")[1])["enabled"] is False


def test_impact_file_lists_sessions_that_edited_it_without_a_commit(world, capsys):
    from cli_support import add_session
    add_session(world.store, "sess-nocommit", files=("src/app.py",), started="2026-01-05T10:00:00Z")
    data = run_json(world, capsys, "impact", "src/app.py")
    sessions = [r for r in data["results"] if r["type"] == "session"]
    row = next(r for r in sessions if r["id"] == "sess-nocommit")
    assert row["shas"] == [] and row["links"] == ["ctx show sess-nocommit"]
    assert "no linked commit" in row["body"]
    assert data["counts"]["sessions"] == len(sessions)
    _, text, _ = world.ctx(capsys, "impact", "src/app.py")
    assert "sess-nocommit" in text and "no linked commit" in text


def test_impact_symbol_ignores_sessions_without_a_commit(world, capsys):
    from cli_support import add_session
    add_session(world.store, "sess-nocommit", files=("src/app.py",), started="2026-01-05T10:00:00Z")
    data = run_json(world, capsys, "impact", "src/app.py#alpha")
    assert "sess-nocommit" not in [r["id"] for r in data["results"]]


def test_impact_does_not_list_a_committed_session_twice(world, capsys):
    checkpoint(world.store, world.c1, ["sess-one"])
    data = run_json(world, capsys, "impact", "src/app.py")
    ids = [r["id"] for r in data["results"] if r["type"] == "session"]
    assert ids == ["sess-one"]
    row = next(r for r in data["results"] if r["type"] == "session")
    assert row["shas"] == [world.c1]
