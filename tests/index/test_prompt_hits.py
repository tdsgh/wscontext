import sqlite3

from index_support import add_session, store  # noqa: F401
from wscontext.cli.output import estimate_tokens
from wscontext.index.build import refresh
from wscontext.index.db import EXCHANGE_KEYS, EXCHANGE_STATS, db_path
from wscontext.index.search import prompt_hits

PROMPT = "The signing keys rotation script broke on the staging cluster again, can you look?"

TOPICS = {
    "keys": ("rotate the signing keys on the staging cluster",
             "Rotated the signing keys with the rotation script; the staging cluster reloaded."),
    "keys2": ("rotate the signing keys on the production cluster",
              "Signing keys rotated on the production cluster."),
    "invoice": ("rename the invoice exporter to billing export",
                "Renamed InvoiceExporter to BillingExport everywhere."),
    "sqlite": ("pick a sqlite journal mode", "We chose WAL journal mode for concurrent readers."),
    "backoff": ("make the fetcher back off after a timeout",
                "Added exponential backoff with jitter to the fetcher."),
    "logo": ("update the logo on the landing page",
             "Swapped the svg logo and adjusted the header padding."),
    "deps": ("upgrade the http client dependency",
             "Bumped httpx and fixed the deprecated timeout argument."),
    "docs": ("write the onboarding guide for new contributors",
             "Drafted CONTRIBUTING.md with setup steps."),
    "locale": ("translate error messages to french", "Added a fr catalog for the error messages."),
    "cache": ("cache the rendered templates in memory", "Added an LRU cache keyed by template path."),
    "metrics": ("export request latency metrics to prometheus",
                "Added a histogram for request latency."),
    "auth": ("expire idle login sessions after thirty minutes",
             "Idle sessions now expire; the cookie carries the deadline."),
}


def build(store):
    for sid, (prompt, reply) in TOPICS.items():
        add_session(store, sid, [(prompt, reply, "")])
    refresh(store)


def refs(hits):
    return [h.ref for h in hits]


def test_a_prompt_naming_a_past_topic_returns_that_session_first(store):
    build(store)
    hits = prompt_hits(store, PROMPT)
    assert refs(hits)[0] == "keys" and set(refs(hits)) <= {"keys", "keys2"}
    assert hits[0].title == TOPICS["keys"][0] and hits[0].kind == "session"


def test_a_prompt_with_no_related_past_work_returns_nothing(store):
    build(store)
    assert prompt_hits(store, "Add a dark theme to the settings screen with a toggle") == []
    # one shared word ("page") is never enough
    assert prompt_hits(store, "Add a dark theme to the settings page") == []
    assert prompt_hits(store, "hi") == [] and prompt_hits(store, "") == []


def test_the_current_session_is_never_returned(store):
    build(store)
    assert refs(prompt_hits(store, PROMPT, "keys")) == ["keys2"]
    assert "keys2" not in refs(prompt_hits(store, PROMPT, "keys2"))


def test_the_result_fits_the_token_budget_and_the_limit(store):
    build(store)

    def cost(hits):
        return sum(estimate_tokens(h.title) + estimate_tokens(h.snippet) for h in hits)

    full = prompt_hits(store, PROMPT)
    assert len(full) == 2 and cost(full) <= 300
    assert refs(prompt_hits(store, PROMPT, limit=1)) == ["keys"]
    # room for one title and a few words: the second hit goes, the first one's snippet is cut
    budget = estimate_tokens(full[0].title) + 4
    cut = prompt_hits(store, PROMPT, budget=budget)
    assert refs(cut) == ["keys"] and cost(cut) <= budget
    assert cut[0].snippet.endswith("...") and len(cut[0].snippet) < len(full[0].snippet)
    # not even the title fits
    assert prompt_hits(store, PROMPT, budget=1) == []


def test_a_missing_old_or_broken_index_gives_no_hits(store):
    assert prompt_hits(store, PROMPT) == []
    build(store)
    conn = sqlite3.connect(db_path(store))
    conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    assert prompt_hits(store, PROMPT) == []
    for suffix in ("-wal", "-shm"):
        db_path(store).with_name(db_path(store).name + suffix).unlink(missing_ok=True)
    db_path(store).write_bytes(b"not a database" * 100)
    assert prompt_hits(store, PROMPT) == []


def test_refresh_keeps_exchange_stats_in_meta_and_a_missing_entry_is_computed(store):
    build(store)
    conn = sqlite3.connect(db_path(store))

    def meta():
        return [int(conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()[0])
                for k in EXCHANGE_KEYS]

    assert meta() == list(conn.execute(EXCHANGE_STATS).fetchone())
    assert meta()[0] == len(TOPICS)
    add_session(store, "more", [("one more exchange", "a longer reply than most of the others", ""),
                                ("and another", "done", "")])
    refresh(store)
    assert meta() == list(conn.execute(EXCHANGE_STATS).fetchone()) and meta()[0] == len(TOPICS) + 2
    expected = refs(prompt_hits(store, PROMPT))
    conn.execute(f"DELETE FROM meta WHERE key IN ({','.join('?' * len(EXCHANGE_KEYS))})",
                 EXCHANGE_KEYS)
    conn.commit()
    assert refs(prompt_hits(store, PROMPT)) == expected
    refresh(store)  # nothing changed, but the entry was missing
    assert meta() == list(conn.execute(EXCHANGE_STATS).fetchone())
    conn.close()
