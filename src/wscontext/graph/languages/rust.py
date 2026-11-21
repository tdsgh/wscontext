"""Rust: functions, methods, types and macros; `mod` and `use`; calls by name, inside macros too."""

import functools
from typing import Any

from wscontext.graph.languages.base import (Call, Definition, FileFacts, Import, LanguagePart,
                                            LanguageUnavailable, RepoFiles)

SIGNATURE_CHARS = 200

_DEFINITIONS = {"function_item": "function", "macro_definition": "function",
                "function_signature_item": "method", "struct_item": "type", "enum_item": "type",
                "union_item": "type", "trait_item": "type", "type_item": "type"}
# scope frames that name a qualname prefix but are not definitions
_CONTAINERS = ("mod", "impl")
# folders under a package whose `*.rs` files are each a crate root
_TARGET_DIRS = ("tests", "benches", "examples")


@functools.cache
def _language() -> Any:
    try:
        import tree_sitter
        import tree_sitter_rust
        return tree_sitter.Language(tree_sitter_rust.language())
    except Exception as e:
        raise LanguageUnavailable(f"rust grammar: {e}") from e


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _end_line(node) -> int:
    # a node ending at column 0 ends on the line before
    end = node.end_point
    return end.row if end.column == 0 and end.row > node.start_point.row else end.row + 1


def _attributes(node) -> tuple[Any, list]:
    """(first node of the item, its attributes): the run of attributes and outer doc comments
    directly before `node`. A plain comment ends the run."""
    first, attrs = node, []
    sib = node.prev_sibling
    while sib is not None:
        if sib.type == "attribute_item":
            attrs.append(sib)
        elif not (sib.type in ("line_comment", "block_comment")
                  and sib.child_by_field_name("outer") is not None):
            break
        first, sib = sib, sib.prev_sibling
    return first, attrs


def _is_test_attribute(attr) -> bool:
    """`#[test]`, `#[tokio::test]`: an attribute whose path ends in `test`."""
    inner = next((c for c in attr.named_children if c.type == "attribute"), None)
    path = inner.named_children[0] if inner is not None and inner.named_children else None
    if path is not None and path.type == "scoped_identifier":
        path = path.child_by_field_name("name")
    return path is not None and path.type == "identifier" and _text(path) == "test"


def _is_cfg_test(attr) -> bool:
    return "".join(_text(attr).split()) == "#[cfg(test)]"


def _signature(node, source: bytes) -> str:
    """The header up to the body, or the whole item without its `;`, on one line."""
    body = node.child_by_field_name("body")
    if body is None and node.type == "macro_definition":
        # `macro_rules! name { ... }` has no body field; its rules start at the first bracket
        body = next((c for c in node.children if c.type in ("{", "(", "[")), None)
    end = body.start_byte if body is not None else node.end_byte
    header = source[node.start_byte:end].decode("utf-8", "replace").rstrip()
    if body is None and header.endswith(";"):
        header = header[:-1]
    return " ".join(header.split())[:SIGNATURE_CHARS]


def _impl_name(node) -> str:
    """The impl's type: the last identifier of its base type, without references and type
    arguments; the type's text without whitespace when there is none."""
    t = node.child_by_field_name("type")
    if t is None:
        return ""
    base = t
    while base.type in ("reference_type", "generic_type", "pointer_type", "dynamic_type"):
        inner = base.child_by_field_name("trait" if base.type == "dynamic_type" else "type")
        if inner is None:
            break
        base = inner
    if base.type == "scoped_type_identifier":
        base = base.child_by_field_name("name") or base
    if base.type in ("type_identifier", "primitive_type"):
        return _text(base)
    return "".join(_text(t).split())


def _use_paths(node, prefix: str) -> list[str]:
    """Every path a `use` argument names: `a::{b, c::*}` gives `a::b`, `a::c::*`."""
    def join(a: str, b: str) -> str:
        return f"{a}::{b}" if a and b else a or b

    t = node.type
    if t == "use_as_clause":
        path = node.child_by_field_name("path")
        return _use_paths(path, prefix) if path is not None else []
    if t == "use_list":
        return [p for c in node.named_children for p in _use_paths(c, prefix)]
    if t == "scoped_use_list":
        path = node.child_by_field_name("path")
        lst = node.child_by_field_name("list")
        base = join(prefix, "".join(_text(path).split())) if path is not None else prefix
        return _use_paths(lst, base) if lst is not None else []
    if t == "use_wildcard":
        path = "".join(_text(node).split())
        return [join(prefix, path)]
    if t == "self" and prefix:
        # `a::{self}` names `a`
        return [prefix]
    if t in ("identifier", "scoped_identifier", "crate", "self", "super"):
        return [join(prefix, "".join(_text(node).split()))]
    return []


