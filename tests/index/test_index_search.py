from index_support import add_commit, add_session, store  # noqa: F401
from wscontext.index.build import refresh
from wscontext.index.db import connect
from wscontext.index.search import search


def run(store, text, **kw):
    conn = connect(store, readonly=True)
    try:
        return search(conn, text, **kw)
    finally:
        conn.close()


def build(store):
    add_session(store, "s1", [("how to rotate signing keys", "Run the rotate script.", ""),
                              ("unrelated question", "unrelated", "")],
                branch="main", author="ann", files=["src/keys.py"], date="2026-01-10")
    add_session(store, "s2", [("rotate the logs", "logrotate config done", "")],
                branch="feat", author="bob", files=["etc/log.conf"], date="2026-02-10",
                ended=False)
    add_session(store, "s3", [("nothing to see", "the reply mentions keys rotation", "")],
                branch="main", author="bob", date="2026-02-20")
    add_commit(store, "Rotate signing keys monthly", ["s1"])
    refresh(store)


def test_one_hit_per_session_with_best_exchange_and_snippet(store):
    build(store)
    hits = run(store, "rotate signing keys", kind="session")
    top = hits[0]
    assert (top.ref, top.turn) == ("s1", 0)
    assert "rotate" in top.snippet.lower()
    assert len({(h.kind, h.ref) for h in hits}) == len(hits)


def test_and_then_or(store):
    build(store)
    # only s1 has all three words; OR fills the rest
    hits = run(store, "rotate signing keys", kind="session", limit=10)
    assert hits[0].ref == "s1" and len(hits) > 1
    # with limit 1 the AND hit is enough and nothing else is added
    assert [h.ref for h in run(store, "rotate signing keys", kind="session", limit=1)] == ["s1"]


def test_weights_prefer_prompt_over_reply(store):
    build(store)
    refs = [h.ref for h in run(store, "rotate keys", kind="session", limit=1) ]
    assert refs == ["s1"]
    # a prompt match outranks a reply-only match of the same term
    both = run(store, "keys rotation", kind="session")
    assert both[0].ref == "s1"


def test_filters(store):
    build(store)
    assert {h.ref for h in run(store, "rotate", branch="feat")} == {"s2"}
    assert {h.ref for h in run(store, "rotate", author="ann", kind="session")} == {"s1"}
    assert {h.ref for h in run(store, "rotate", since="2026-02-05")} == {"s2", "s3"}
    assert {h.ref for h in run(store, "rotate", file="keys.py", kind="session")} == {"s1"}
    kinds = {h.kind for h in run(store, "rotate signing keys")}
    assert kinds == {"session", "commit"}
    commit = run(store, "rotate signing", kind="commit")
    assert len(commit) == 1 and commit[0].branch == "main" and commit[0].author == "ann"
    assert run(store, "rotate", file="keys.py", kind="commit")[0].kind == "commit"


def test_active_flag_and_empty_query(store):
    build(store)
    assert run(store, "signing", kind="session")[0].active is False
    assert run(store, "rotate", branch="feat")[0].active is True
    assert run(store, "   ") == []
    assert run(store, 'quote " AND OR (') == []


def test_special_characters_do_not_break_the_query(store):
    build(store)
    assert {h.ref for h in run(store, 'rotate" NEAR(keys', kind="session")} >= {"s1"}
