import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

from cli_support import add_session, checkpoint, isolated_git, run_json, spawned, world  # noqa: F401
from test_stats import stats

from wscontext.config import Config
from wscontext.history.branch import commit_paths
from wscontext.index import build
from wscontext.store.paths import StorePaths
from wscontext.store.records import SessionRecord, write_session


def stats(world, capsys, *argv: str) -> list[dict]:
    """The view's blocks merged into one dict, as the JSON carries them."""
    merged: dict = {}
    for block in run_json(world, capsys, "stats", *argv)["results"]:
        merged.update(block)
    return [merged]


def set_config(world, text: str) -> None:
    (world.repo / ".wscontext").mkdir(exist_ok=True)
    (world.repo / ".wscontext" / "config.toml").write_text(text)


def schema2(world, sid: str, **fields) -> None:
    write_session(world.store, SessionRecord(
        session_id=sid, author="t@example.com", started_at="2026-01-08T10:00:00Z", **fields),
        Config())


def test_schema1_session_is_measured_on_its_stored_copy(world, capsys):
    data = stats(world, capsys, "data")[0]
    t = data["transcript"]
    assert t["sessions_measured"] == 1 and t["stored"] > 0 and t["source"] == 0
    assert sum(t["kinds"].values()) == t["stored"]
    assert data["per_session"][0]["session_id"] == "sess-one"
    assert data["per_session"][0]["measured"] is True
    assert data["local"]["groups"].keys() >= {"raw/sessions", "records"}
    assert data["history_branch"]["bytes"] == data["branch_bytes"] > 0
    assert data["shared_history"]["bytes"] == 0 and data["pruned_paths"] == 0


def test_recorded_bytes_compare_with_the_source(world, capsys):
    schema2(world, "sess-two", retrieval=True, brief_tokens=120, ended_by="idle",
            transcript_bytes={"prompt": 100, "bulk": 700, "extra": 50,
                              "stored": 850, "source": 3400})
    t = stats(world, capsys, "data")[0]["transcript"]
    assert t["sessions_with_source"] == 1 and t["source"] == 3400
    assert t["stored_of_known_source"] == 850 and t["sessions_measured"] == 1
    assert t["kinds"]["bulk"] == 700 and t["kinds"]["extra"] == 50
    _, out, _ = world.ctx(capsys, "stats", "data", "--full")
    assert "25% of the source Claude Code wrote (3.3 KB, 1 sessions with a source)" in out
    assert "bulk 700 B, prompt 425 B" in out


def test_size_warnings_name_the_kind_and_the_session(world, capsys):
    set_config(world, "[store]\nsession_warn_bytes = 500\nstore_warn_bytes = 1000\n")
    schema2(world, "sess-big", transcript_bytes={"prompt": 100, "bulk": 90000,
                                                "stored": 90100, "source": 90100})
    warnings = stats(world, capsys, "data")[0]["warnings"]
    session = next(w for w in warnings if w["scope"] == "session" and w["id"] == "sess-big")
    assert session["largest"][0] == {"name": "bulk", "bytes": 90000}
    local = next(w for w in warnings if w["scope"] == "local store")
    assert local["largest_kinds"][0]["name"] == "bulk"
    assert local["largest_sessions"][0]["name"] == "sess-one"
    _, out, _ = world.ctx(capsys, "stats", "data", "--full")
    assert "WARNING session sess-big: 88.0 KB is over the 500 B limit; largest: bulk 87.9 KB" in out
    assert "WARNING local store:" in out and "by kind: bulk 87.9 KB" in out
    assert "shared history" not in out.split("WARNING")[-1]


def test_no_warning_under_the_default_limits(world, capsys):
    assert stats(world, capsys, "data")[0]["warnings"] == []
    _, out, _ = world.ctx(capsys, "stats", "data", "--full")
    assert "size warnings: none" in out


def test_growth_per_week(world, capsys):
    schema2(world, "sess-two", transcript_bytes={"prompt": 10, "stored": 10})
    commit_paths(world.store, [world.store.records / "sessions" / "sess-two.json"], "two")
    g = stats(world, capsys, "data")[0]["growth"]
    assert [r["week"] for r in g["sessions"]] == ["2026-W01", "2026-W02"]
    assert g["sessions"][1] == {"week": "2026-W02", "sessions": 1, "stored_bytes": 10}
    assert g["history_branch"] and g["history_branch"][-1]["bytes"] > 0
    assert sum(r["growth"] for r in g["history_branch"]) == g["history_branch"][-1]["bytes"]