def _relative_to_file(spec: str, mods: tuple[str, ...]) -> str:
    """Rewrite a leading `self::`/`super::` inside inline modules `mods` relative to the file's
    own module: inside `mod x`, `self::a` is `self::x::a` and `super::a` is `self::a`."""
    segs = spec.split("::")
    if not mods or segs[0] not in ("self", "super"):
        return spec
    path = list(mods)
    i = 1 if segs[0] == "self" else 0
    climbed = 0
    while i < len(segs) and segs[i] == "super":
        if path:
            path.pop()
        else:
            climbed += 1
        i += 1
    rest = segs[i:]
    if climbed:
        return "::".join(["super"] * climbed + rest)
    return "::".join(["self", *path, *rest])


class _Extractor:
    def __init__(self, source: bytes):
        self.source = source
        self.definitions: list[Definition] = []
        self.imports: list[Import] = []
        self.calls: list[Call] = []

    def visit(self, node, scope: tuple[tuple[str, str], ...], test: bool) -> None:
        """Walk `node` with `scope`, the (qualname prefix, frame kind) of each enclosing module,
        impl or definition, and `test`, whether it sits inside a `#[cfg(test)]` module."""
        t = node.type
        if t in ("attribute_item", "inner_attribute_item"):
            return
        if t in _DEFINITIONS:
            self.define(node, scope, test)
            return
        if t == "mod_item":
            self.module(node, scope, test)
            return
        if t == "impl_item":
            self.impl(node, scope, test)
            return
        if t == "use_declaration":
            arg = node.child_by_field_name("argument")
            mods = tuple(q.rsplit(".", 1)[-1] for q, k in scope if k == "mod")
            for path in _use_paths(arg, "") if arg is not None else []:
                self.imports.append(Import(spec=_relative_to_file(path, mods), names=(),
                                           line=node.start_point.row + 1))
            return
        if t == "extern_crate_declaration":
            name = node.child_by_field_name("name")
            if name is not None:
                self.imports.append(Import(spec=_text(name), names=(),
                                           line=node.start_point.row + 1))
            return
        if t == "call_expression":
            self.call(node.child_by_field_name("function"), scope)
        elif t == "macro_invocation":
            macro = node.child_by_field_name("macro")
            if macro is not None and macro.type == "scoped_identifier":
                macro = macro.child_by_field_name("name")
            if macro is not None and macro.type == "identifier":
                self.add_call(macro, scope)
        elif t == "token_tree":
            self.token_calls(node, scope)
        for c in node.children:
            self.visit(c, scope, test)

    def define(self, node, scope, test: bool) -> None:
        name_node = node.child_by_field_name("name")
        frame = scope[-1][1] if scope else None
        # a signature outside a trait (an `extern` block) and an associated type are not
        # definitions
        if (name_node is None or node.type == "function_signature_item" and frame != "trait"
                or node.type == "type_item" and frame in ("impl", "trait")):
            for c in node.children:
                self.visit(c, scope, test)
            return
        name = _text(name_node)
        kind = _DEFINITIONS[node.type]
        if kind == "function" and frame in ("impl", "trait"):
            kind = "method"
        prefix = scope[-1][0] if scope else None
        parent = prefix if frame not in (None, "mod") else None
        qualname = f"{prefix}.{name}" if prefix else name
        first, attrs = _attributes(node)
        mark = test or (node.type == "function_item" and any(map(_is_test_attribute, attrs)))
        self.definitions.append(Definition(
            name=name, qualname=qualname, kind=kind, start=first.start_point.row + 1,
            end=_end_line(node), signature=_signature(node, self.source), parent=parent,
            test=mark))
        if node.type == "macro_definition":
            # no body field: the rules, and the calls in them, belong to the macro
            for c in node.children:
                self.visit(c, (*scope, (qualname, kind)), test)
            return
        body = node.child_by_field_name("body")
        for c in node.children:
            if c != body:
                self.visit(c, scope, test)
        if body is not None:
            frame_kind = "trait" if node.type == "trait_item" else kind
            self.visit(body, (*scope, (qualname, frame_kind)), test)

    def module(self, node, scope, test: bool) -> None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None:
            return
        name = _text(name_node)
        mods = [q.rsplit(".", 1)[-1] for q, k in scope if k == "mod"]
        if body is None:
            self.imports.append(Import(spec="mod " + "::".join([*mods, name]), names=(),
                                       line=node.start_point.row + 1))
            return
        _, attrs = _attributes(node)
        prefix = scope[-1][0] if scope else None
        qualname = f"{prefix}.{name}" if prefix else name
        self.visit(body, (*scope, (qualname, "mod")), test or any(map(_is_cfg_test, attrs)))

    def impl(self, node, scope, test: bool) -> None:
        body = node.child_by_field_name("body")
        for c in node.children:
            if c != body:
                self.visit(c, scope, test)
        if body is not None:
            prefix = scope[-1][0] if scope else None
            name = _impl_name(node)
            self.visit(body, (*scope, (f"{prefix}.{name}" if prefix else name, "impl")), test)

    def add_call(self, name_node, scope) -> None:
        caller = next((q for q, k in reversed(scope) if k not in _CONTAINERS), None)
        self.calls.append(Call(name=_text(name_node), line=name_node.start_point.row + 1,
                               caller=caller))

    def call(self, fn, scope) -> None:
        while fn is not None and fn.type == "generic_function":
            fn = fn.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "identifier":
            self.add_call(fn, scope)
        elif fn.type == "scoped_identifier":
            name = fn.child_by_field_name("name")
            if name is not None:
                self.add_call(name, scope)
        elif fn.type == "field_expression":
            field = fn.child_by_field_name("field")
            if field is not None:
                self.add_call(field, scope)

    def token_calls(self, tree, scope) -> None:
        """In a macro's token tree: `name(` is a call, `name!(` a macro call. Nested token trees
        are visited by the walk."""
        children = tree.children
        for i, c in enumerate(children[:-1]):
            if c.type != "identifier":
                continue
            after = children[i + 1]
            if after.type == "token_tree" and after.children and after.children[0].type == "(":
                self.add_call(c, scope)
            elif (after.type == "!" and i + 2 < len(children)
                  and children[i + 2].type == "token_tree"):
                self.add_call(c, scope)


