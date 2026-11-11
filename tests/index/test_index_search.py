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


def test_stopwords_are_dropped_unless_nothing_else_is_left(store):
    add_session(store, "talk", [("how was it done, and what was the plan for it", "ok", "")])
    add_session(store, "up", [("uploader retries", "done", "")])
    add_session(store, "x", [("unrelated", "fine", "")])
    add_session(store, "y", [("elsewhere", "fine", "")])
    refresh(store)
    # function words alone would rank "talk" first; only "uploader" is searched
    assert [h.ref for h in run(store, "how was the uploader")] == ["up"]
    assert [h.ref for h in run(store, "how was")] == ["talk"]


def test_a_term_in_half_the_rows_does_not_narrow_and(store):
    for sid, prompt in [("s1", "deploy alpha"), ("s2", "deploy beta"), ("s3", "deploy canary"),
                        ("s4", "canary canary canary checklist"), ("s5", "misc"),
                        ("s6", "misc too")]:
        add_session(store, sid, [(prompt, "ok", "")])
    refresh(store)
    # "deploy" is in 3 of 6 rows, so bm25 gives it no weight: s4 is in the AND tier and ranks first
    assert [h.ref for h in run(store, "deploy canary", limit=1)] == ["s4"]
    assert [h.ref for h in run(store, "deploy", limit=1)] in (["s1"], ["s2"], ["s3"])


def test_a_matching_commit_follows_the_session_it_came_from(store):
    add_session(store, "s1", [("rework the uploader retry backoff with jitter", "done", "")])
    add_session(store, "s2", [("uploader retry backoff notes", "uploader retry backoff", "")])
    add_session(store, "s3", [("unrelated", "fine", "")])
    add_session(store, "s4", [("elsewhere", "fine", "")])
    sha = add_commit(store, "Tune jitter", ["s1"])
    refresh(store)
    query = "uploader retry backoff jitter"
    # on bm25 alone the commit ranks after s2
    commits = run(store, query, kind="commit")
    assert [h.ref for h in commits] == [sha]
    assert [h.ref for h in run(store, query, limit=3)] == ["s1", sha, "s2"]
    assert [h.ref for h in run(store, query, kind="session")] == ["s1", "s2"]
    # the commit is not listed twice, and a limit counts it
    assert [h.ref for h in run(store, query, limit=2)] == ["s1", sha]


def test_a_commit_matching_only_weightless_terms_keeps_its_own_rank(store):
    for sid, prompt in [("s1", "uploader notes"), ("s2", "uploader retry notes"), ("s3", "notes"),
                        ("s4", "notes misc"), ("s5", "misc")]:
        add_session(store, sid, [(prompt, "ok", "")])
    sha = add_commit(store, "Write notes", ["s1"])
    refresh(store)
    refs = [h.ref for h in run(store, "uploader notes")]
    # "notes" is in 5 of 6 rows: the commit matches it but is not pulled up behind s1
    assert set(refs[:2]) == {"s1", "s2"} and sha in refs[2:]


def test_a_tool_name_alone_does_not_match_a_session(store):
    add_session(store, "only-tool", [("check the disk", "done", "df -h")], date="2026-03-01")
    add_session(store, "in-command", [("check the shell", "done", "bash -c 'echo hi'")],
                date="2026-03-02")
    add_session(store, "in-prompt", [("why does bash fail here", "it is quoting", "")],
                date="2026-03-03")
    refresh(store)
    refs = {h.ref for h in run(store, "bash", kind="session", limit=10)}
    assert refs == {"in-command", "in-prompt"}