def test_index_figures_and_lag_without_a_refresh(world, capsys):
    assert stats(world, capsys, "data")[0]["index"] == {
        "present": False, "bytes": 0, "sessions": 0, "commits": 0, "documents": 0, "lag": None}
    checkpoint(world.store, world.c1, ["sess-one"])
    build.refresh(world.store)
    ix = stats(world, capsys, "data")[0]["index"]
    assert ix["present"] and ix["bytes"] > 0 and ix["sessions"] == 1 and ix["commits"] == 1
    assert ix["documents"] >= 2 and ix["lag"] == 0
    add_session(world.store, "sess-late")
    checkpoint(world.store, world.c2, ["sess-late"])
    assert stats(world, capsys, "data")[0]["index"]["lag"] == 2
    assert stats(world, capsys, "data")[0]["index"]["sessions"] == 1  # no refresh ran
    _, out, _ = world.ctx(capsys, "stats", "data", "--full")
    assert "lag: 2 sources behind captured history" in out


def test_brief_share_and_retrieval_state(world, capsys):
    schema2(world, "sess-brief", retrieval=True, brief_tokens=300)
    schema2(world, "sess-quiet", retrieval=False, brief_tokens=0)
    set_config(world, "[retrieval]\nenabled = false\n")
    r = stats(world, capsys, "data")[0]["retrieval"]
    assert r["enabled"] is False and r["sessions_with_switch"] == 2
    assert r["sessions_with_brief"] == 1 and r["brief_share"] == 0.5
    assert r["sessions_retrieval_on"] == 1 and r["brief_tokens"] == 300
    _, out, _ = world.ctx(capsys, "stats", "data", "--full")
    assert "retrieval switch: off; brief: off" in out
    assert "sessions with a brief: 1 of 2 (50%); brief tokens given: 300" in out


def test_adoption_over_the_window(world, capsys):
    schema2(world, "sess-find", ctx_calls={"find": 2, "why": 1})
    schema2(world, "sess-why", ctx_calls={"why": 1})
    q = stats(world, capsys, "queries")[0]["adoption"]
    assert (q["sessions"], q["with_ctx"], q["with_find"]) == (3, 2, 1)
    assert q["with_find_share"] == 1 / 3
    assert q["by_week"][-1] == {"week": "2026-W02", "sessions": 2, "with_ctx": 2, "with_find": 1}
    _, out, _ = world.ctx(capsys, "stats", "queries")
    assert "sessions that called ctx: 2 of 3 (67%); called ctx find: 1 (33%)" in out


def test_session_view_shows_brief_retrieval_ended_by_and_bytes(world, capsys):
    schema2(world, "sess-new", retrieval=True, brief_tokens=210, ended_by="idle",
            transcript_bytes={"prompt": 40, "reply": 60, "stored": 100, "source": 400})
    d = stats(world, capsys, "session", "sess-new")[0]
    assert (d["ended_by"], d["retrieval"], d["brief_tokens"]) == ("idle", True, 210)
    assert d["volume"]["kinds"] == {"prompt": 40, "reply": 60} and d["volume"]["source"] == 400
    _, out, _ = world.ctx(capsys, "stats", "session", "sess-new")
    assert "ended by: idle; retrieval: on; brief: 210 tokens" in out
    assert "stored: 100 B (source 400 B); by kind: reply 60 B, prompt 40 B" in out
    old = stats(world, capsys, "session", "sess-one")[0]
    assert old["ended_by"] is None and old["volume"]["measured"] is True
    _, out, _ = world.ctx(capsys, "stats", "session", "sess-one")
    assert "ended by: not recorded; retrieval: not recorded; brief: not recorded" in out
    assert "measured on the stored copy" in out


def test_every_text_figure_is_in_json(world, capsys):
    out = run_json(world, capsys, "stats", "data")
    blocks = {r["view"] for r in out["results"]} if "view" in out["results"][0] else None
    keys = set().union(*(r.keys() for r in out["results"]))
    assert {"transcript", "local", "history_branch", "shared_history", "warnings", "growth",
            "index", "retrieval", "per_session"} <= keys
    assert blocks is None or "data" in blocks


