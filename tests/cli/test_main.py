import json
import subprocess
import sys

from cli_support import world  # noqa: F401

from wscontext.cli import main as ctx_main
from wscontext.cli.main import main
from wscontext.cli.output import (DEFAULT_BUDGET_TOKENS, Block, estimate_tokens, fit, quote,
                                  render)


def test_unknown_command_and_missing_command(capsys):
    assert main(["bogus"]) == 2
    assert "unknown command 'bogus'" in capsys.readouterr().err
    assert main([]) == 2 and "usage: ctx" in capsys.readouterr().err
    assert main(["--nope", "why"]) == 2


def test_launcher_and_shared_options_may_come_before_the_command(world, capsys):
    code, out, _ = world.ctx(capsys, "--plugin-root", "/r", "--plugin-data", "/d", "--json",
                             "--limit", "1", "sessions")
    assert code == 0
    assert json.loads(out)["command"] == "ctx sessions"


def test_outside_a_repository_is_a_clean_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["sessions"]) == 1
    assert "not inside a Git repository" in capsys.readouterr().err


def test_internal_commands_are_wired_to_their_modules(monkeypatch):
    from wscontext.capture import dispatch, worker
    from wscontext.githooks import dispatch as githooks
    seen = []
    monkeypatch.setattr(dispatch, "main", lambda argv: seen.append(("hook", argv)) or 0)
    monkeypatch.setattr(worker, "main", lambda argv: seen.append(("worker", argv)) or 0)
    monkeypatch.setattr(githooks, "main", lambda argv: seen.append(("git-hook", argv)) or 0)
    assert main(["--plugin-root", "r", "--plugin-data", "d", "hook", "Stop"]) == 0
    assert main(["worker", "copy", "sid", "t.jsonl", "/cwd"]) == 0
    assert main(["git-hook", "pre-push", "origin", "url"]) == 0
    assert seen == [("hook", ["Stop"]), ("worker", ["copy", "sid", "t.jsonl", "/cwd"]),
                    ("git-hook", ["pre-push", "origin", "url"])]


def test_wip_commands_have_no_entry_yet():
    assert set(ctx_main.COMMANDS) == {"why", "sessions", "show", "forget"}


def test_a_command_imports_only_its_own_module(world):
    code = ("import sys\nfrom wscontext.cli.main import main\n"
            "assert main(['sessions']) == 0\n"
            "print(sorted(m for m in sys.modules if m.startswith('wscontext.cli.')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=world.repo, check=True)
    assert out.stdout.splitlines()[-1] == str(["wscontext.cli.data", "wscontext.cli.main",
                                      "wscontext.cli.output", "wscontext.cli.sessions"])


def test_every_call_is_logged_with_redacted_argv(world, capsys):
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    world.ctx(capsys, "show", secret)
    world.ctx(capsys, "sessions", "--branch", "main")
    lines = [json.loads(x) for x in (world.store.logs / "ctx-calls.jsonl").read_text().splitlines()]
    assert [e["argv"][0] for e in lines] == ["show", "sessions"]
    assert lines[1]["argv"] == ["sessions", "--branch", "main"]
    assert lines[0]["exit"] == 1 and lines[1]["exit"] == 0
    assert lines[1]["source"] == "cli" and lines[1]["output_bytes"] > 0
    assert {"ts", "cwd", "ms"} <= set(lines[1])
    assert secret not in (world.store.logs / "ctx-calls.jsonl").read_text()
    assert "[REDACTED" in lines[0]["argv"][1]


def test_fit_cuts_an_oversized_block_and_keeps_its_pointer():
    big = Block("commit", "abc", "big", "2026-01-01", [f"line {i} " + "x" * 80 for i in range(200)],
                ["ctx show s1"])
    kept, left = fit([big], 300)
    assert left == 0 and kept[0].truncated
    assert estimate_tokens(kept[0].text()) <= 300
    assert kept[0].text().endswith("more: `ctx show s1`")
    assert kept[0].as_dict()["truncated"] is True


def test_fit_drops_whole_blocks_and_counts_them():
    blocks = [Block("session", f"s{i}", "t", None, ["y" * 400]) for i in range(10)]
    kept, left = fit(blocks, 350)
    assert 0 < len(kept) < 10 and left == 10 - len(kept)
    assert fit(blocks, None) == (blocks, 0)


def test_render_text_and_json_carry_the_same_results():
    blocks = [Block("session", "s1", "main by a", "2026-01-01", ["2 turns"], ["ctx show s1"])]
    text = render("ctx sessions", blocks, full=False, as_json=False)
    data = json.loads(render("ctx sessions", blocks, full=False, as_json=True))
    assert text.startswith("RETRIEVED DATA") and text.endswith("END OF RETRIEVED DATA.")
    assert data["results"][0]["id"] == "s1" and "[session s1] 2026-01-01 main by a" in text
    assert "No results." in render("ctx sessions", [], full=False, as_json=False)
    assert quote("a\nb") == "> a\n> b"


def test_why_output_stays_within_the_budget(world, capsys):
    from cli_support import commit
    from conftest import run_git
    for i in range(40):
        with (world.repo / "all.py").open("a") as f:
            f.write(f"v{i} = 1\n")
        run_git(world.repo, "add", "all.py")
        commit(world.repo, f"change number {i} " + "word " * 30 + "\n\nAI-Session: sess-one",
               f"2026-02-{(i % 27) + 1:02d}T12:00:00+00:00")
    code, out, _ = world.ctx(capsys, "why", "all.py", "--limit", "40")
    assert code == 0 and estimate_tokens(out) <= DEFAULT_BUDGET_TOKENS
    assert "more results not shown" in out
    data = json.loads(world.ctx(capsys, "why", "all.py", "--limit", "40", "--json")[1])
    assert 0 < len(data["results"]) < 40 and data["omitted"] == 40 - len(data["results"])
    _, full, _ = world.ctx(capsys, "why", "all.py", "--limit", "40", "--full")
    assert estimate_tokens(full) > DEFAULT_BUDGET_TOKENS and full.count("[commit ") == 40
    _, out, _ = world.ctx(capsys, "sessions", "--limit", "500")
    assert estimate_tokens(out) <= DEFAULT_BUDGET_TOKENS
