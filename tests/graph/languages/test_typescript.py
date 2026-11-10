from pathlib import Path

import pytest

from wscontext.config import Config, GraphConfig
from wscontext.graph import db, parse
from wscontext.graph.build import refresh
from wscontext.graph.languages.base import Import, RepoFiles
from wscontext.graph.languages.typescript import PART
from wscontext.store.paths import StorePaths

SAMPLES = Path(__file__).resolve().parents[2] / "fixtures" / "graph" / "typescript"


def sample(name: str):
    return parse.facts(PART, (SAMPLES / name).read_bytes(), name)


def test_definitions_methods_types_and_decorators():
    defs = {d.qualname: d for d in sample("shapes.ts").definitions}

    assert {q: d.kind for q, d in defs.items()} == {
        "Drawable": "type", "Point": "type", "Color": "type", "helper": "function",
        "area": "function", "area.inner": "function", "short": "function", "a": "function",
        "b": "function", "Shape": "class", "Shape.handler": "method",
        "Shape.constructor": "method", "Shape.area": "method", "Shape.label": "method",
        "Shape.#secret": "method", "unit": "function", "over": "function"}
    # an exported or decorated declaration starts at `export` or its first decorator
    assert (defs["Shape"].start, defs["Shape"].end) == (29, 56)
    assert (defs["helper"].start, defs["helper"].end) == (14, 16)
    assert defs["Shape.area"].start == 39
    assert defs["area.inner"].parent == "area"
    assert defs["Shape.handler"].parent == "Shape"
    assert defs["area"].signature == "const area = async (w: number, h: number): Promise<number>"
    assert defs["Drawable"].signature == "interface Drawable<T> extends Base"
    assert defs["Point"].signature == "type Point = { x: number; y: number }"
    assert defs["Shape"].signature == "abstract class Shape<T> extends Base implements Drawable<T>"
    # two declarators in one statement are two definitions on their own line
    assert (defs["a"].start, defs["b"].start) == (27, 27)
    # overloads without a body are not definitions; the implementation is
    assert (defs["over"].start, defs["over"].end) == (68, 70)
    # an abstract method, a computed name and an anonymous default export are not named
    assert not {"Shape.perimeter", "Shape.[Symbol.iterator]"} & set(defs)


def test_calls_carry_their_enclosing_definition():
    calls = {(c.name, c.line): c.caller for c in sample("shapes.ts").calls}

    assert calls[("normalize", 14)] is None
    assert calls[("trim", 15)] == "helper"
    assert calls[("helper", 20)] == "area.inner"
    assert calls[("inner", 22)] == "area"
    assert calls[("double", 25)] == "short"
    assert calls[("a", 27)] == "b"
    # an enum, a class body and a decorator run in their own or the enclosing scope
    assert calls[("paint", 10)] == "Color"
    assert calls[("make", 31)] == "Shape"
    assert calls[("memo", 39)] == "Shape"
    # `obj.f()`, `new Cls()` and `a?.f()` count by short name; `super()` does not
    assert calls[("add", 36)] == "Shape.constructor"
    assert calls[("Point3D", 52)] == "Shape.#secret"
    assert calls[("Shape", 62)] == "unit"
    assert calls[("toString", 69)] == "over"
    assert calls[("register", 72)] is None
    assert not [c for c in calls if c[0] == "super"]


def test_imports_exports_require_and_dynamic_import():
    imports = [(i.spec, i.names, i.line) for i in sample("imports.ts").imports]

    assert imports == [
        ("./default", ("default",), 1), ("./ns", ("*",), 2), ("./named", ("a", "b"), 3),
        ("./types", ("T",), 4), ("fs", (), 5), ("./side-effect", (), 6), ("./all", ("*",), 7),
        ("./tools", ("*",), 8), ("./reexport", ("x", "y"), 9), ("./cjs", (), 11),
        ("./destructured", (), 12), ("./lazy", (), 16)]
    # `require(name)` is not a literal: a call, not an import
    assert [(c.name, c.line) for c in sample("imports.ts").calls] == [("require", 13)]


