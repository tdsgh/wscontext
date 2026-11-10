"""TypeScript: JavaScript's constructs plus interfaces, type aliases and enums (kind `type`).
`.tsx` files use the TSX grammar. The extraction is shared with JavaScript."""

import functools
from typing import Any

from wscontext.graph.languages import _ecmascript
from wscontext.graph.languages.base import (FileFacts, Import, LanguagePart, LanguageUnavailable,
                                            RepoFiles)


@functools.cache
def _language(tsx: bool) -> Any:
    try:
        import tree_sitter
        import tree_sitter_typescript
        grammar = (tree_sitter_typescript.language_tsx() if tsx
                   else tree_sitter_typescript.language_typescript())
        return tree_sitter.Language(grammar)
    except Exception as e:
        raise LanguageUnavailable(f"typescript grammar: {e}") from e


class TypeScriptPart(LanguagePart):
    name = "typescript"
    version = 1
    extensions = (".ts", ".mts", ".cts", ".tsx")

    def load(self) -> Any:
        return _language(False)

    def grammar(self, path: str) -> Any:
        return _language(path.lower().endswith(".tsx"))

    def extract(self, tree: Any, source: bytes) -> FileFacts:
        return _ecmascript.extract(tree, source)

    def resolve_import(self, imp: Import, from_path: str, files: RepoFiles) -> list[str]:
        return _ecmascript.resolve_import(imp, from_path, files)

    def is_test(self, path: str) -> bool:
        return _ecmascript.is_test(path)


PART = TypeScriptPart()
