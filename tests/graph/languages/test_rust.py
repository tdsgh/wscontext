import shutil
from pathlib import Path

import pytest

from wscontext.config import Config, GraphConfig
from wscontext.graph import db, parse
from wscontext.graph.build import refresh
from wscontext.graph.languages.base import Import, RepoFiles
from wscontext.graph.languages.rust import PART
from wscontext.store.paths import StorePaths

SAMPLES = Path(__file__).resolve().parents[2] / "fixtures" / "graph" / "rust"
PACKAGE = sorted(p.relative_to(SAMPLES).as_posix() for p in SAMPLES.rglob("*")
                 if p.is_file() and p.name != "expected.json")


def sample(name: str):
    return parse.facts(PART, (SAMPLES / name).read_bytes(), name)


def source(text: str):
    return parse.facts(PART, text.encode(), "src/lib.rs")


def test_definitions_kinds_qualnames_and_parents():
    defs = {(d.qualname, d.start): d for d in sample("src/lib.rs").definitions}

    assert [(q, d.kind) for q, d in sorted(((q, d) for (q, _), d in defs.items()),
                                           key=lambda x: x[1].start)] == [
        ("Foo", "type"), ("make", "function"), ("Foo.new", "method"), ("Foo.fmt", "method"),
        ("Foo.fmt", "method"), ("Area", "type"), ("Area.area", "method"),
        ("Area.double", "method"), ("Bits", "type"), ("Pair", "type"), ("square", "function"),
        ("total", "function"), ("total.add", "function"), ("geometry.Line", "type"),
        ("geometry.Line.length", "method"), ("geometry.nested.deep", "function"),
        ("tests.fixture", "function"), ("tests.makes_a_foo", "function"),
        ("tests.runs_async", "function")]
    by_name = {q: d for (q, _), d in defs.items()}
    # `impl<T> Foo<T>`, `impl Display for &Foo<T>` and `impl Debug for Foo<u8>` all give `Foo`
    assert {d.parent for (q, _), d in defs.items() if q.startswith("Foo.")} == {"Foo"}
    assert by_name["Area.area"].parent == "Area"
    assert by_name["total.add"].parent == "total"
    # an inline module is a prefix, not a definition; an impl inside it is `x.Foo`
    assert by_name["geometry.Line"].parent is None
    assert by_name["geometry.nested.deep"].parent is None
    assert by_name["geometry.Line.length"].parent == "geometry.Line"
    # consts, statics, associated types and consts, fields and variants are not definitions
    shapes = [d.qualname for d in sample("src/shapes.rs").definitions]
    assert shapes == ["Circle", "Circle.area", "Kind"]
    assert not {"ZERO", "NAME", "Area.Unit", "Area.SIDES", "geometry", "tests"} & set(by_name)


def test_a_definition_starts_at_its_attributes_and_outer_doc_comments():
    defs = {d.qualname: d for d in sample("src/lib.rs").definitions}

    # `///` and `#[derive]`; a `/** */` doc comment
    assert (defs["Foo"].start, defs["Foo"].end) == (15, 19)
    assert (defs["Bits"].start, defs["Bits"].end) == (60, 64)
    # a plain comment before `#[inline]` ends the run
    assert (defs["make"].start, defs["make"].end) == (22, 29)
    assert defs["total"].start == 77
    assert defs["tests.makes_a_foo"].start == 118
    # an inner doc comment `//!` is not part of the next item
    facts = source("//! crate docs\nfn f() {}\n")
    assert facts.definitions[0].start == 2


