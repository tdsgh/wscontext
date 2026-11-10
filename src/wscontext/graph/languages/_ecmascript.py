"""What TypeScript and JavaScript share: fact extraction, import resolution and test paths.

The grammars differ in node names for a few constructs (`public_field_definition` against
`field_definition`, TypeScript-only declarations); the extractor accepts both sets.
"""

import posixpath
import re

from wscontext.graph.languages.base import Call, Definition, FileFacts, Import, RepoFiles

SIGNATURE_CHARS = 200
# probing order for an import without an extension: TypeScript first, as a TypeScript file may
# import a JavaScript one and the reverse is rare
SOURCE_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
# `./x.js` in a TypeScript file names `x.ts`
_TS_FOR_JS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}

_CLASSES = ("class_declaration", "abstract_class_declaration")
_TYPES = ("interface_declaration", "type_alias_declaration", "enum_declaration")
_FUNCTION_VALUES = ("arrow_function", "function_expression", "function", "generator_function")
_FIELDS = ("public_field_definition", "field_definition")
_TEST_FILE = re.compile(r"\.(test|spec)\.")


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _string(node) -> str | None:
    """The contents of a plain string literal, else None."""
    if node is None or node.type != "string":
        return None
    return _text(node)[1:-1] or None


def _end_line(node) -> int:
    end = node.end_point
    return end.row if end.column == 0 and end.row > node.start_point.row else end.row + 1


def _squash(text: str) -> str:
    return " ".join(text.split()).replace("( ", "(").replace(" )", ")")


def _signature(node, source: bytes, end_node, prefix: str = "") -> str:
    """The header from the keyword (after any decorators) to the body, on one line."""
    start = next((c for c in node.children if c.type != "decorator"), node).start_byte
    end = end_node.start_byte if end_node is not None else node.end_byte
    header = _squash(source[start:end].decode("utf-8", "replace")).removesuffix("=>").rstrip()
    return (prefix + header).removesuffix(";").rstrip()[:SIGNATURE_CHARS]


def _decorator_start(node) -> int:
    """The line a class member starts on: its decorators are siblings that precede it."""
    start = node
    prev = node.prev_sibling
    while prev is not None and prev.type == "decorator":
        start = prev
        prev = prev.prev_sibling
    return start.start_point.row + 1


