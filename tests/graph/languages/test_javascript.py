from pathlib import Path

import pytest

from wscontext.config import Config, GraphConfig
from wscontext.graph import db, parse
from wscontext.graph.build import refresh
from wscontext.graph.languages.base import Import, RepoFiles
from wscontext.graph.languages.javascript import PART
from wscontext.store.paths import StorePaths

SAMPLES = Path(__file__).resolve().parents[2] / "fixtures" / "graph" / "javascript"


def sample(name: str):
    return parse.facts(PART, (SAMPLES / name).read_bytes(), name)


def test_definitions_methods_and_function_values():
    defs = {d.qualname: d for d in sample("shapes.js").definitions}

    assert {q: d.kind for q, d in defs.items()} == {
        "helper": "function", "ids": "function", "area": "function", "area.inner": "function",
        "short": "function", "a": "function", "b": "function", "Shape": "class",
        "Shape.handler": "method", "Shape.constructor": "method", "Shape.area": "method",
        "Shape.label": "method", "Shape.create": "method", "Shape.#secret": "method"}
    assert (defs["Shape"].start, defs["Shape"].end) == (20, 46)
    assert (defs["area"].start, defs["area"].end) == (9, 14)
    assert defs["area.inner"].parent == "area"
    assert defs["ids"].signature == "function* ids()"
    assert defs["a"].signature == "let a = ()"
    assert defs["Shape.handler"].signature == "handler = (e)"
    assert defs["Shape"].signature == "class Shape extends Base"
    # a computed method name is not a definition
    assert not [q for q in defs if "Symbol" in q]


def test_calls_carry_their_enclosing_definition():
    calls = {(c.name, c.line): c.caller for c in sample("shapes.js").calls}

    assert calls[("normalize", 1)] is None
    assert calls[("trim", 2)] == "helper"
    assert calls[("next", 6)] == "ids"
    assert calls[("helper", 11)] == "area.inner"
    assert calls[("double", 16)] == "short"
    assert calls[("make", 21)] == "Shape"
    assert calls[("onEvent", 22)] == "Shape.handler"
    assert calls[("add", 26)] == "Shape.constructor"
    assert calls[("Shape", 38)] == "Shape.create"
    assert calls[("Point3D", 42)] == "Shape.#secret"
    assert calls[("register", 50)] is None
    assert not [c for c in calls if c[0] == "super"]


def test_imports_exports_require_and_dynamic_import():
    facts = sample("imports.js")

    assert [(i.spec, i.names, i.line) for i in facts.imports] == [
        ("./default", ("default",), 1), ("./ns", ("*",), 2), ("./named", ("a", "b"), 3),
        ("./side-effect", (), 4), ("./all", ("*",), 5), ("./tools", ("*",), 6),
        ("./reexport", ("x", "y"), 7), ("./cjs", (), 9), ("./destructured", (), 10),
        ("./lazy", (), 14)]
    assert [(c.name, c.line) for c in facts.calls] == [("require", 11)]


def test_jsx_parses_with_the_javascript_grammar():
    facts = sample("view.jsx")

    assert facts.error is None
    assert [(d.qualname, d.kind) for d in facts.definitions] == [
        ("View", "function"), ("render", "function")]
    assert ("useState", 4, "View") in [(c.name, c.line, c.caller) for c in facts.calls]


FILES = RepoFiles([
    "src/app.js", "src/util.js", "src/lib/index.js", "src/lib/a.jsx", "src/old.mjs",
    "src/cjs.cjs", "src/typed.ts", "src/data.json"])


def resolve(spec, from_path="src/app.js"):
    return PART.resolve_import(Import(spec, (), 1), from_path, FILES)


def test_relative_specifiers_probe_extensions_and_index():
    assert resolve("./util") == ["src/util.js"]
    assert resolve("./util.js") == ["src/util.js"]
    assert resolve("./lib") == ["src/lib/index.js"]
    assert resolve("./lib/a") == ["src/lib/a.jsx"]
    assert resolve("./old.mjs") == ["src/old.mjs"]
    assert resolve("./cjs") == ["src/cjs.cjs"]
    assert resolve("../util", "src/lib/a.jsx") == ["src/util.js"]
    assert resolve("./typed") == ["src/typed.ts"]


def test_bare_packages_and_missing_files_resolve_to_nothing():
    assert resolve("react") == []
    assert resolve("lodash/fp") == []
    assert resolve("./missing") == []
    assert resolve("./data.json") == []
    assert resolve("../../../escape") == []


@pytest.mark.parametrize("path,expected", [
    ("src/util.test.js", True), ("a/b.spec.jsx", True), ("src/__tests__/x.js", True),
    ("src/util.js", False), ("src/test/util.js", False), ("src/contest.js", False),
    ("src/spec.js", False)])
def test_is_test(path, expected):
    assert PART.is_test(path) is expected


def test_a_syntax_error_is_recorded_as_error_and_keeps_the_facts_around_it(git_repo):
    store = StorePaths.resolve(git_repo)
    (git_repo / "broken.js").write_bytes((SAMPLES / "broken.js").read_bytes())
    (git_repo / "good.js").write_text("export function fine() {}\n")

    result = refresh(store, Config(graph=GraphConfig(languages=("javascript",))))

    assert result.complete and result.parse_errors == ("broken.js",)
    conn = db.connect(store, readonly=True)
    try:
        assert conn.execute("SELECT path, status, error FROM files ORDER BY path").fetchall() == [
            ("broken.js", "error", "syntax error at line 6"), ("good.js", "ok", None)]
        assert conn.execute("SELECT qualname FROM symbols WHERE path='broken.js' "
                            "ORDER BY start_line").fetchall() == [("good",)]
    finally:
        conn.close()
