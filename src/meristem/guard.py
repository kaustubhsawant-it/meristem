"""Trust guard — a deterministic, stdlib-only scanner for secrets and personal data.

Memory that leaves the machine (the shared export) or that a user never typed
as a durable fact (captured chat) must not carry credentials or personal data.
`scan` finds them; `redact` masks them. Nothing here ever returns, logs or
stores a matched value — callers get a kind and a span, and must keep it that
way.

The rules are precision-first: a guard that cries wolf gets disabled. Hashes,
UUIDs, version strings, credential-free URLs, dotted identifiers and prose all
pass; each exclusion below is pinned by a test in `tests/test_guard.py`.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int


@dataclass(frozen=True)
class Policy:
    """What to scan for. `allow_patterns` are regexes; a finding whose matched
    text matches any of them (re.search) is whitelisted. `exclude_terms` is the
    workspace's own denylist (`[capture] exclude_terms`)."""

    enabled: bool = True
    allow_patterns: tuple[str, ...] = ()
    exclude_terms: tuple[str, ...] = ()


def policy_from_config(cfg) -> Policy:
    """Build a Policy from a loaded `config.Config` (duck-typed: no import cycle)."""
    g = cfg.guard
    return Policy(
        enabled=bool(g.enabled),
        allow_patterns=tuple(g.allow_patterns),
        exclude_terms=tuple(cfg.capture.exclude_terms),
    )


SECRET_KINDS = frozenset({
    "private-key", "aws-access-key", "aws-secret-key", "github-token", "slack-token",
    "anthropic-key", "openai-key", "google-api-key", "jwt", "url-credentials",
    "credential-assignment", "high-entropy",
})
PERSONAL_KINDS = frozenset({"email", "phone", "excluded-term"})


# --- secret patterns (ordered: specific before generic) ----------------------

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
        r"(?:[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|[\s\S]*)"
    )),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("aws-secret-key", re.compile(
        r"(?i)aws[_-]?secret[_-]?(?:access[_-]?)?key[\"']?\s*[:=]\s*[\"']?"
        r"[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"
    )),
    ("github-token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})\b"
    )),
    ("slack-token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    # a digit is required so hyphenated prose ("sk-learn-compatible-...") passes
    ("openai-key", re.compile(r"\bsk-(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"
    )),
    ("url-credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@[^/\s]+", re.I)),
]

_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z])(?:password|passwd|secret|token|api[_-]?key)"
    r"(?:[_-]?(?:key|token|value))?[\"']?\s*[:=]\s*(?P<val>\S{6,})"
)
_DOTTED_IDENT = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
_SPECIAL = set("!@#%^&*+/=~")


def _credential_value_ok(val: str) -> bool:
    quoted = val[:1] in "\"'" and len(val) >= 2
    v = val.strip("\"',;)")
    if len(v) < 6:
        return False
    if v[0] in "$%{<[(*" or any(c in v for c in "()[]{}<>"):
        return False  # a reference or template, not a literal
    if _DOTTED_IDENT.match(v) or len(set(v)) <= 2:
        return False
    return quoted or any(c.isdigit() for c in v) or any(c in _SPECIAL for c in v)


# --- high entropy -------------------------------------------------------------

_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_HEX = re.compile(r"^[0-9a-fA-F]+$")
_HEX_LENGTHS = frozenset({32, 40, 56, 64, 96, 128})
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ENTROPY_MIN = 4.0


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _high_entropy(text: str) -> Iterable[Finding]:
    for m in _TOKEN.finditer(text):
        tok = m.group()
        if re.match(r"sha(?:256|384|512)-", tok, re.I):
            continue  # subresource-integrity / lockfile hash
        if tok.count("/") >= 2 or _UUID.match(tok):
            continue  # a path, not a secret
        if _HEX.match(tok) and len(tok) in _HEX_LENGTHS:
            continue  # git sha / md5 / sha256 etc.
        if not (any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)):
            continue
        if _entropy(tok) >= ENTROPY_MIN:
            yield Finding("high-entropy", m.start(), m.end())