class _Extractor:
    def __init__(self, source: bytes):
        self.source = source
        self.definitions: list[Definition] = []
        self.imports: list[Import] = []
        self.calls: list[Call] = []

    def visit(self, node, scope: tuple[tuple[str, str], ...]) -> None:
        """Walk `node` with `scope`, the (qualname, kind) of each enclosing definition."""
        t = node.type
        if t in ("function_declaration", "generator_function_declaration") + _CLASSES + _TYPES:
            self.declaration(node, scope)
            return
        if t == "method_definition":
            self.method(node, scope)
            return
        if t in _FIELDS:
            self.field(node, scope)
            return
        if t == "variable_declarator":
            self.variable(node, scope)
            return
        if t == "import_statement":
            self.import_statement(node)
            return
        if t == "export_statement" and node.child_by_field_name("source") is not None:
            self.export_from(node)
            return
        if t == "call_expression":
            self.call(node, scope)
        elif t == "new_expression":
            self.named_call(node.child_by_field_name("constructor"), scope)
        for c in node.children:
            self.visit(c, scope)

    def define(self, node, outer, name_node, kind, scope, body, end_node, prefix="") -> None:
        name = _text(name_node)
        parent = scope[-1] if scope else None
        qualname = f"{parent[0]}.{name}" if parent else name
        if node.type == "method_definition" or node.type in _FIELDS:
            start = _decorator_start(outer)
        else:
            start = outer.start_point.row + 1
        self.definitions.append(Definition(
            name=name, qualname=qualname, kind=kind, start=start, end=_end_line(outer),
            signature=_signature(node, self.source, end_node, prefix),
            parent=parent[0] if parent else None))
        # parameters, decorators, base classes and defaults belong to the enclosing scope
        for c in node.children:
            if c != body and c != name_node:
                self.visit(c, scope)
        if body is not None:
            self.visit(body, (*scope, (qualname, kind)))

    def declaration(self, node, scope) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        outer = node.parent if node.parent.type == "export_statement" else node
        if name_node is None:
            for c in node.children:
                self.visit(c, scope)
            return
        if node.type in _CLASSES:
            kind = "class"
        elif node.type in _TYPES:
            kind = "type"
        else:
            kind = "function"
        # a type alias has no body: its whole text is the signature
        self.define(node, outer, name_node, kind, scope,
                    None if node.type == "type_alias_declaration" else body,
                    None if node.type == "type_alias_declaration" else body)

    def method(self, node, scope) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None or name_node.type == "computed_property_name":
            for c in node.children:
                self.visit(c, scope)
            return
        self.define(node, node, name_node, "method", scope, node.child_by_field_name("body"),
                    node.child_by_field_name("body"))

    def field(self, node, scope) -> None:
        name_node = node.child_by_field_name("name") or node.child_by_field_name("property")
        value = node.child_by_field_name("value")
        if (name_node is None or name_node.type == "computed_property_name"
                or value is None or value.type not in _FUNCTION_VALUES):
            for c in node.children:
                self.visit(c, scope)
            return
        # a class field holding a function is a method; the function's body is its scope
        body = value.child_by_field_name("body")
        self.define_value(node, node, name_node, "method", scope, value, body)

    def variable(self, node, scope) -> None:
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if (name_node is None or name_node.type != "identifier"
                or value is None or value.type not in _FUNCTION_VALUES):
            for c in node.children:
                self.visit(c, scope)
            return
        declaration = node.parent
        outer = declaration.parent if declaration.parent.type == "export_statement" else declaration
        if len([c for c in declaration.children if c.type == "variable_declarator"]) > 1:
            outer = node
        keyword = _text(declaration.children[0]) if declaration.type == "lexical_declaration" \
            else "var"
        self.define_value(node, outer, name_node, "function", scope, value,
                          value.child_by_field_name("body"), prefix=keyword + " ")

    def define_value(self, node, outer, name_node, kind, scope, value, body, prefix="") -> None:
        """A name bound to a function value: `const f = () => …`, `field = function () {…}`."""
        name = _text(name_node)
        parent = scope[-1] if scope else None
        qualname = f"{parent[0]}.{name}" if parent else name
        start = _decorator_start(outer) if node.type in _FIELDS else outer.start_point.row + 1
        self.definitions.append(Definition(
            name=name, qualname=qualname, kind=kind, start=start, end=_end_line(outer),
            signature=_signature(node, self.source, body, prefix),
            parent=parent[0] if parent else None))
        for c in node.children:
            if c != value and c != name_node:
                self.visit(c, scope)
        for c in value.children:
            if c != body:
                self.visit(c, scope)
        if body is not None:
            self.visit(body, (*scope, (qualname, kind)))

    def call(self, node, scope) -> None:
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn is None:
            return
        first = next((c for c in args.named_children if c.type != "comment"), None) \
            if args else None
        if fn.type == "import" or (fn.type == "identifier" and _text(fn) == "require"):
            spec = _string(first)
            if spec is not None:
                self.imports.append(Import(spec=spec, names=(), line=node.start_point.row + 1))
                return
        self.named_call(fn, scope)

    def named_call(self, fn, scope) -> None:
        if fn is None:
            return
        if fn.type == "identifier":
            name_node = fn
        elif fn.type == "member_expression":
            name_node = fn.child_by_field_name("property")
        else:
            return
        if name_node is not None:
            self.calls.append(Call(name=_text(name_node), line=name_node.start_point.row + 1,
                                   caller=scope[-1][0] if scope else None))

    def import_statement(self, node) -> None:
        line = node.start_point.row + 1
        source = node.child_by_field_name("source")
        if source is not None:
            spec = _string(source)
            if spec is not None:
                clause = next((c for c in node.children if c.type == "import_clause"), None)
                names = self.import_names(clause) if clause is not None else ()
                self.imports.append(Import(spec=spec, names=names, line=line))
            return
        # `import x = require("./x")`
        for clause in node.children:
            if clause.type == "import_require_clause":
                spec = _string(clause.child_by_field_name("source"))
                if spec is None:
                    spec = next((_string(c) for c in clause.children if c.type == "string"), None)
                if spec is not None:
                    self.imports.append(Import(spec=spec, names=(), line=line))

    def import_names(self, clause) -> tuple[str, ...]:
        names: list[str] = []
        for c in clause.children:
            if c.type == "identifier":
                names.append("default")
            elif c.type == "namespace_import":
                names.append("*")
            elif c.type == "named_imports":
                for s in c.children:
                    if s.type == "import_specifier":
                        n = s.child_by_field_name("name")
                        if n is not None:
                            names.append(_text(n))
        return tuple(names)

    def export_from(self, node) -> None:
        spec = _string(node.child_by_field_name("source"))
        if spec is None:
            return
        names: list[str] = []
        for c in node.children:
            if c.type == "namespace_export" or c.type == "*":
                names.append("*")
            elif c.type == "export_clause":
                for s in c.children:
                    if s.type == "export_specifier":
                        n = s.child_by_field_name("name")
                        if n is not None:
                            names.append(_text(n))
        self.imports.append(Import(spec=spec, names=tuple(names), line=node.start_point.row + 1))


def extract(tree, source: bytes) -> FileFacts:
    x = _Extractor(source)
    x.visit(tree.root_node, ())
    return FileFacts(definitions=tuple(x.definitions), imports=tuple(x.imports),
                     calls=tuple(x.calls))


def resolve_import(imp: Import, from_path: str, files: RepoFiles) -> list[str]:
    """Relative specifiers only: the file itself, then the extensions, then a directory `index`.
    Bare package names and path aliases are not resolved."""
    if not imp.spec.startswith("."):
        return []
    base = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), imp.spec))
    if base == ".." or base.startswith("../"):
        return []
    if base == ".":
        base = ""
    candidates = []
    stem, ext = posixpath.splitext(base)
    if ext in SOURCE_EXTENSIONS:
        candidates.append(base)
        candidates += [stem + e for e in _TS_FOR_JS.get(ext, ())]
    candidates += [base + e for e in SOURCE_EXTENSIONS]
    candidates += [posixpath.join(base, "index" + e) for e in SOURCE_EXTENSIONS]
    return next(([c] for c in candidates if c in files), [])


def is_test(path: str) -> bool:
    parts = path.split("/")
    return bool(_TEST_FILE.search(parts[-1])) or "__tests__" in parts[:-1]
