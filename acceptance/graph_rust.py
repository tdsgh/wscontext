"""Rust-in-the-graph acceptance run: docs/graph-rust-phase-1.md §4 to §9 against the installed plugin.

    .venv/bin/python acceptance/graph_rust.py [--only NAME ...] [--list] [--keep]

Not part of the offline suite (pytest never collects `acceptance/`). No scenario calls `claude -p`, so
the run spends no API usage. Each scenario builds a Rust package in a throwaway Git repository and
drives the real `ctx` commands, real Git and the real hook entrypoint with hand-written hook inputs,
as stage 3 does. Every repository lives under one temporary directory; `ctx init` and `ctx rebuild`
only ever target paths inside it.

It reuses the plugin lookup and the throwaway-repository helpers of `stage1.py`, `stage2.py` and
`stage3.py`, which sit beside it, and runs `ctx` through the installed plugin's own launcher. It does
not run those scripts' scenarios; the stage gate runs them separately, unchanged.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from record_corpus import isolation
from stage1 import COMMIT_SCRIPT, PLUGIN_ID, Acc, Fail, check, init_repo, locate_plugin, wait_for
from stage2 import doctor_item, set_config
from stage3 import commit_all, definitions, results, session_commit, where, write

MAX_FILE_BYTES = 1_048_576  # the graph's size cap for one file (src/wscontext/graph/scope.py)

CARGO = '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'

LIB_RS = """\
mod kinds;
mod shapes;
mod util;

pub mod geometry {
    pub fn span() -> i32 {
        3
    }
}

pub struct Foo;

impl Foo {
    pub fn new() -> Self {
        Foo
    }
}

pub struct Bar;

impl Bar {
    pub fn new() -> Self {
        Bar
    }
}

/// Doubles the target.
pub fn f() -> i32 {
    target_fn() * 2
}

pub fn g() -> i32 {
    2
}

pub fn target_fn() -> i32 {
    1
}

macro_rules! wrap {
    ($e:expr) => {
        target_fn() + $e
    };
}

pub fn via_assert() {
    assert_eq!(target_fn(), 1);
}

pub fn via_closure() -> i32 {
    let c = || target_fn();
    c()
}

pub fn via_vec() -> Vec<i32> {
    vec![target_fn(), 2]
}

pub fn via_macro() -> i32 {
    wrap!(1)
}

pub fn via_turbofish() -> i32 {
    pick::<u8>()
}

pub fn pick<T>() -> i32 {
    0
}

pub fn via_new() -> (Foo, Bar) {
    (Foo::new(), Bar::new())
}

pub fn standalone_user() -> i32 {
    f() + g()
}

#[test]
fn standalone() {
    assert_eq!(target_fn(), 1);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checks() {
        assert_eq!(target_fn(), 1);
    }

    fn helper_in_tests() -> i32 {
        target_fn()
    }
}
"""

KINDS_RS = """\
use std::fmt;

/// A point.
/// Second line.
#[derive(Clone)]
pub struct Point<T> {
    x: T,
}

pub enum Mode { A, B }

pub union Bits { a: u32, b: f32 }

pub type Id = u64;

pub trait Shape {
    fn area(&self) -> f64;
    fn name(&self) -> String { String::new() }
}

impl fmt::Display for Point<u8> {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result { Ok(()) }
}

impl fmt::Debug for Point<u8> {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result { Ok(()) }
}

pub fn describe<T>(t: T) -> String
where
    T: fmt::Debug,
{
    format!("{:?}", t)
}

macro_rules! square {
    ($x:expr) => { $x * $x };
}

