"""The contract between the graph core and a language part.

Core code speaks only to these types. A part never imports the core; it receives the parsed tree
and the repository's file list and returns plain facts.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


class LanguageUnavailable(Exception):
    """A part's grammar cannot be loaded."""


@dataclass(frozen=True)
class Definition:
    name: str
    # `Class.method`, `outer.inner`
    qualname: str
    # function, class, method or type
    kind: str
    # 1-based, inclusive; the start includes decorators
    start: int
    end: int
    signature: str
    # qualname of the enclosing definition
    parent: str | None


@dataclass(frozen=True)
class Import:
    # the module as written: `a.b`, `..pkg`, `./util`
    spec: str
    # the imported names, `*` for a wildcard; empty for a plain module import
    names: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class Call:
    # the callee's short name: `f` for `f()`, `obj.f()` and `f.g.f()`
    name: str
    line: int
    # qualname of the enclosing definition; None at file level
    caller: str | None


@dataclass(frozen=True)
class FileFacts:
    definitions: tuple[Definition, ...] = ()
    imports: tuple[Import, ...] = ()
    calls: tuple[Call, ...] = ()
    # set by the core when the tree has a syntax error; the facts found are kept
    error: str | None = None


class RepoFiles:
    """The Git-listed repository paths an import may resolve to, `/`-separated."""

    def __init__(self, paths: Iterable[str]):
        self._paths = frozenset(paths)
        self._by_name: dict[str, list[str]] = {}
        for p in self._paths:
            self._by_name.setdefault(p.rsplit("/", 1)[-1], []).append(p)

    def __contains__(self, path: object) -> bool:
        return path in self._paths

    def ending_with(self, suffix: str) -> list[str]:
        """Paths equal to `suffix` or ending in `/suffix`, fewest directories first."""
        found = [p for p in self._by_name.get(suffix.rsplit("/", 1)[-1], ())
                 if p == suffix or p.endswith("/" + suffix)]
        return sorted(found, key=lambda p: (p.count("/"), p))


class LanguagePart(ABC):
    """One language: its grammar, its fact extraction and its import resolution."""

    # the name used in `[graph] languages`
    name: str
    # raise it whenever extraction changes: that language's files reparse on the next refresh
    version: int
    # lower-case, with the dot
    extensions: tuple[str, ...]

    @abstractmethod
    def load(self) -> Any:
        """The grammar's `tree_sitter.Language`. Raises `LanguageUnavailable`."""

    def grammar(self, path: str) -> Any:
        """The grammar for one file; a part with several grammars picks by extension here."""
        return self.load()

    @abstractmethod
    def extract(self, tree: Any, source: bytes) -> FileFacts:
        """Definitions, imports and calls of one parsed file, in the order the tree holds them."""

    @abstractmethod
    def resolve_import(self, imp: Import, from_path: str, files: RepoFiles) -> list[str]:
        """The repository files `imp` in `from_path` refers to; empty when none is in the repo."""

    @abstractmethod
    def is_test(self, path: str) -> bool:
        """Whether a repository path is a test file by this language's conventions."""