def test_signatures_run_to_the_body_or_drop_the_semicolon():
    defs = {d.qualname: d for d in sample("src/lib.rs").definitions}

    assert defs["make"].signature == "pub fn make<T: Clone>(size: T) -> Foo<T> where T: Copy,"
    assert defs["Foo"].signature == "pub struct Foo<T>"
    assert defs["Area.area"].signature == "fn area(&self) -> f64"
    assert defs["Area"].signature == "pub trait Area"
    assert defs["Pair"].signature == "pub type Pair<T> = (T, T)"
    assert defs["square"].signature == "macro_rules! square"
    assert defs["tests.runs_async"].signature == "async fn runs_async()"
    long = source("fn f(" + ", ".join(f"a{i}: u8" for i in range(60)) + ") {}\n")
    assert len(long.definitions[0].signature) == 200


def test_test_marks_follow_test_attributes_and_cfg_test_modules():
    defs = {d.qualname: d.test for d in sample("src/lib.rs").definitions}

    assert {q for q, t in defs.items() if t} == {
        "tests.fixture", "tests.makes_a_foo", "tests.runs_async"}
    assert [d.test for d in sample("tests/integration.rs").definitions] == [True]

    facts = source(
        "#[tokio::test]\nasync fn a() {}\n"
        "#[test_case(1)]\nfn b() {}\n"
        "#[cfg(test)]\nfn c() {}\n"
        "#[cfg(not(test))]\nmod d { fn d() {} }\n"
        "#[cfg(test)]\nmod e { struct S; mod deeper { impl S { fn m() {} } } }\n"
        "#[allow(unused)]\n#[cfg(test)]\nmod f { fn f() {} }\n"
        "#[test]\nfn outer() { fn inner() {} }\n")
    assert {d.qualname: d.test for d in facts.definitions} == {
        "a": True, "b": False, "c": False, "d.d": False, "e.S": True, "e.deeper.S.m": True,
        "f.f": True, "outer": True, "outer.inner": False}


def test_impl_type_names_and_items_without_a_definition():
    facts = source(
        "impl<T> Tr for Vec<T> { fn a() {} }\n"
        "impl std::fmt::Debug for a::b::Bar { fn b() {} }\n"
        "impl Tr for (u8, u16) { fn c() {} }\n"
        "impl [u8; 4] { fn d() {} }\n"
        "impl dyn Shape { fn e() {} }\n"
        "mod x { mod y { impl Foo { fn f() {} } } }\n"
        "trait T { type V = u8; fn g(); }\n"
        "extern \"C\" { fn ext(); }\n")
    assert [(d.qualname, d.kind, d.parent) for d in facts.definitions] == [
        ("Vec.a", "method", "Vec"), ("Bar.b", "method", "Bar"),
        ("(u8,u16).c", "method", "(u8,u16)"), ("[u8;4].d", "method", "[u8;4]"),
        ("Shape.e", "method", "Shape"), ("x.y.Foo.f", "method", "x.y.Foo"),
        ("T", "type", None), ("T.g", "method", "T")]


def test_imports_one_spec_per_path():
    imports = [(i.spec, i.names, i.line) for i in sample("src/lib.rs").imports]

    assert imports == [
        ("mod shapes", (), 3), ("mod util", (), 4), ("mod renamed", (), 6),
        ("crate::shapes::Circle", (), 8), ("crate::shapes::circle", (), 8),
        ("crate::shapes::circle::area", (), 8), ("crate::shapes::*", (), 8),
        ("self::util::helper", (), 9), ("std::fmt", (), 10), ("std::fmt::Debug", (), 10),
        ("std::fmt::Display", (), 10), ("serde::Serialize", (), 11),
        ("sibling_crate::Thing", (), 12), ("alloc", (), 13),
        # inside `mod geometry`: `self::`/`super::` are relative to the file's module
        ("mod geometry::inner", (), 88), ("self::Foo", (), 90),
        ("self::geometry::inner::Point", (), 91), ("super::outside", (), 92),
        # inside `mod geometry::nested`
        ("self::make", (), 103), ("self::geometry::Line", (), 104),
        # `use super::*` inside `mod tests`
        ("self::*", (), 112)]


