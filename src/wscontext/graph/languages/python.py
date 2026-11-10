"""Python: functions, classes and methods; `import` and `from … import`; calls by name."""

import functools
from typing import Any

from wscontext.graph.languages.base import (Call, Definition, FileFacts, Import, LanguagePart,
                                            LanguageUnavailable, RepoFiles)

SIGNATURE_CHARS = 200


@functools.cache
def _language() -> Any:
    try:
        import tree_sitter
        import tree_sitter_python
        return tree_sitter.Language(tree_sitter_python.language())
    except Exception as e:
        raise LanguageUnavailable(f"python grammar: {e}") from e


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _end_line(node) -> int:
    # a node ending at column 0 ends on the line before
    end = node.end_point
    return end.row if end.column == 0 and end.row > node.start_point.row else end.row + 1


def _signature(node, source: bytes) -> str:
    """The header from `def`/`class` to the colon before the body, on one line."""
    body = node.child_by_field_name("body")
    end = node.end_byte
    if body is not None:
        colons = [c for c in node.children if c.type == ":" and c.end_byte <= body.start_byte]
        end = colons[-1].start_byte if colons else body.start_byte
    header = " ".join(source[node.start_byte:end].decode("utf-8", "replace").split())
    return header.replace("( ", "(").replace(" )", ")")[:SIGNATURE_CHARS]


class _Extractor:
    def __init__(self, source: bytes):
        self.source = source
        self.definitions: list[Definition] = []
        self.imports: list[Import] = []
        self.calls: list[Call] = []

    def visit(self, node, scope: tuple[tuple[str, str], ...]) -> None:
        """Walk `node` with `scope`, the (qualname, kind) of each enclosing definition."""
        t = node.type
        if t == "decorated_definition":
            inner = node.child_by_field_name("definition")
            for c in node.children:
                if c != inner:
                    self.visit(c, scope)
            if inner is not None:
                self.define(inner, node, scope)
            return
        if t in ("function_definition", "class_definition"):
            self.define(node, node, scope)
            return
        if t == "import_statement":
            self.import_modules(node)
            return
        if t == "import_from_statement":
            self.import_from(node)
            return
        if t == "call":
            self.call(node, scope)
        for c in node.children:
            self.visit(c, scope)

    def define(self, node, outer, scope) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None:
            for c in node.children:
                self.visit(c, scope)
            return
        name = _text(name_node)
        parent = scope[-1] if scope else None
        if node.type == "class_definition":
            kind = "class"
        else:
            kind = "method" if parent and parent[1] == "class" else "function"
        qualname = f"{parent[0]}.{name}" if parent else name
        self.definitions.append(Definition(
            name=name, qualname=qualname, kind=kind, start=outer.start_point.row + 1,
            end=_end_line(node), signature=_signature(node, self.source),
            parent=parent[0] if parent else None))
        # parameters, defaults and base classes belong to the enclosing scope
        for c in node.children:
            if c != body:
                self.visit(c, scope)
        if body is not None:
            self.visit(body, (*scope, (qualname, kind)))

    def call(self, node, scope) -> None:
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "identifier":
            name_node = fn
        elif fn.type == "attribute":
            name_node = fn.child_by_field_name("attribute")
        else:
            return
        if name_node is not None:
            self.calls.append(Call(name=_text(name_node), line=name_node.start_point.row + 1,
                                   caller=scope[-1][0] if scope else None))

    def import_modules(self, node) -> None:
        for c in node.children_by_field_name("name"):
            module = c.child_by_field_name("name") if c.type == "aliased_import" else c
            if module is not None:
                self.imports.append(Import(spec=_text(module), names=(),
                                           line=node.start_point.row + 1))

    def import_from(self, node) -> None:
        module = node.child_by_field_name("module_name")
        if module is None:
            return
        names = []
        for c in node.children_by_field_name("name"):
            n = c.child_by_field_name("name") if c.type == "aliased_import" else c
            if n is not None:
                names.append(_text(n))
        if any(c.type == "wildcard_import" for c in node.children):
            names.append("*")
        self.imports.append(Import(spec="".join(_text(module).split()), names=tuple(names),
                                   line=node.start_point.row + 1))


class PythonPart(LanguagePart):
    name = "python"
    version = 1
    extensions = (".py",)

    def load(self) -> Any:
        return _language()

    def extract(self, tree: Any, source: bytes) -> FileFacts:
        x = _Extractor(source)
        x.visit(tree.root_node, ())
        return FileFacts(definitions=tuple(x.definitions), imports=tuple(x.imports),
                         calls=tuple(x.calls))

    def resolve_import(self, imp: Import, from_path: str, files: RepoFiles) -> list[str]:
        level = len(imp.spec) - len(imp.spec.lstrip("."))
        dotted = imp.spec[level:]
        if level:
            base = from_path.split("/")[:-1]
            if level - 1 > len(base):
                return []
            base = base[:len(base) - (level - 1)]
            module = "/".join(base + (dotted.split(".") if dotted else []))

            def locate(rel: str) -> list[str]:
                rel = rel.lstrip("/")
                return [p for p in (f"{rel}.py", f"{rel}/__init__.py") if p in files]
        else:
            module = dotted.replace(".", "/")

            def locate(rel: str) -> list[str]:
                # by path suffix, so `src/` layouts resolve; the shallowest root wins
                found = [(p[:-len(s)].count("/"), p) for s in (f"{rel}.py", f"{rel}/__init__.py")
                         for p in files.ending_with(s)]
                if not found:
                    return []
                depth = min(d for d, _ in found)
                return [p for d, p in found if d == depth]

        module_files = locate(module) if module else []
        targets: list[str] = []
        for name in imp.names:
            sub = locate(f"{module}/{name}") if name != "*" else []
            targets += sub or module_files
        if not imp.names:
            targets = module_files
        return sorted(set(targets))

    def is_test(self, path: str) -> bool:
        parts = path.split("/")
        name = parts[-1]
        return (name.startswith("test_") and name.endswith(".py") or name.endswith("_test.py")
                or any(p in ("tests", "test") for p in parts[:-1]))


PART = PythonPart()
