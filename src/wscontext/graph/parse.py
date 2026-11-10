"""The only place a syntax tree is made."""

from dataclasses import replace

from wscontext.graph.languages.base import FileFacts, LanguagePart


def _error_span(root) -> tuple[int, int]:
    """1-based lines of the first innermost ERROR or MISSING node."""
    node = root
    while True:
        bad = next((c for c in node.children if c.has_error or c.is_missing), None)
        if bad is None:
            return node.start_point.row + 1, node.end_point.row + 1
        node = bad


def facts(part: LanguagePart, source: bytes, path: str) -> FileFacts:
    """Parse one file with its part's grammar and extract its facts. A syntax error keeps the facts
    found around it and sets `error`."""
    from tree_sitter import Parser

    tree = Parser(part.grammar(path)).parse(source)
    found = part.extract(tree, source)
    if not tree.root_node.has_error:
        return found
    start, end = _error_span(tree.root_node)
    where = f"line {start}" if start == end else f"lines {start}-{end}"
    return replace(found, error=f"syntax error at {where}")
