import dataclasses
import os
import sqlite3

import pytest

from index_support import add_session, store  # noqa: F401
from wscontext.config import Config, DocumentsConfig
from wscontext.index.build import DOCUMENT_MAX_BYTES, index_docs, refresh
from wscontext.index.db import SCHEMA_VERSION, IndexUnavailable, connect, db_path
from wscontext.index.docs import sections
from wscontext.index.search import prompt_hits, search
from wscontext.store.records import SessionRecord, write_session


def cfg(*paths: str) -> Config:
    return dataclasses.replace(Config(), documents=DocumentsConfig(paths=tuple(paths)))


def write(store, path: str, text: str) -> None:
    file = store.root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


def find(store, text, **kw):
    conn = connect(store, readonly=True)
    try:
        return search(conn, text, **kw)
    finally:
        conn.close()


def doc_rows(store):
    conn = sqlite3.connect(db_path(store))
    try:
        return conn.execute("SELECT rowid, ref, prompt, reply FROM docs_fts "
                            "WHERE kind='document' ORDER BY rowid").fetchall()
    finally:
        conn.close()


# sections

def test_sections_levels_and_lines():
    text = "# One\nbody one\n## Two\nbody two\n\n### Three\n"
    got = [(s.heading, s.line, s.text) for s in sections(text, "a.md")]
    assert got == [("One", 1, "body one"), ("Two", 3, "body two"), ("Three", 6, "")]


def test_sections_hash_in_fence_is_not_a_heading():
    text = "# Top\n```sh\n# not a heading\n```\n~~~\n## nor this\n~~~\nafter\n"
    got = sections(text, "a.md")
    assert [s.heading for s in got] == ["Top"]
    assert "# not a heading" in got[0].text and "after" in got[0].text


def test_sections_text_before_first_heading_named_by_file():
    got = sections("intro text\n\n# Head\nx\n", "docs/guide.md")
    assert [(s.heading, s.line, s.text) for s in got] == [
        ("guide.md", 1, "intro text"), ("Head", 3, "x")]


def test_sections_empty_section_dropped_and_no_blank_preamble():
    assert sections("\n\n# Head\nx\n", "a.md")[0].heading == "Head"
    assert sections("# \n\n", "a.md") == []
    assert sections("", "a.md") == []


# index_docs

def test_doc_indexed_and_found(store):
    write(store, "docs/a.md", "# Rotation\nRotate the signing keys monthly.\n")
    result = index_docs(store, cfg("docs/a.md"))
    assert result["documents_indexed"] == 1 and not result["locked"]
    hits = find(store, "signing keys", kind="document")
    assert [(h.kind, h.ref, h.title, h.session_id, h.turn) for h in hits] == [
        ("document", "docs/a.md:1", "Rotation", None, None)]
    assert "signing" in hits[0].snippet


def test_edit_replaces_text(store):
    write(store, "docs/a.md", "# H\nalpha zebra text\n")
    index_docs(store, cfg("docs/a.md"))
    write(store, "docs/a.md", "# H\nbeta giraffe text, longer\n")
    assert index_docs(store, cfg("docs/a.md"))["documents_indexed"] == 1
    assert find(store, "giraffe", kind="document")
    assert not find(store, "zebra", kind="document")


def test_path_dropped_from_list_removes_rows(store):
    write(store, "a.md", "# H\nalpha\n")
    write(store, "b.md", "# H\nbeta\n")
    index_docs(store, cfg("a.md", "b.md"))
    result = index_docs(store, cfg("a.md"))
    assert result["documents_removed"] == 1
    assert [r[1] for r in doc_rows(store)] == ["a.md:1"]
    index_docs(store, cfg())
    assert doc_rows(store) == []


def test_deleted_file_removes_rows(store):
    write(store, "a.md", "# H\nalpha\n")
    index_docs(store, cfg("a.md"))
    (store.root / "a.md").unlink()
    result = index_docs(store, cfg("a.md"))
    assert result["documents_removed"] == 1 and result["skipped"] == ["a.md"]
    assert doc_rows(store) == []


def test_unchanged_file_is_not_rewritten(store):
    write(store, "a.md", "# H\nalpha\n## I\nbeta\n")
    index_docs(store, cfg("a.md"))
    before = doc_rows(store)
    assert index_docs(store, cfg("a.md"))["documents_indexed"] == 0
    assert doc_rows(store) == before
    # same bytes, new mtime: still no rewrite
    os.utime(store.root / "a.md", ns=(1, 1))
    assert index_docs(store, cfg("a.md"))["documents_indexed"] == 0
    assert doc_rows(store) == before