def test_tsx_uses_its_own_grammar():
    facts = sample("view.tsx")

    assert facts.error is None
    assert [(d.qualname, d.kind) for d in facts.definitions] == [
        ("View", "function"), ("render", "function")]
    assert ("format", 9, "render") in [(c.name, c.line, c.caller) for c in facts.calls]


FILES = RepoFiles([
    "src/app.ts", "src/util.ts", "src/util.test.ts", "src/lib/index.ts", "src/lib/a.tsx",
    "src/legacy.js", "src/both.ts", "src/both/index.ts", "src/data.json", "src/old.mjs",
    "src/old.mts", "index.ts", "src/x.d.ts"])


def resolve(spec, from_path="src/app.ts"):
    return PART.resolve_import(Import(spec, (), 1), from_path, FILES)


def test_relative_specifiers_probe_extensions_and_index():
    assert resolve("./util") == ["src/util.ts"]
    assert resolve("./lib") == ["src/lib/index.ts"]
    assert resolve("./lib/a") == ["src/lib/a.tsx"]
    assert resolve("./legacy") == ["src/legacy.js"]
    assert resolve("../index", "src/lib/a.tsx") == []
    assert resolve("../../index", "src/lib/a.tsx") == ["index.ts"]
    assert resolve("../util", "src/lib/a.tsx") == ["src/util.ts"]
    assert resolve(".", "src/lib/a.tsx") == ["src/lib/index.ts"]
    assert resolve("..", "src/lib/a.tsx") == []
    # a file wins over a directory of the same name
    assert resolve("./both") == ["src/both.ts"]


def test_a_written_extension_resolves_and_js_names_find_their_typescript_source():
    assert resolve("./util.ts") == ["src/util.ts"]
    assert resolve("./util.js") == ["src/util.ts"]
    assert resolve("./legacy.js") == ["src/legacy.js"]
    assert resolve("./old.mjs") == ["src/old.mjs"]
    assert resolve("./lib/a.jsx") == ["src/lib/a.tsx"]


def test_bare_packages_aliases_and_missing_files_resolve_to_nothing():
    assert resolve("react") == []
    assert resolve("@scope/pkg") == []
    assert resolve("@/util") == []
    assert resolve("./missing") == []
    assert resolve("../../../escape") == []
    assert resolve("./data.json") == []
    assert resolve("") == []


@pytest.mark.parametrize("path,expected", [
    ("src/util.test.ts", True), ("a/b.spec.tsx", True), ("src/__tests__/x.ts", True),
    ("__tests__/x.ts", True), ("src/util.ts", False), ("src/test/util.ts", False),
    ("src/contest.ts", False), ("src/testing.ts", False), ("src/spec.ts", False),
    ("src/a.test.d.ts", True)])
def test_is_test(path, expected):
    assert PART.is_test(path) is expected


def test_a_syntax_error_is_recorded_as_error_and_keeps_the_facts_around_it(git_repo):
    store = StorePaths.resolve(git_repo)
    (git_repo / "broken.ts").write_bytes((SAMPLES / "broken.ts").read_bytes())
    (git_repo / "good.ts").write_text("export function fine(): void {}\n")

    result = refresh(store, Config(graph=GraphConfig(languages=("typescript",))))

    assert result.complete and result.parse_errors == ("broken.ts",)
    conn = db.connect(store, readonly=True)
    try:
        assert conn.execute("SELECT path, status, error FROM files ORDER BY path").fetchall() == [
            ("broken.ts", "error", "syntax error at line 5"), ("good.ts", "ok", None)]
        assert conn.execute("SELECT qualname FROM symbols WHERE path='broken.ts' "
                            "ORDER BY start_line").fetchall() == [("good",), ("bad",), ("after",)]
    finally:
        conn.close()