def test_grouped_aliased_and_self_leaves_split_into_paths():
    specs = [i.spec for i in source(
        "use a::{b, c::{d, e as f}, *};\n"
        "use a::{self, g};\n"
        "use self::h as i;\n"
        "use super::super::j;\n"
        "mod x { mod y { mod z; use super::super::super::k; use self::l::{self, m}; } }\n"
    ).imports]
    assert specs == [
        "a::b", "a::c::d", "a::c::e", "a::*", "a", "a::g", "self::h", "super::super::j",
        "mod x::y::z", "super::k", "self::x::y::l", "self::x::y::l::m"]


def _resolve_package(files: RepoFiles) -> dict[str, list[tuple[str, list[str]]]]:
    resolved = {}
    for rel in PACKAGE:
        if rel.endswith(".rs"):
            resolved[rel] = [(i.spec, PART.resolve_import(i, rel, files))
                             for i in sample(rel).imports]
    return resolved


def test_every_import_in_the_package_resolves_by_rusts_module_rules():
    assert "Cargo.toml" in PACKAGE
    lib = "src/lib.rs"
    assert _resolve_package(RepoFiles(PACKAGE)) == {
        "benches/speed.rs": [("fixture::total", [])],
        "broken.rs": [],
        "src/geometry/inner.rs": [
            # `geometry` is inline in the crate root
            ("super::Line", [lib])],
        "src/lib.rs": [
            ("mod shapes", ["src/shapes.rs"]), ("mod util", ["src/util/mod.rs"]),
            # `#[path]` is not followed
            ("mod renamed", []),
            ("crate::shapes::Circle", ["src/shapes.rs"]),
            ("crate::shapes::circle", ["src/shapes/circle.rs"]),
            ("crate::shapes::circle::area", ["src/shapes/circle.rs"]),
            ("crate::shapes::*", ["src/shapes.rs"]),
            ("self::util::helper", ["src/util/mod.rs"]),
            # the standard library, a registry crate, a sibling crate, `extern crate`
            ("std::fmt", []), ("std::fmt::Debug", []), ("std::fmt::Display", []),
            ("serde::Serialize", []), ("sibling_crate::Thing", []), ("alloc", []),
            ("mod geometry::inner", ["src/geometry/inner.rs"]),
            ("self::Foo", [lib]),
            ("self::geometry::inner::Point", ["src/geometry/inner.rs"]),
            # `super` past the crate root
            ("super::outside", []),
            ("self::make", [lib]), ("self::geometry::Line", [lib]), ("self::*", [lib])],
        "src/main.rs": [
            ("mod cli", []),
            # no module file: the crate root is the file itself
            ("crate::cli::run", ["src/main.rs"]),
            # the library is another crate
            ("fixture::make", [])],
        "src/shapes.rs": [
            # a non-mod-rs file keeps its children in its own folder
            ("mod circle", ["src/shapes/circle.rs"]),
            ("crate::util::helper", ["src/util/mod.rs"]),
            # an item of the crate root
            ("super::Foo", [lib])],
        "src/shapes/circle.rs": [
            ("super::Circle", ["src/shapes.rs"]), ("super::super::util", ["src/util/mod.rs"]),
            ("self::detail::*", ["src/shapes/circle.rs"])],
        "src/util/mod.rs": [("super::shapes::Circle", ["src/shapes.rs"])],
        # a module under `tests/` has no known crate root
        "tests/common/mod.rs": [("crate::helpers", [])],
        "tests/integration.rs": [
            ("mod common", ["tests/common/mod.rs"]),
            ("crate::common::setup", ["tests/common/mod.rs"]),
            ("fixture::Foo", [])],
    }


def resolve(spec: str, from_path: str, files: list[str]) -> list[str]:
    return PART.resolve_import(Import(spec, (), 1), from_path, RepoFiles(files))