def test_large_file_and_non_files_skipped(store):
    write(store, "big.md", "# H\n" + "x" * DOCUMENT_MAX_BYTES)
    (store.root / "dir.md").mkdir()
    result = index_docs(store, cfg("big.md", "dir.md", "missing.md", "../outside.md"))
    assert result["documents_indexed"] == 0
    assert result["skipped"] == ["big.md", "dir.md", "missing.md", "../outside.md"]
    assert doc_rows(store) == []


def test_secret_in_section_is_redacted(store):
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    write(store, "a.md", f"# Setup {secret}\nexport the credential {secret} before deploy\n")
    index_docs(store, cfg("a.md"))
    rows = doc_rows(store)
    assert secret not in rows[0][2] + rows[0][3]
    hits = find(store, "credential deploy", kind="document")
    assert "[REDACTED:" in hits[0].snippet and secret not in hits[0].snippet


def test_rebuild_leaves_documents_to_index_docs(store):
    write(store, "a.md", "# H\nalpha\n")
    index_docs(store, cfg("a.md"))
    from wscontext.index.build import rebuild
    rebuild(store)
    assert doc_rows(store) == []
    index_docs(store, cfg("a.md"))
    assert len(doc_rows(store)) == 1


def test_schema_3_index_is_recreated(store):
    add_session(store, "s1", [("hello there", "reply", "")])
    refresh(store)
    conn = sqlite3.connect(db_path(store))
    conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(IndexUnavailable):
        connect(store, readonly=True)
    refresh(store)
    conn = connect(store, readonly=True)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(
        SCHEMA_VERSION)
    assert conn.execute("SELECT imported FROM sessions").fetchone() == (0,)
    conn.close()


# search

def test_search_kind_filters_and_filters(store):
    add_session(store, "s1", [("rotate the signing keys", "rotated signing keys", "")],
                branch="main", author="ann", files=["src/keys.py"])
    write(store, "docs/keys.md", "# Keys\nSigning keys rotate monthly.\n")
    refresh(store)
    index_docs(store, cfg("docs/keys.md"))
    assert {h.kind for h in find(store, "signing keys")} == {"session", "document"}
    assert {h.kind for h in find(store, "signing keys", kind="document")} == {"document"}
    assert {h.kind for h in find(store, "signing keys", kind="session")} == {"session"}
    for kw in ({"branch": "main"}, {"author": "ann"}, {"since": "2020-01-01"}):
        assert {h.kind for h in find(store, "signing keys", **kw)} == {"session"}
    assert {h.kind for h in find(store, "signing keys", file="docs/keys.md")} == {"document"}
    assert {h.kind for h in find(store, "signing keys", file="src/keys.py")} == {"session"}


def test_sessions_rank_as_before_without_document_match(store):
    add_session(store, "s1", [("rotate signing keys", "Run the rotate script.", "")])
    add_session(store, "s2", [("rotate the logs", "logrotate config done", "")])
    refresh(store)
    before = [(h.kind, h.ref, h.score, h.snippet) for h in find(store, "rotate keys")]
    write(store, "a.md", "# Cooking\nBoil the pasta for ten minutes.\n")
    index_docs(store, cfg("a.md"))
    after = [(h.kind, h.ref, h.snippet) for h in find(store, "rotate keys")]
    assert after == [(k, r, s) for k, r, _, s in before]


def test_imported_flag_reaches_hit(store):
    add_session(store, "s1", [("rotate signing keys", "done", "")])
    add_session(store, "s2", [("rotate signing keys again", "done", "")])
    write_session(store, SessionRecord(session_id="s2", author="ann", branch="main",
                                       started_at="2026-02-01T09:00:00Z",
                                       ended_at="2026-02-01T10:00:00Z",
                                       imported_at="2026-03-01T00:00:00Z"), Config())
    refresh(store)
    assert {h.ref: h.imported for h in find(store, "rotate signing keys")} == {
        "s1": False, "s2": True}


# prompt brief

def test_prompt_brief_ignores_document_rows(store):
    from test_prompt_hits import PROMPT, build
    build(store)
    before = prompt_hits(store, PROMPT)
    assert before
    for i in range(10):
        write(store, f"docs/d{i}.md",
              f"# Signing keys {i}\nThe signing keys rotation script on the staging cluster.\n")
    index_docs(store, cfg(*[f"docs/d{i}.md" for i in range(10)]))
    assert len(doc_rows(store)) == 10
    # bm25 scores shift with the table's size; what the brief prints does not
    after = prompt_hits(store, PROMPT)
    strip = lambda hits: [dataclasses.replace(h, score=0.0) for h in hits]  # noqa: E731
    assert strip(after) == strip(before)
