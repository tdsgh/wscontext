"""The registry of language parts: the only place that names a language module."""

import importlib
import logging
from collections.abc import Iterable

from wscontext.graph.languages.base import LanguagePart

log = logging.getLogger(__name__)

# language name -> module exposing `PART`
REGISTRY: dict[str, str] = {
    "python": "wscontext.graph.languages.python",
    "typescript": "wscontext.graph.languages.typescript",
    "javascript": "wscontext.graph.languages.javascript",
}


def load_parts(enabled: Iterable[str]) -> tuple[list[LanguagePart], list[str]]:
    """(parts that loaded, names that did not). Never raises: a name missing from the registry, a
    module that fails to import or a grammar that fails to load is unavailable."""
    parts: list[LanguagePart] = []
    unavailable: list[str] = []
    for name in dict.fromkeys(enabled):
        module = REGISTRY.get(name)
        if module is None:
            unavailable.append(name)
            continue
        try:
            part = importlib.import_module(module).PART
            part.load()
        except Exception as e:
            log.warning("graph: language %s unavailable: %s", name, e)
            unavailable.append(name)
            continue
        parts.append(part)
    return parts, unavailable