def test_the_nearest_cargo_toml_is_the_package():
    files = ["Cargo.toml", "src/lib.rs", "src/util.rs", "crates/x/Cargo.toml",
             "crates/x/src/lib.rs", "crates/x/src/a.rs"]
    assert resolve("crate::util", "crates/x/src/a.rs", files) == ["crates/x/src/lib.rs"]
    assert resolve("crate::a", "crates/x/src/lib.rs", files) == ["crates/x/src/a.rs"]
    assert resolve("crate::util", "src/lib.rs", files) == ["src/util.rs"]
    # no package: `mod` still resolves, paths do not
    assert resolve("mod b", "a/a.rs", ["a/a.rs", "a/a/b.rs"]) == ["a/a/b.rs"]
    assert resolve("mod b", "a/mod.rs", ["a/mod.rs", "a/b/mod.rs"]) == ["a/b/mod.rs"]
    assert resolve("crate::b", "a/a.rs", ["a/a.rs", "a/b.rs"]) == []


def test_binaries_examples_and_benches_are_crate_roots_with_their_own_base():
    files = ["Cargo.toml", "src/lib.rs", "src/main.rs", "src/bin/tool.rs", "src/bin/sub/mod.rs",
             "src/bin/sub/x.rs", "examples/demo.rs", "examples/part.rs", "benches/b.rs",
             "benches/util/mod.rs"]
    assert resolve("mod sub", "src/bin/tool.rs", files) == ["src/bin/sub/mod.rs"]
    assert resolve("crate::sub::x", "src/bin/tool.rs", files) == ["src/bin/sub/x.rs"]
    assert resolve("crate::thing", "src/bin/tool.rs", files) == ["src/bin/tool.rs"]
    assert resolve("super::x::y", "src/bin/sub/x.rs", files) == ["src/bin/sub/x.rs"]
    # a module of a binary has no known crate root, and the library is not it
    assert resolve("crate::thing", "src/bin/sub/x.rs", files) == []
    assert resolve("crate::part", "examples/demo.rs", files) == ["examples/part.rs"]
    assert resolve("mod util", "benches/b.rs", files) == ["benches/util/mod.rs"]
    assert resolve("super::x", "benches/util/mod.rs", files) == []
    # `lib.rs` is the crate root before `main.rs`
    assert resolve("crate::x", "src/a.rs", files + ["src/a.rs"]) == ["src/lib.rs"]
    assert resolve("crate::x", "src/a.rs", ["Cargo.toml", "src/main.rs", "src/a.rs"]) == [
        "src/main.rs"]
    # `foo.rs` before `foo/mod.rs`; a file outside the crate's folders resolves nothing
    assert resolve("mod m", "src/lib.rs", ["Cargo.toml", "src/lib.rs", "src/m.rs",
                                           "src/m/mod.rs"]) == ["src/m.rs"]
    assert resolve("crate::x", "build.rs", files + ["build.rs"]) == []


def test_calls_by_name_inside_macros_and_closures():
    calls = [(c.name, c.line, c.caller) for c in sample("src/lib.rs").calls]

    # a closure's call belongs to the enclosing function; `Foo::new()` calls `new`
    assert ("new", 27, "make") in calls
    assert ("build", 28, "make") in calls
    # `x.f()`, `write!` and a call inside a macro argument
    assert ("write", 39, "Foo.fmt") in calls
    assert ("write_str", 45, "Foo.fmt") in calls and ("helper", 45, "Foo.fmt") in calls
    # a call in a `macro_rules!` body belongs to the macro; its use calls the macro
    assert ("helper", 73, "square") in calls
    assert ("square", 80, "total.add") in calls
    # `f::<T>()` and `vec![...]`
    assert ("parse", 82, "total") in calls
    assert [c for c in calls if c[1] == 83] == [
        ("vec", 83, "total"), ("make", 83, "total"), ("new", 83, "total")]
    # `assert_eq!(fixture().size, total(&[1]))`, nested `assert!(check(make(2)))`
    assert [c for c in calls if c[1] in (120, 125)] == [
        ("assert_eq", 120, "tests.makes_a_foo"), ("fixture", 120, "tests.makes_a_foo"),
        ("total", 120, "tests.makes_a_foo"), ("assert", 125, "tests.runs_async"),
        ("check", 125, "tests.runs_async"), ("make", 125, "tests.runs_async")]
    # attributes are not code: `#[cfg(any(unix, windows))]` calls nothing
    assert not {"any", "cfg", "derive", "inline"} & {c[0] for c in calls}