# --- personal data ------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_EMAIL_OK = re.compile(
    r"noreply|no-reply|@(?:[a-z0-9-]+\.)*(?:example\.(?:com|org|net)|localhost|invalid|test)$",
    re.I,
)
_PHONE = re.compile(
    r"(?<![\w.+-])(?:"
    r"\+\d{7,15}"
    r"|\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}"
    r"|\(\d{2,4}\)[\s.-]?\d{3,4}[\s.-]\d{3,4}"
    r"|\d{3,4}[\s-]\d{3,4}[\s-]\d{3,4}"
    r")(?![\w])"
)


def _emails(text: str) -> Iterable[Finding]:
    for m in _EMAIL.finditer(text):
        if not _EMAIL_OK.search(m.group()):
            yield Finding("email", m.start(), m.end())


def _phones(text: str) -> Iterable[Finding]:
    for m in _PHONE.finditer(text):
        digits = sum(c.isdigit() for c in m.group())
        if 10 <= digits <= 15:
            yield Finding("phone", m.start(), m.end())


def _terms(text: str, terms: Sequence[str]) -> Iterable[Finding]:
    alt = "|".join(re.escape(t) for t in terms if t)
    if alt:
        for m in re.finditer(rf"\b(?:{alt})\b", text, re.IGNORECASE):
            yield Finding("excluded-term", m.start(), m.end())


# --- public API ---------------------------------------------------------------

def _compile_allow(patterns: Sequence[str]) -> list[re.Pattern[str]]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p))
        except re.error:
            continue  # a bad whitelist entry must never widen or crash the scan
    return out


def _raw_findings(text: str, exclude_terms: Sequence[str]) -> Iterable[Finding]:
    for kind, pat in _PATTERNS:
        for m in pat.finditer(text):
            yield Finding(kind, m.start(), m.end())
    for m in _ASSIGNMENT.finditer(text):
        if _credential_value_ok(m.group("val")):
            yield Finding("credential-assignment", m.start(), m.end())
    yield from _high_entropy(text)
    yield from _emails(text)
    yield from _phones(text)
    yield from _terms(text, exclude_terms)


def scan(
    text: str,
    *,
    policy: Policy | None = None,
    allow_patterns: Sequence[str] = (),
    exclude_terms: Sequence[str] = (),
) -> list[Finding]:
    """Findings in `text`, ordered by position, overlaps collapsed (the earlier
    / more specific rule wins). `policy`, when given, supplies enabled /
    allow_patterns / exclude_terms; the keyword args extend it."""
    if not text:
        return []
    if policy is not None:
        if not policy.enabled:
            return []
        allow_patterns = (*policy.allow_patterns, *allow_patterns)
        exclude_terms = (*policy.exclude_terms, *exclude_terms)
    allow = _compile_allow(allow_patterns)
    kept: list[Finding] = []
    for f in _raw_findings(text, exclude_terms):
        if any(f.start < k.end and k.start < f.end for k in kept):
            continue
        matched = text[f.start:f.end]
        if any(a.search(matched) for a in allow):
            continue
        kept.append(f)
    kept.sort(key=lambda f: (f.start, f.end))
    return kept


def redact(text: str, **kw) -> str:
    """`text` with every finding replaced by `[REDACTED:<kind>]`."""
    out: list[str] = []
    pos = 0
    for f in scan(text, **kw):
        out.append(text[pos:f.start])
        out.append(f"[REDACTED:{f.kind}]")
        pos = f.end
    out.append(text[pos:])
    return "".join(out)


def kinds_in(text: str, **kw) -> list[str]:
    """Distinct finding kinds in `text`, in order of first appearance."""
    return list(dict.fromkeys(f.kind for f in scan(text, **kw)))