class RustPart(LanguagePart):
    name = "rust"
    version = 1
    extensions = (".rs",)

    def load(self) -> Any:
        return _language()

    def extract(self, tree: Any, source: bytes) -> FileFacts:
        x = _Extractor(source)
        x.visit(tree.root_node, (), False)
        return FileFacts(definitions=tuple(x.definitions), imports=tuple(x.imports),
                         calls=tuple(x.calls))

    def resolve_import(self, imp: Import, from_path: str, files: RepoFiles) -> list[str]:
        folders = from_path.split("/")[:-1]
        package = next((folders[:i] for i in range(len(folders), -1, -1)
                        if "/".join([*folders[:i], "Cargo.toml"]) in files), None)
        # the file's path inside its package, as folders and a file name
        rel = from_path.split("/")[len(package):] if package is not None else None
        root = rel is not None and (
            rel in (["src", "lib.rs"], ["src", "main.rs"])
            or len(rel) == 3 and rel[:2] == ["src", "bin"]
            or len(rel) == 2 and rel[0] in _TARGET_DIRS)

        def first_file(base: list[str], name: str) -> str | None:
            return next((p for p in ("/".join([*base, f"{name}.rs"]),
                                     "/".join([*base, name, "mod.rs"])) if p in files), None)

        if imp.spec.startswith("mod "):
            *within, name = imp.spec[4:].split("::")
            # a crate root or a `mod.rs` keeps its children in its own folder, `a/b.rs` in `a/b/`
            if root or from_path.rsplit("/", 1)[-1] == "mod.rs":
                child = folders
            else:
                child = from_path.removesuffix(".rs").split("/")
            found = first_file([*child, *within], name)
            return [found] if found else []

        if rel is None or len(rel) < 2:
            return []
        # the module base folder: the folder of the file's crate root
        if rel[0] == "src" and rel[1] != "bin" or rel[0] in _TARGET_DIRS:
            top = rel[:1]
        elif rel[:2] == ["src", "bin"] and len(rel) > 2:
            top = rel[:2]
        else:
            return []
        base = [*package, *top]
        if root:
            module: list[str] = []
        else:
            module = rel[len(top):]
            module[-1] = module[-1].removesuffix(".rs")
            if module[-1] == "mod":
                module.pop()

        segs = imp.spec.split("::")
        if segs[-1] == "*":
            segs.pop()
        if not segs:
            return []
        if segs[0] == "crate":
            path, rest = [], segs[1:]
        elif segs[0] in ("self", "super"):
            path = list(module)
            i = 1 if segs[0] == "self" else 0
            while i < len(segs) and segs[i] == "super":
                if not path:
                    return []
                path.pop()
                i += 1
            rest = segs[i:]
        else:
            return []
        mods = path + rest
        for j in range(len(mods), 0, -1):
            found = first_file([*base, *mods[:j - 1]], mods[j - 1])
            if found:
                return [found]
        if root:
            return [from_path]
        # the crate of a module under `src/bin`, `tests`, `benches` or `examples` is not known
        if top == ["src"]:
            found = next((p for p in ("/".join([*package, "src", "lib.rs"]),
                                      "/".join([*package, "src", "main.rs"])) if p in files),
                         None)
            return [found] if found else []
        return []

    def is_test(self, path: str) -> bool:
        for folder in path.split("/")[:-1]:
            if folder == "src":
                return False
            if folder == "tests":
                return True
        return False


PART = RustPart()
