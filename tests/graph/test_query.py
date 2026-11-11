import pytest

from graph_support import config, store, unknown_language, write  # noqa: F401
from wscontext.graph import db, query
from wscontext.graph.build import refresh

pytestmark = pytest.mark.usefixtures("unknown_language")

SHAPES = """\
class Shape:
    def area(self):
        return 0

    def render(self):
        return self.area()


def area_of(shape):
    return shape.area()
"""

PLAN = """\
from pkg.shapes import Shape


def area():
    return Shape().area()


class Planner:
    def area(self):
        return area()
"""


@pytest.fixture
def conn(store):
    write(store, "pkg/__init__.py", "")
    write(store, "pkg/shapes.py", SHAPES)
    write(store, "plan.py", PLAN)
    write(store, "broken.py", "def ok():\n    pass\n\ndef (:\n")
    refresh(store, config())
    c = db.connect(store, readonly=True)
    yield c
    c.close()


def where(symbols):
    return [(s.path, s.qualname) for s in symbols]


def test_definitions_put_exact_matches_first_then_names_containing_the_text(conn):
    found = query.definitions(conn, "area")

    assert where(found) == [
        ("pkg/shapes.py", "Shape.area"), ("plan.py", "area"), ("plan.py", "Planner.area"),
        ("pkg/shapes.py", "area_of")]
    first = found[0]
    assert (first.kind, first.start, first.end, first.parent) == ("method", 2, 3, "Shape")
    assert first.signature == "def area(self)"


def test_definitions_match_a_qualname_filter_by_kind_and_honour_the_limit(conn):
    assert where(query.definitions(conn, "Planner.area")) == [("plan.py", "Planner.area")]
    # the exact match is case-sensitive, the containing one is not
    assert where(query.definitions(conn, "AREA", kind="function")) == [
        ("plan.py", "area"), ("pkg/shapes.py", "area_of")]
    assert len(query.definitions(conn, "area", limit=2)) == 2
    every = {q for _, q in where(query.definitions(conn, "a", limit=None))}
    assert every == {"Shape", "Shape.area", "Shape.render", "area_of", "area", "Planner",
                     "Planner.area"}


def test_file_outline_lists_definitions_imports_and_importers(conn):
    outline = query.file_outline(conn, "pkg/shapes.py")

    assert (outline.language, outline.status, outline.error) == ("python", "ok", None)
    assert [(d.qualname, d.start) for d in outline.definitions] == [
        ("Shape", 1), ("Shape.area", 2), ("Shape.render", 5), ("area_of", 9)]
    assert outline.imports == []
    assert [(e.path, e.spec, e.line) for e in outline.importers] == [
        ("plan.py", "pkg.shapes", 1)]
    assert [(e.spec, e.target) for e in query.file_outline(conn, "plan.py").imports] == [
        ("pkg.shapes", "pkg/shapes.py")]


def test_file_outline_says_when_a_file_did_not_parse_and_knows_unknown_files(conn):
    broken = query.file_outline(conn, "broken.py")
    assert broken.status == "error" and broken.error.startswith("syntax error at line")
    assert [d.qualname for d in broken.definitions][:1] == ["ok"]
    assert query.file_outline(conn, "nope.py") is None


def test_resolve_a_file_a_qualname_in_a_file_and_a_short_name_in_a_file(conn):
    assert query.resolve(conn, "plan.py") == query.Resolved(path="plan.py", symbols=[])
    assert query.resolve(conn, "./pkg/shapes.py").path == "pkg/shapes.py"
    assert query.resolve(conn, "docs/notes.md") == query.Resolved(path="docs/notes.md",
                                                                   symbols=[])
    assert where(query.resolve(conn, "plan.py#Planner.area").symbols) == [
        ("plan.py", "Planner.area")]
    # a qualname match wins over the short name
    assert where(query.resolve(conn, "plan.py#area").symbols) == [("plan.py", "area")]
    assert where(query.resolve(conn, "pkg/shapes.py#render").symbols) == [
        ("pkg/shapes.py", "Shape.render")]
    assert query.resolve(conn, "plan.py#missing").symbols == []


def test_resolve_a_name_across_the_repository_gives_one_or_the_candidates(conn):
    one = query.resolve(conn, "#area_of")
    assert one.path is None and where(one.symbols) == [("pkg/shapes.py", "area_of")]
    assert where(query.resolve(conn, "area").symbols) == [
        ("pkg/shapes.py", "Shape.area"), ("plan.py", "area"), ("plan.py", "Planner.area")]
    assert where(query.resolve(conn, "Shape.render").symbols) == [
        ("pkg/shapes.py", "Shape.render")]
    assert query.resolve(conn, "#nothing").symbols == []


def test_callers_of_lists_calls_by_path_and_line_with_the_calling_definition(store):
    write(store, "pkg/__init__.py", "")
    write(store, "pkg/shapes.py", SHAPES)
    write(store, "plan.py", PLAN + "\n\nprint(area())\n")
    refresh(store, config())
    conn = db.connect(store, readonly=True)
    try:
        found = query.callers_of(conn, "area")
        assert [(c.path, c.line, c.caller and c.caller.qualname) for c in found] == [
            ("pkg/shapes.py", 6, "Shape.render"), ("pkg/shapes.py", 10, "area_of"),
            ("plan.py", 5, "area"), ("plan.py", 10, "Planner.area"), ("plan.py", 13, None)]
        method = found[3].caller
        assert (method.name, method.kind, method.start, method.end, method.parent) == (
            "area", "method", 9, 10, "Planner")
        assert method.id is not None
        assert query.callers_of(conn, "nothing_calls_this") == []
    finally:
        conn.close()


def test_name_count_counts_the_definitions_sharing_a_short_name(conn):
    assert query.name_count(conn, "area") == 3
    assert query.name_count(conn, "render") == 1
    assert query.name_count(conn, "missing") == 0