pub mod nested {
    pub fn deep() {}
}
"""

RUST_FILES = {
    "Cargo.toml": CARGO,
    "src/lib.rs": LIB_RS,
    "src/kinds.rs": KINDS_RS,
    "src/main.rs": "fn main() {\n    println!(\"{}\", demo::f());\n}\n",
    "src/shapes.rs": ("mod circle;\n\nuse crate::util::tidy;\nuse serde::Serialize;\n\n"
                      "pub fn area() -> i32 {\n    tidy()\n}\n"),
    "src/shapes/circle.rs": "use super::area;\n\npub fn round() -> i32 {\n    area()\n}\n",
    "src/util/mod.rs": "mod deep;\n\nuse self::deep::dig;\n\npub fn tidy() -> i32 {\n    dig()\n}\n",
    "src/util/deep.rs": "pub fn dig() -> i32 {\n    1\n}\n",
    "tests/integration.rs": ("mod common;\n\nuse demo::target_fn;\n\n#[test]\nfn it_works() {\n"
                             "    assert_eq!(target_fn(), common::one());\n}\n"),
    "tests/common/mod.rs": "pub fn one() -> i32 {\n    1\n}\n",
    "benches/speed.rs": "use demo::target_fn;\n\nfn bench_speed() -> i32 {\n    target_fn()\n}\n",
    "py/mod.py": "def scale(n):\n    return n\n\n\ndef only_py(x):\n    return scale(x)\n",
    ".gitignore": "target/\n",
}


# --- helpers -----------------------------------------------------------------------------------

def rust_repo(acc: Acc, name: str, languages: str | None = None) -> Path:
    """The Rust package above, committed and set up; `languages` (a TOML literal) replaces the default list."""
    repo = acc.repo(name)
    for rel, text in RUST_FILES.items():
        write(repo, rel, text)
    commit_all(acc, repo, "start")
    init_repo(acc, repo)
    if languages:
        set_config(repo, "graph", "languages", languages)
    commit_all(acc, repo, "set up wscontext")  # so a later session commit holds only its own change
    return repo


def impact(acc: Acc, repo: Path, target: str, code: int | None = 0) -> dict:
    """`ctx impact` with every result: the default limit would cut a long list of callers."""
    return acc.ctx_json(repo, "--full", "impact", target, code=code)


# --- scenarios ---------------------------------------------------------------------------------

def rust_definitions(acc: Acc) -> str:
    repo = rust_repo(acc, "defs")
    outline = results(acc.ctx_json(repo, "code", "src/kinds.rs"), "definition")
    got = [(d["kind"], d["qualname"]) for d in outline]
    want = [("type", "Point"), ("type", "Mode"), ("type", "Bits"), ("type", "Id"), ("type", "Shape"),
            ("method", "Shape.area"), ("method", "Shape.name"), ("method", "Point.fmt"), ("method", "Point.fmt"),
            ("function", "describe"), ("function", "square"), ("function", "nested.deep")]
    check(got == want, f"outline kinds and qualnames: {got}")
    by = {(d["qualname"], d["start"]): d for d in outline}
    point = outline[0]
    check((point["start"], point["end"]) == (3, 8), f"Point must start on its doc comment (line 3) and end at line 8: {point['start']}-{point['end']}")
    check([d["start"] for d in outline if d["qualname"] == "Point.fmt"] == [22, 26], "the two Point.fmt methods are not at lines 22 and 26")
    check(all(d["parent"] == "Point" for d in outline if d["qualname"] == "Point.fmt"), "Point.fmt does not name Point as its parent")
    check(by[("Shape.area", 17)]["signature"] == "fn area(&self) -> f64" and by[("Shape.name", 18)]["parent"] == "Shape",
          "a trait method with no body or with one is wrong")
    describe = next(d for d in outline if d["qualname"] == "describe")
    check(describe["start"] == 29 and "where T: fmt::Debug" in describe["signature"], f"describe: {describe['start']} {describe['signature']!r}")
    check(next(d for d in outline if d["qualname"] == "square")["signature"] == "macro_rules! square", "macro_rules! signature")
    check(next(d for d in outline if d["qualname"] == "nested.deep")["parent"] is None, "an item in an inline module has a parent")
    # the same qualname twice: one target, the head block lists both definitions (D7)
    both = impact(acc, repo, "src/kinds.rs#Point.fmt")
    check(len(results(both, "target")) == 1 and "22" in str(results(both, "target")[0]["body"]) and "26" in str(results(both, "target")[0]["body"]),
          f"impact on a qualname defined twice: {results(both, 'target')}")
    check(definitions(acc, repo, "src/main.rs") and not [d for d in definitions(acc, repo, "square") if d["path"] != "src/kinds.rs"],
          "a macro or main is not found by name")
    return "kinds and qualnames, Point.fmt twice, start on the doc comment, a signature with where, a trait with and without bodies, macro_rules!, an inline module"


def rust_doc_change(acc: Acc) -> str:
    repo = rust_repo(acc, "doc")
    sid = "doc-" + uuid.uuid4().hex[:8]
    text = (repo / "src/lib.rs").read_text()
    check("/// Doubles the target.\n" in text, "the fixture lost its doc comment")
    sha = session_commit(acc, repo, sid, "src/lib.rs", text.replace("/// Doubles the target.", "/// Doubles the target, twice over."),
                         "reword the doc comment")
    diff = acc.git(repo, "show", "--format=", "-U0", sha)
    check(diff.count("\n+") == 2 and "pub fn f" not in diff, f"the commit changes more than the doc comment: {diff}")
    why = acc.ctx_json(repo, "why", "src/lib.rs#f")
    check(any(sid in str(r) for r in why["results"]), f"the session that rewrote the doc comment is not linked to f: {why['results']}")
    other = acc.ctx_json(repo, "why", "src/lib.rs#g")
    check(not any(sid in str(r) for r in other["results"]), "the session is linked to g, whose lines it did not touch")
    return f"session {sid[:8]} changed only the doc comment above f and is linked to f, not to g"


def rust_imports(acc: Acc) -> str:
    repo = rust_repo(acc, "imports")

    def importers(target: str) -> set[str]:
        doc = impact(acc, repo, target)
        return {r["path"] for r in results(doc) if r["type"] in ("importer", "test") and r.get("kind") == "importer"}

    check(importers("src/shapes.rs") == {"src/lib.rs", "src/shapes/circle.rs"}, f"importers of shapes.rs: {importers('src/shapes.rs')}")
    check(importers("src/util/mod.rs") == {"src/lib.rs", "src/shapes.rs"}, f"importers of util/mod.rs: {importers('src/util/mod.rs')}")
    check(importers("src/util/deep.rs") == {"src/util/mod.rs"}, f"importers of util/deep.rs: {importers('src/util/deep.rs')}")
    check(importers("tests/common/mod.rs") == {"tests/integration.rs"}, f"importers of tests/common/mod.rs: {importers('tests/common/mod.rs')}")
    check(importers("src/kinds.rs") == {"src/lib.rs"}, f"importers of kinds.rs: {importers('src/kinds.rs')}")
    for spec, line in (("mod circle", "src/shapes.rs"), ("crate::util::tidy", "src/shapes.rs"), ("serde::Serialize", "src/shapes.rs")):
        check(any(i["spec"] == spec for i in results(acc.ctx_json(repo, "code", line), "file")[0]["imports"]), f"{line} records no import {spec!r}")
    imports = {i["spec"]: i["target"] for i in results(acc.ctx_json(repo, "code", "src/shapes.rs"), "file")[0]["imports"]}
    check(imports["mod circle"] == "src/shapes/circle.rs" and imports["crate::util::tidy"] == "src/util/mod.rs",
          f"resolution from shapes.rs: {imports}")
    check(imports["serde::Serialize"] is None, f"`use serde::...` resolved to {imports['serde::Serialize']}")
    super_imports = {i["spec"]: i["target"] for i in results(acc.ctx_json(repo, "code", "src/shapes/circle.rs"), "file")[0]["imports"]}
    check(super_imports == {"super::area": "src/shapes.rs"}, f"use super:: from circle.rs: {super_imports}")
    self_imports = {i["spec"]: i["target"] for i in results(acc.ctx_json(repo, "code", "src/util/mod.rs"), "file")[0]["imports"]}
    check(self_imports.get("self::deep::dig") == "src/util/deep.rs" and self_imports.get("mod deep") == "src/util/deep.rs",
          f"mod and use self:: in util/mod.rs: {self_imports}")
    check(all(t is None for s, t in {i["spec"]: i["target"] for i in results(acc.ctx_json(repo, "code", "tests/integration.rs"), "file")[0]["imports"]}.items()
              if s.startswith("demo::")), "`use demo::...` (the package's own library by name) resolved to a file")
    return "mod, use crate::, super:: and self:: resolve to the files they name; `use serde::` and `use demo::` stay unresolved"


def rust_calls(acc: Acc) -> str:
    repo = rust_repo(acc, "calls")
    doc = impact(acc, repo, "target_fn")
    found = {r["qualname"] for r in results(doc, "caller")}
    check({"f", "via_assert", "via_closure", "via_vec", "wrap"} <= found,
          f"callers of target_fn (assert_eq!, a closure, vec![], a macro body): {sorted(found)}")
    check("via_macro" not in found, "via_macro calls wrap!, not target_fn, but is listed as a caller of target_fn")
    wrapper = impact(acc, repo, "src/lib.rs#wrap")
    check("via_macro" in {r["qualname"] for r in results(wrapper, "caller")}, f"a macro invocation is not a call: {[r['qualname'] for r in results(wrapper, 'caller')]}")
    turbofish = impact(acc, repo, "pick")
    check("via_turbofish" in {r["qualname"] for r in results(turbofish, "caller")}, "pick::<u8>() is not a call to pick")
    for target, caller in (("src/lib.rs#Foo.new", "via_new"), ("src/lib.rs#Bar.new", "via_new")):
        doc = impact(acc, repo, target)
        mine = [r for r in results(doc, "caller") if r["qualname"] == caller]
        check(mine and mine[0]["shared"] == 2 and "2 definitions share this name" in mine[0]["body"][0],
              f"the shared-name count for `new` on {target}: {[(r['qualname'], r.get('shared')) for r in results(doc, 'caller')]}")
    check(all(r["match"] == "name" for r in results(impact(acc, repo, "target_fn"), "caller")), "a Rust caller is not marked as a name match")
    return "callers found inside assert_eq!, a closure, vec![], a macro body, a macro invocation and a turbofish; `new` reports 2 definitions sharing the name"


def rust_tests(acc: Acc) -> str:
    repo = rust_repo(acc, "tests")
    doc = impact(acc, repo, "target_fn")
    tests = {(r["path"], r["qualname"]) for r in results(doc, "test") if r.get("kind") == "caller"}
    want_tests = {("tests/integration.rs", "it_works"), ("src/lib.rs", "standalone"), ("src/lib.rs", "tests.checks"),
                  ("src/lib.rs", "tests.helper_in_tests")}
    check(tests == want_tests, f"tests calling target_fn on a name target: {sorted(tests)}")
    callers = {(r["path"], r["qualname"]) for r in results(doc, "caller")}
    check(("benches/speed.rs", "bench_speed") in callers, f"benches/ is not listed under callers: {sorted(callers)}")
    check({"f", "via_assert", "via_closure"} <= {q for _, q in callers} and not callers & want_tests,
          f"callers: {sorted(callers)}")
    check(doc["counts"]["tests"] >= 4, f"counts: {doc['counts']}")
    # on a file target, in-file calls are skipped, except from a test
    on_file = impact(acc, repo, "src/lib.rs")
    file_tests = {(r["path"], r["qualname"]) for r in results(on_file, "test") if r.get("kind") == "caller"}
    check({("src/lib.rs", "standalone"), ("src/lib.rs", "tests.checks"), ("src/lib.rs", "tests.helper_in_tests"),
           ("tests/integration.rs", "it_works")} <= file_tests, f"tests on a file target: {sorted(file_tests)}")
    file_callers = {(r["path"], r["qualname"]) for r in results(on_file, "caller")}
    check(not [c for c in file_callers if c[0] == "src/lib.rs"], f"a call from inside the same file is listed as a caller on a file target: {sorted(file_callers)}")
    check(("benches/speed.rs", "bench_speed") in file_callers, f"benches/ on a file target: {sorted(file_callers)}")
    return "tests/, #[test] and #[cfg(test)] callers listed as tests (name and file targets); benches/ and the rest stay callers"


def rust_switch(acc: Acc) -> str:
    repo = rust_repo(acc, "switch")
    config = (repo / ".wscontext" / "config.toml").read_text()
    check('languages = ["python", "typescript", "javascript", "rust"]' in config, f"ctx init did not write rust into the language list: {config}")
    check(doctor_item(acc, repo, "language rust")["detail"] == "on", f"doctor: {doctor_item(acc, repo, 'language rust')}")
    acc.ctx(repo, "code", "target_fn")  # `ctx stats code` never refreshes: build the graph first
    stats = acc.ctx_json(repo, "stats", "code")["results"][0]
    check("rust" in stats["languages"]["enabled"] and "rust" in stats["languages"]["supported"] and stats["graph"]["by_language"]["rust"]["files"] >= 9,
          f"stats code: {stats['languages']} {stats['graph']['by_language']}")
    check("rust" in "\n".join(stats["body"]), "the text report of ctx stats code does not list rust")

    def python_answers() -> dict:
        commands = (("code", "py/mod.py"), ("impact", "py/mod.py#only_py"), ("code", "scale"), ("impact", "py/mod.py"))
        return {" ".join(c): (acc.ctx(repo, *c).stdout, json.dumps(acc.ctx_json(repo, *c), sort_keys=True)) for c in commands}

    before = python_answers()
    set_config(repo, "graph", "languages", '["python", "typescript", "javascript"]')
    gone = acc.ctx_json(repo, "code", "src/lib.rs")
    check(not gone["results"] and "not in the graph" in gone["note"], f"Rust answers after switching it off: {gone}")
    check(not definitions(acc, repo, "target_fn"), "Rust definitions remain after switching rust off")
    check(python_answers() == before, "Python answers changed when rust was switched off")
    off = doctor_item(acc, repo, "language rust")
    check(off["detail"] == "off" and 'add "rust" to [graph] languages in .wscontext/config.toml' in off.get("fix", ""),
          f"doctor for rust off lacks the fix: {off}")
    after = acc.ctx_json(repo, "stats", "code")["results"][0]
    check("rust" not in after["languages"]["enabled"] and "rust" not in after["graph"]["by_language"], f"stats with rust off: {after['languages']}")
    set_config(repo, "graph", "languages", '["python", "typescript", "javascript", "rust"]')
    check(len(definitions(acc, repo, "target_fn")) == 1, "Rust did not return when switched on again")
    check(python_answers() == before, "Python answers changed when rust was switched on again")
    return "init writes rust; doctor and stats show it; with rust off the Python answers are byte-identical, doctor names the fix; on again restores Rust"


def rust_relink(acc: Acc) -> str:
    repo = rust_repo(acc, "relink", languages='["python", "typescript", "javascript"]')
    sid = "relink-" + uuid.uuid4().hex[:8]
    text = (repo / "src/lib.rs").read_text()
    session_commit(acc, repo, sid, "src/lib.rs", text.replace("    target_fn() * 2\n", "    target_fn() * 20\n"), "raise f")
    acc.ctx(repo, "code", "py/mod.py")  # the graph reads the commit with rust off
    off = impact(acc, repo, "src/lib.rs#f", code=None)
    check(sid not in str(off), f"the session is linked to f while rust is off: {str(off)[:300]}")
    set_config(repo, "graph", "languages", '["python", "typescript", "javascript", "rust"]')
    acc.ctx(repo, "code", "target_fn")  # starts the refresh that re-links past commits

    def linked(target: str) -> bool:
        return any(sid in str(r) for r in results(impact(acc, repo, target, code=None), "session"))

    wait_for(lambda: linked("src/lib.rs#f"), "the past commit to be linked to f after rust was switched on", 120)
    check(not linked("src/lib.rs#g"), "the session is linked to g, which it did not change")
    why = acc.ctx_json(repo, "why", "src/lib.rs#f")
    check(any(sid in str(r) for r in why["results"]), f"ctx why src/lib.rs#f does not show the session: {why['results']}")
    return f"session {sid[:8]}, made while rust was off, is linked to f and not to g once rust is added"


def rust_limits(acc: Acc) -> str:
    repo = acc.repo("limits")
    write(repo, "Cargo.toml", CARGO)
    write(repo, "src/lib.rs", "pub fn small_zq() -> i32 {\n    1\n}\n")
    write(repo, "src/big.rs", "pub fn big_zq() -> i32 {\n    1\n}\n// " + "x" * MAX_FILE_BYTES + "\n")
    write(repo, "vendor/dep/lib.rs", "pub fn vendored_zq() -> i32 {\n    2\n}\n")
    write(repo, "target/debug/build.rs", "pub fn built_zq() -> i32 {\n    3\n}\n")
    write(repo, ".gitignore", "target/\n")
    commit_all(acc, repo, "start")
    init_repo(acc, repo)
    set_config(repo, "graph", "exclude", '["vendor"]')
    check(len(definitions(acc, repo, "small_zq")) == 1, "the ordinary Rust file has no definitions")
    check((repo / "src/big.rs").stat().st_size > MAX_FILE_BYTES, "the large fixture is under the cap")
    big = results(acc.ctx_json(repo, "code", "src/big.rs"), "file")
    check(big and big[0]["status"] == "skipped" and "bytes" in str(big[0]["error"]), f"a file over the size cap: {big}")
    check(not definitions(acc, repo, "big_zq"), "a definition of a file over the cap was found")
    check(not definitions(acc, repo, "vendored_zq"), "a definition in an excluded path was found")
    check("not in the graph" in acc.ctx(repo, "code", "vendor/dep/lib.rs").stdout, "an excluded file is not reported as outside the graph")
    check(not definitions(acc, repo, "built_zq"), "a definition in a path Git ignores was found")
    status = acc.ctx_json(repo, "stats", "code")["results"][0]["graph"]
    check(status["by_status"]["skipped"] == 1 and status["by_language"]["rust"]["files"] == 2, f"stats: {status}")
    return "a .rs file over 1 MiB is recorded as skipped with no definitions; an excluded path and a Git-ignored path are absent"


@dataclass
class Scenario:
    name: str
    run: Callable[[Acc], str]
    what: str


SCENARIOS = [
    Scenario("rust_definitions", rust_definitions, "kinds and qualnames, Foo.fmt twice, start on the doc comment, a where signature (§4)"),
    Scenario("rust_doc_change", rust_doc_change, "a session that changes only a doc comment is linked to the function (§4)"),
    Scenario("rust_imports", rust_imports, "mod and use crate::/super::/self:: resolve; use serde:: stays unresolved (§5)"),
    Scenario("rust_calls", rust_calls, "callers inside assert_eq!, a closure, a macro invocation; shared-name count (§6)"),
    Scenario("rust_tests", rust_tests, "tests/, #[test], #[cfg(test)] under tests; benches/ under callers (§7)"),
    Scenario("rust_switch", rust_switch, "init writes rust; doctor and stats show it; off leaves Python byte-identical (§8, §9)"),
    Scenario("rust_relink", rust_relink, "a session made while rust is off is linked once rust is added (§9)"),
    Scenario("rust_limits", rust_limits, "a file over the size cap is skipped; an excluded path is absent (§9)"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", action="append", metavar="NAME", help="run only this scenario (repeatable)")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    parser.add_argument("--plugin-root", help="plugin root to run ctx from (default: detected)")
    parser.add_argument("--plugin-data", help="plugin data directory (default: ~/.claude/plugins/data/wscontext-wscontext)")
    args = parser.parse_args(argv)
    known = {s.name for s in SCENARIOS}
    unknown = set(args.only or []) - known
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(sorted(unknown))}; --list shows them")
    chosen = [s for s in SCENARIOS if not args.only or s.name in args.only]
    if args.list:
        for s in SCENARIOS:
            print(f"{s.name:18} offline  {s.what}")
        return 0

    plugin = locate_plugin(args.plugin_root, args.plugin_data)
    print(f"plugin root: {plugin.root}\nplugin version: {plugin.version}\nplugin data: {plugin.data}")
    for tool in ("git", "uv"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is not on PATH")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}
    tmp = Path(tempfile.mkdtemp(prefix="wscontext-acceptance-rust-")).resolve()
    print(f"working directory: {tmp}\n")
    acc = Acc(tmp, plugin, None, 0.0, env, extra=isolation(PLUGIN_ID))
    Path(acc.commit).write_text(COMMIT_SCRIPT)
    Path(acc.commit).chmod(0o755)
    outcomes: list[tuple[str, str]] = []
    try:
        for s in chosen:
            started = time.monotonic()
            try:
                status, observed = "PASS", s.run(acc)
            except Fail as e:
                status, observed = "FAIL", str(e)
            except Exception as e:  # a broken scenario is a failure, not a crash
                status, observed = "FAIL", f"{type(e).__name__}: {e}"
            outcomes.append((s.name, status))
            print(f"{status:6} {s.name}  {observed}  ({time.monotonic() - started:.0f}s)", flush=True)
        acc.plugin.reconcile()
        print(f"\nran against plugin root {acc.plugin.root}, version {acc.plugin.version}")
    finally:
        if args.keep:
            print(f"\nkept {tmp}")
        else:
            time.sleep(1)  # let detached workers finish writing
            shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, st in outcomes if st == "FAIL"]
    print(f"\n{len(outcomes) - len(failed)} of {len(outcomes)} scenarios passed; failed: {', '.join(failed) or 'none'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
