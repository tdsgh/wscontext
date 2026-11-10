"""JavaScript: functions, classes and methods; `import`, `export … from`, `require()` and
`import()`; calls and `new`. The extraction is shared with TypeScript."""

import functools
from typing import Any

from wscontext.graph.languages import _ecmascript
from wscontext.graph.languages.base import (FileFacts, Import, LanguagePart, LanguageUnavailable,
                                            RepoFiles)


@functools.cache
def _language() -> Any:
    try:
        import tree_sitter
        import tree_sitter_javascript
        return tree_sitter.Language(tree_sitter_javascript.language())
    except Exception as e:
        raise LanguageUnavailable(f"javascript grammar: {e}") from e


class JavaScriptPart(LanguagePart):
    name = "javascript"
    version = 1
    extensions = (".js", ".jsx", ".mjs", ".cjs")

    def load(self) -> Any:
        return _language()

    def extract(self, tree: Any, source: bytes) -> FileFacts:
        return _ecmascript.extract(tree, source)

    def resolve_import(self, imp: Import, from_path: str, files: RepoFiles) -> list[str]:
        return _ecmascript.resolve_import(imp, from_path, files)

    def is_test(self, path: str) -> bool:
        return _ecmascript.is_test(path)


PART = JavaScriptPart()
