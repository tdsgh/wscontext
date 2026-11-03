"""Applies redaction rules to text or bytes. Stdlib only; safe to call from a hook."""

import logging
import re
from collections.abc import Iterable

from wscontext.config import Config
from wscontext.redact.rules import BUILTIN_RULES, Rule

log = logging.getLogger(__name__)


def compile_extra(patterns: Iterable[str]) -> list[Rule]:
    """Turn configured regex strings into rules named `custom-N`. A bad pattern is logged and skipped."""
    rules = []
    for i, pattern in enumerate(patterns, 1):
        try:
            rules.append(Rule(f"custom-{i}", re.compile(pattern)))
        except re.error as e:
            log.warning("invalid redaction pattern %d ignored: %s", i, e)
    return rules


def redact(text: str, extra_rules: Iterable[str] = ()) -> tuple[str, list[str]]:
    """Return the text with every match replaced by `[REDACTED:<rule>]`, and one rule name per hit.

    `extra_rules` are regex strings added to the built-in rules.
    """
    hits: list[str] = []
    for rule in (*BUILTIN_RULES, *compile_extra(extra_rules)):
        def replace(m: re.Match[str], rule: Rule = rule) -> str:
            secret = m.group(rule.group)
            if rule.check is not None and not rule.check(secret):
                return m.group(0)
            hits.append(rule.name)
            marker = f"[REDACTED:{rule.name}]"
            if rule.group == 0:
                return marker
            start, end = m.span(rule.group)
            return m.group(0)[: start - m.start()] + marker + m.group(0)[end - m.start():]

        text = rule.pattern.sub(replace, text)
    return text, hits


def redact_text(text: str, config: Config) -> tuple[str, list[str]]:
    return redact(text, config.redaction.extra_patterns)


def redact_value(value, config: Config):
    """Redact every string, dict key included, in a JSON-shaped value. The shape never changes."""
    if isinstance(value, str):
        return redact_text(value, config)[0]
    if isinstance(value, dict):
        return {redact_value(k, config): redact_value(v, config) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, config) for v in value]
    return value


def redact_bytes(data: bytes, config: Config) -> tuple[bytes, list[str]]:
    """The single entry point for content arriving as bytes, e.g. history copied from teammates."""
    text, hits = redact(data.decode("utf-8", errors="surrogateescape"),
                        config.redaction.extra_patterns)
    return text.encode("utf-8", errors="surrogateescape"), hits
