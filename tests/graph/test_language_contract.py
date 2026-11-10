"""The contract every registered language part keeps. A new language is picked up with no edit."""

import ast
import json
from dataclasses import asdict
from pathlib import Path

import pytest

import wscontext.graph
from wscontext.graph import parse
from wscontext.graph.languages import REGISTRY, load_parts
from wscontext.graph.languages.base import LanguagePart

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "graph"
GRAPH = Path(wscontext.graph.__file__).resolve().parent
LANGUAGES = sorted(REGISTRY)


def _part(name: str) -> LanguagePart:
    parts, unavailable = load_parts([name])
    assert unavailable == [], f"{name} did not load"
    return parts[0]


def _samples(part: LanguagePart) -> list[Path]:
    directory = FIXTURES / part.name
    return sorted(p for p in directory.rglob("*") if p.suffix.lower() in part.extensions)


@pytest.mark.parametrize("name", LANGUAGES)
def test_the_part_loads_and_describes_itself(name):
    part = _part(name)
    assert isinstance(part, LanguagePart)
    assert part.name == name
    assert isinstance(part.version, int) and part.version >= 1
    assert part.extensions and all(e.startswith(".") and e == e.lower() for e in part.extensions)


@pytest.mark.parametrize("name", LANGUAGES)
def test_the_fixture_directory_holds_samples_and_their_expected_facts(name):
    part = _part(name)
    assert (FIXTURES / name / "expected.json").is_file()
    assert _samples(part)


@pytest.mark.parametrize("name", LANGUAGES)
def test_extract_over_the_samples_equals_expected_json(name):
    part = _part(name)
    directory = FIXTURES / name
    expected = json.loads((directory / "expected.json").read_text(encoding="utf-8"))
    got = {}
    for sample in _samples(part):
        rel = sample.relative_to(directory).as_posix()
        got[rel] = json.loads(json.dumps(asdict(parse.facts(part, sample.read_bytes(), rel))))
    assert sorted(got) == sorted(expected)
    for rel in got:
        assert got[rel] == expected[rel], rel


def test_extensions_do_not_overlap_between_languages():
    owner: dict[str, str] = {}
    for name in LANGUAGES:
        for ext in _part(name).extensions:
            assert ext not in owner, f"{ext} claimed by {owner.get(ext)} and {name}"
            owner[ext] = name


def _imports(tree: ast.AST, package: str, in_function: bool = False):
    """(absolute module or module.name, inside a function) for every import in `tree`."""
    for node in ast.iter_child_nodes(tree):
        inner = in_function or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, in_function
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[:len(package.split(".")) - (node.level - 1)]
                base = ".".join(anchor + ([node.module] if node.module else []))
            yield base, in_function
            for a in node.names:
                yield f"{base}.{a.name}", in_function
        yield from _imports(node, package, inner)


def _module(path: Path) -> list[tuple[str, bool]]:
    package = "wscontext." + ".".join(path.parent.relative_to(GRAPH.parent).parts)
    return list(_imports(ast.parse(path.read_text(encoding="utf-8")), package))


def test_core_modules_import_no_language_module():
    prefix = "wscontext.graph.languages."
    language_modules = {p.stem for p in (GRAPH / "languages").glob("*.py")} - {"__init__", "base"}
    found = {}
    for path in sorted(GRAPH.glob("*.py")):
        bad = [m for m, _ in _module(path)
               if m.startswith(prefix) and m[len(prefix):].split(".")[0] in language_modules]
        if bad:
            found[path.name] = bad
    assert not found, found


def test_the_parser_is_imported_only_inside_functions_of_parse_and_language_modules():
    may_import = {GRAPH / "parse.py"} | {
        p for p in (GRAPH / "languages").glob("*.py") if p.name not in ("__init__.py", "base.py")}
    found = {}
    for path in sorted(GRAPH.rglob("*.py")):
        for module, in_function in _module(path):
            if module.split(".")[0].startswith("tree_sitter") and (
                    not in_function or path not in may_import):
                found.setdefault(str(path.relative_to(GRAPH)), []).append(module)
    assert not found, found


def test_the_import_scan_sees_through_from_and_relative_imports():
    tree = ast.parse("from wscontext.graph.languages import python\n"
                     "from .languages import base\n"
                     "def f():\n    import tree_sitter\n")
    assert list(_imports(tree, "wscontext.graph")) == [
        ("wscontext.graph.languages", False), ("wscontext.graph.languages.python", False),
        ("wscontext.graph.languages", False), ("wscontext.graph.languages.base", False),
        ("tree_sitter", True)]
