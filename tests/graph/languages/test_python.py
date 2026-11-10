from pathlib import Path

import pytest

from wscontext.config import Config, GraphConfig
from wscontext.graph import db, parse
from wscontext.graph.build import refresh
from wscontext.graph.languages.base import Import, RepoFiles
from wscontext.graph.languages.python import PART
from wscontext.store.paths import StorePaths

SAMPLES = Path(__file__).resolve().parents[2] / "fixtures" / "graph" / "python"


def sample(name: str):
    return parse.facts(PART, (SAMPLES / name).read_bytes(), name)


def test_definitions_methods_nested_functions_and_decorators():
    defs = {d.qualname: d for d in sample("shapes.py").definitions}

    assert {q: d.kind for q, d in defs.items()} == {
        "helper": "function", "cached": "function", "cached.inner": "function",
        "Shape": "class", "Shape.__init__": "method", "Shape.area": "method",
        "Shape.fetch": "method", "Shape.Inner": "class", "Shape.Inner.method": "method",
        "Square": "class", "Square.area": "method"}
    # a decorated definition starts at its first decorator
    assert (defs["cached"].start, defs["cached"].end) == (10, 15)
    assert (defs["Shape.area"].start, defs["Square"].start) == (24, 39)
    assert defs["cached.inner"].parent == "cached"
    assert defs["Shape.Inner.method"].parent == "Shape.Inner"
    assert defs["Shape.fetch"].signature == "async def fetch(self, url: str,) -> bytes"
    assert defs["Shape"].signature == "class Shape(Base, metaclass=Meta)"


def test_calls_carry_their_enclosing_definition():
    calls = {(c.name, c.line): c.caller for c in sample("shapes.py").calls}

    assert calls[("normalize", 7)] == "helper"
    assert calls[("helper", 13)] == "cached.inner"
    # a default value and a decorator run in the enclosing scope
    assert calls[("helper", 11)] is None
    assert calls[("register", 39)] is None
    # a class body is its own caller; `obj.f()` and `Cls()` count by short name
    assert calls[("make_registry", 19)] == "Shape"
    assert calls[("get", 32)] == "Shape.fetch"
    assert calls[("Shape", 36)] == "Shape.Inner.method"
    assert calls[("strip", 22)] == "Shape.__init__"
    # `functools.cache` without parentheses is not a call
    assert ("cache", 9) not in calls


def test_imports_absolute_aliased_relative_wildcard_and_lazy():
    imports = [(i.spec, i.names, i.line) for i in sample("imports.py").imports]

    assert imports == [
        ("os", (), 3), ("os.path", (), 4), ("json", (), 4),
        ("pkg.core", ("Engine", "run"), 5), (".", ("sibling",), 6),
        (".util", ("tools",), 7), ("..parent.mod", ("*",), 8), ("pkg.lazy", (), 12)]


FILES = RepoFiles([
    "src/pkg/__init__.py", "src/pkg/core.py", "src/pkg/sub/__init__.py", "src/pkg/sub/a.py",
    "src/pkg/sub/b.py", "tests/fixtures/pkg/core.py", "ns/mod.py", "tool.py"])


def resolve(spec, names=(), from_path="src/pkg/sub/a.py"):
    return PART.resolve_import(Import(spec, tuple(names), 1), from_path, FILES)


def test_absolute_imports_resolve_by_path_suffix_so_src_layouts_resolve():
    # the shallowest match wins over a copy deeper in the tree
    assert resolve("pkg.core") == ["src/pkg/core.py"]
    assert resolve("pkg") == ["src/pkg/__init__.py"]
    assert resolve("pkg", ["core"]) == ["src/pkg/core.py"]
    assert resolve("pkg", ["Engine"]) == ["src/pkg/__init__.py"]
    assert resolve("pkg", ["core", "Engine"]) == ["src/pkg/__init__.py", "src/pkg/core.py"]
    assert resolve("pkg.sub", ["*"]) == ["src/pkg/sub/__init__.py"]
    # a namespace package has no module file, only submodules
    assert resolve("ns", ["mod"]) == ["ns/mod.py"]
    assert resolve("ns") == []
    assert resolve("tool") == ["tool.py"]
    assert resolve("os.path") == []


def test_relative_imports_resolve_from_the_importing_file():
    assert resolve(".", ["b"]) == ["src/pkg/sub/b.py"]
    assert resolve(".", ["name"]) == ["src/pkg/sub/__init__.py"]
    assert resolve(".b") == ["src/pkg/sub/b.py"]
    assert resolve("..", ["core"]) == ["src/pkg/core.py"]
    assert resolve("..core", ["Engine"]) == ["src/pkg/core.py"]
    assert resolve(".", ["a"], from_path="src/pkg/sub/__init__.py") == ["src/pkg/sub/a.py"]
    assert resolve("." * 9, ["x"]) == []
    assert resolve(".missing") == []


@pytest.mark.parametrize("path,expected", [
    ("test_x.py", True), ("pkg/x_test.py", True), ("tests/helpers.py", True),
    ("src/test/util.py", True), ("src/testing.py", False), ("src/contest.py", False),
    ("latest_tests.py", False)])
def test_is_test(path, expected):
    assert PART.is_test(path) is expected


def test_a_syntax_error_is_recorded_as_error_and_keeps_the_facts_around_it(git_repo):
    store = StorePaths.resolve(git_repo)
    (git_repo / "broken.py").write_bytes((SAMPLES / "broken.py").read_bytes())
    (git_repo / "good.py").write_text("def fine():\n    pass\n")

    result = refresh(store, Config(graph=GraphConfig(languages=("python",))))

    assert result.complete and result.parse_errors == ("broken.py",)
    conn = db.connect(store, readonly=True)
    try:
        assert conn.execute("SELECT path, status, error FROM files ORDER BY path").fetchall() == [
            ("broken.py", "error", "syntax error at line 5"), ("good.py", "ok", None)]
        assert conn.execute("SELECT qualname FROM symbols WHERE path='broken.py' "
                            "ORDER BY start_line").fetchall() == [("good",), ("bad",), ("after",)]
    finally:
        conn.close()
