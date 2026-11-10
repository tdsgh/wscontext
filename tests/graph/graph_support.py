"""Small repositories for the graph tests, and a second language part for tests that need two."""

import sys
import types
from pathlib import Path

import pytest

from wscontext.capture import worker as capture_worker
from wscontext.config import Config, GraphConfig
from wscontext.graph import db
from wscontext.graph.languages import REGISTRY
from wscontext.graph.languages.base import LanguagePart
from wscontext.graph.languages.python import PART as PYTHON
from wscontext.store.paths import StorePaths


@pytest.fixture
def store(git_repo: Path, monkeypatch) -> StorePaths:
    """The repository's store. A refresh that would start the graph worker records the start on
    `capture_worker.spawn_worker.calls` instead; `.real` is the original."""
    calls: list[tuple[list[str], Path, str]] = []

    def fake(args, cwd, module="wscontext.capture.worker"):
        calls.append((list(args), cwd, module))

    fake.calls = calls
    fake.real = capture_worker.spawn_worker
    monkeypatch.setattr(capture_worker, "spawn_worker", fake)
    return StorePaths.resolve(git_repo)


def write(store: StorePaths, rel: str, text: str) -> Path:
    path = store.root.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def config(*languages: str, exclude: tuple[str, ...] = ()) -> Config:
    return Config(graph=GraphConfig(languages=languages or ("python",), exclude=exclude))


def table(store: StorePaths, sql: str, params: tuple = ()) -> list[tuple]:
    conn = db.connect(store, readonly=True)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def snapshot(store: StorePaths) -> dict[str, list[tuple]]:
    """Every fact row, and each file row without its mtime."""
    return {
        "files": table(store, "SELECT path, language, lang_version, size, digest, status, error "
                              "FROM files ORDER BY path"),
        "symbols": table(store, "SELECT * FROM symbols ORDER BY id"),
        "imports": table(store, "SELECT * FROM imports ORDER BY path, line, spec, target"),
        "calls": table(store, "SELECT * FROM calls ORDER BY path, line, name"),
    }


class ToyPart(LanguagePart):
    """`.toy` files, read with the Python grammar and extraction."""

    name = "toy"
    extensions = (".toy",)

    def __init__(self):
        self.version = 1

    def load(self):
        return PYTHON.load()

    def extract(self, tree, source):
        return PYTHON.extract(tree, source)

    def resolve_import(self, imp, from_path, files):
        return []

    def is_test(self, path):
        return False


def register(monkeypatch, name: str, part: LanguagePart | None) -> None:
    """Put a module exposing `part` in the registry under `name`; None registers a module path
    that does not import."""
    module_name = f"graph_test_language_{name}"
    if part is not None:
        module = types.ModuleType(module_name)
        module.PART = part
        monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setitem(REGISTRY, name, module_name)


@pytest.fixture
def toy(monkeypatch) -> ToyPart:
    part = ToyPart()
    register(monkeypatch, "toy", part)
    return part


@pytest.fixture
def unknown_language(monkeypatch) -> None:
    """A registered language no core test enables or knows by name. Core test modules import it
    under `pytestmark` so a hard-coded registry list fails now, not when the next language lands."""
    register(monkeypatch, "unknown_dummy", None)
