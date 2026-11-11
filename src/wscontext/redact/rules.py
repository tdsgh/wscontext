"""Built-in redaction rules. Each rule names itself in its replacement marker."""

import hashlib
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    group: int = 0  # the capture group to replace; 0 replaces the whole match
    check: Callable[[str], bool] | None = None  # veto a match by its replaced text


UUID = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f])")


def account_digest(token: str) -> str:
    return hashlib.sha256(token.lower().encode()).hexdigest()


def account_rule(digests: Iterable[str]) -> Rule:
    """Masks a UUID-shaped token whose digest is one of `digests`."""
    known = frozenset(digests)
    return Rule("account-id", UUID, check=lambda token: account_digest(token) in known)


def _entropy(s: str) -> float:
    return -sum(s.count(c) / len(s) * math.log2(s.count(c) / len(s)) for c in set(s))


def _looks_random(s: str) -> bool:
    return (len(s) >= 20 and _entropy(s) >= 3.5
            and any(c.isdigit() for c in s) and any(c.isalpha() for c in s))


_SECRET_WORDS = r"(?:SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|AUTH)"
_VALUE = r"""(?!\[REDACTED)(?:"[^"\\\n]+"|'[^'\\\n]+'|[^\s"'\\]+)"""

BUILTIN_RULES: tuple[Rule, ...] = (
    Rule("private-key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
        r"(?:[A-Za-z0-9+/=\s\\:,.-]*?-----END [A-Z ]*PRIVATE KEY-----"
        r"|[A-Za-z0-9+/=\s\\]{0,4096})")),
    Rule("aws-key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b")),
    Rule("gcp-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    Rule("github-token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    Rule("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    Rule("slack-token", re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}")),
    Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    Rule("bearer-token", re.compile(r"\b[Bb]earer\s+([A-Za-z0-9._~+/=-]{16,})"), group=1),
    Rule("env-secret", re.compile(
        rf"(?<![A-Za-z0-9_])(?:export\s+)?[A-Z0-9_]*?{_SECRET_WORDS}(?![A-Z0-9])[A-Z0-9_]*"
        rf"=({_VALUE})"), group=1),
    Rule("high-entropy-assignment", re.compile(
        r"""(?i)\b(?:secret|token|password|passwd|api[_-]?key|credentials?)["']?\s*[:=]\s*"""
        r"""["']?([A-Za-z0-9+/_=.-]{20,})"""), group=1, check=_looks_random),
)