def test_shared_history_is_counted_apart_from_the_local_store(shared_remote, capsys, monkeypatch, spawned):
    from wscontext.cli.main import main
    from wscontext.config import SyncConfig
    from wscontext.history import sync
    _, a, _ = shared_remote
    monkeypatch.chdir(a)
    (a / ".wscontext").mkdir()
    (a / ".wscontext" / "config.toml").write_text(
        '[sync]\nremote = "origin"\n[store]\nstore_warn_bytes = 100\n')
    store = StorePaths.resolve(a)
    commit_paths(store, add_session(store, "sess-one"), "one")
    sync.push(store, Config(sync=SyncConfig(remote="origin")))
    capsys.readouterr()
    assert main(["stats", "data", "--json"]) == 0
    blocks = json.loads(capsys.readouterr().out)["results"]
    volume = next(b for b in blocks if "shared_history" in b)
    assert volume["shared_history"]["bytes"] > 0
    assert volume["shared_history"]["groups"].keys() >= {"raw/sessions", "records"}
    scopes = {w["scope"] for w in volume["warnings"]}
    assert {"local store", "shared history"} <= scopes
    shared = next(w for w in volume["warnings"] if w["scope"] == "shared history")
    assert shared["largest_sessions"][0]["name"] == "sess-one"


def snapshot(world, sid: str, stamp: str, size: int) -> Path:
    path = world.store.raw / "sessions" / f"2026-01-01-pc{stamp}-{sid}.jsonl"
    path.write_bytes(b"x" * size)
    return path


def test_precompact_snapshots_are_their_own_figure(world, capsys):
    snapshot(world, "sess-one", "20260101T100000Z", 3000)
    snapshot(world, "sess-one", "20260101T110000Z", 2000)
    commit_paths(world.store, [snapshot(world, "sess-one", "20260101T120000Z", 1000)], "snap")
    d = stats(world, capsys, "data")[0]
    assert d["local"]["snapshots"] == {"bytes": 6000, "files": 3}
    assert d["local"]["groups"]["raw/snapshots"] == 6000
    assert d["history_branch"]["snapshots"] == {"bytes": 1000, "files": 1}
    assert d["history_branch"]["groups"]["raw/snapshots"] == 1000
    assert d["shared_history"]["snapshots"] == {"bytes": 0, "files": 0}
    # snapshots are not counted as the session's own copy
    assert d["transcript"]["sessions_measured"] == 1 and sum(d["transcript"]["kinds"].values()) < 3000
    _, out, _ = world.ctx(capsys, "stats", "data", "--full")
    assert "PreCompact snapshots 5.9 KB in 3 files; 0 raw files pruned" in out
    assert "PreCompact snapshots 1000 B in 1 files" in out


def test_snapshot_bytes_per_session(world, capsys):
    snapshot(world, "sess-one", "20260101T100000Z", 3000)
    d = stats(world, capsys, "data")[0]
    assert d["per_session"][0]["snapshot_bytes"] == 3000
    _, out, _ = world.ctx(capsys, "stats", "data", "--full")
    assert "session   stored  snapshots  source" in out and "sess-one  " in out and "2.9 KB" in out
    s = stats(world, capsys, "session", "sess-one")[0]
    assert s["volume"]["snapshot_bytes"] == 3000
    _, out, _ = world.ctx(capsys, "stats", "session", "sess-one")
    assert "PreCompact snapshots: 2.9 KB" in out
    schema2(world, "sess-none", transcript_bytes={"prompt": 5, "stored": 5})
    assert stats(world, capsys, "session", "sess-none")[0]["volume"]["snapshot_bytes"] == 0


def test_local_store_warning_can_name_snapshots(world, capsys):
    set_config(world, "[store]\nstore_warn_bytes = 5000\n")
    snapshot(world, "sess-one", "20260101T100000Z", 50000)
    warning = next(w for w in stats(world, capsys, "data")[0]["warnings"]
                   if w["scope"] == "local store")
    assert warning["largest"][0] == {"name": "raw/snapshots", "bytes": 50000}
    assert warning["largest_kinds"][0] == {"name": "snapshot", "bytes": 50000}
    assert warning["largest_sessions"][0]["name"] == "sess-one"
    _, out, _ = world.ctx(capsys, "stats", "data", "--full")
    assert "largest: raw/snapshots 48.8 KB" in out and "by kind: snapshot 48.8 KB" in out