def test_call_shapes_outside_macros():
    facts = source(
        "static S: u8 = top();\n"
        "fn f() {\n"
        "    a::b::c();\n"
        "    x.y.z::<u8>();\n"
        "    std::println!(\"{}\", inner!(g()));\n"
        "    (h)();\n"
        "}\n")
    assert [(c.name, c.line, c.caller) for c in facts.calls] == [
        ("top", 1, None), ("c", 3, "f"), ("z", 4, "f"), ("println", 5, "f"),
        ("inner", 5, "f"), ("g", 5, "f")]


@pytest.mark.parametrize("path,expected", [
    ("tests/a.rs", True), ("tests/common/mod.rs", True), ("crates/x/tests/common/mod.rs", True),
    ("src/tests/a.rs", False), ("src/foo/tests.rs", False), ("crates/x/src/tests/a.rs", False),
    ("benches/a.rs", False), ("examples/a.rs", False), ("src/lib.rs", False),
    ("tests.rs", False)])
def test_is_test(path, expected):
    assert PART.is_test(path) is expected


def test_the_package_builds_with_test_marks_and_resolved_imports(git_repo):
    store = StorePaths.resolve(git_repo)
    for rel in PACKAGE:
        if rel != "broken.rs":
            (git_repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SAMPLES / rel, git_repo / rel)

    result = refresh(store, Config(graph=GraphConfig(languages=("rust",))))

    assert result.complete and result.parse_errors == ()
    conn = db.connect(store, readonly=True)
    try:
        assert conn.execute("SELECT qualname FROM symbols WHERE test=1 ORDER BY start_line, "
                            "path").fetchall() == [
            ("integration",), ("tests.fixture",), ("tests.makes_a_foo",), ("tests.runs_async",)]
        assert conn.execute("SELECT DISTINCT target FROM imports WHERE path='src/lib.rs' AND "
                            "spec LIKE 'mod %' ORDER BY target").fetchall() == [
            (None,), ("src/geometry/inner.rs",), ("src/shapes.rs",), ("src/util/mod.rs",)]
        # both `Foo.fmt` methods are stored, each linked to its parent type
        assert conn.execute(
            "SELECT s.start_line, p.qualname FROM symbols s JOIN symbols p ON p.id = s.parent_id "
            "WHERE s.qualname='Foo.fmt' ORDER BY s.start_line").fetchall() == [
            (38, "Foo"), (44, "Foo")]
    finally:
        conn.close()


def test_a_syntax_error_is_recorded_as_error_and_keeps_the_facts_around_it(git_repo):
    store = StorePaths.resolve(git_repo)
    (git_repo / "broken.rs").write_bytes((SAMPLES / "broken.rs").read_bytes())
    (git_repo / "good.rs").write_text("fn fine() {}\n")

    result = refresh(store, Config(graph=GraphConfig(languages=("rust",))))

    assert result.complete and result.parse_errors == ("broken.rs",)
    conn = db.connect(store, readonly=True)
    try:
        assert conn.execute("SELECT path, status, error FROM files ORDER BY path").fetchall() == [
            ("broken.rs", "error", "syntax error at line 5"), ("good.rs", "ok", None)]
        assert conn.execute("SELECT qualname FROM symbols WHERE path='broken.rs' "
                            "ORDER BY start_line").fetchall() == [("good",), ("bad",), ("after",)]
    finally:
        conn.close()
