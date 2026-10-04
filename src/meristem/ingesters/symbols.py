"""Symbol ingester — top-level fn/class declarations → glossary atoms.

Tree-sitter (`../treesitter.py`, SPEC §16) parses each file's real syntax
tree and gets an `ast` liveness predicate — an exact identifier-node match
against a live re-parse. A language whose grammar isn't loadable right now
falls back to the original per-language regex extractor below with a
`regex` predicate, so a partial `pip install` degrades ingest instead of
breaking it.

The cap (``MAX_ATOMS``) keeps the first ingest run from blowing past its
budget on large monorepos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import treesitter
from ..atoms import Liveness, assert_fact
from .base import IngestContext, IngestResult, iter_files, token_pattern

MAX_ATOMS = 200  # per ingest run


@dataclass(frozen=True)
class _Lang:
    name: str
    suffixes: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...]


# Regex fallback, used only for a suffix whose tree-sitter grammar isn't
# loadable right now. Kept in the same shape as the pre-tree-sitter v1.
_LANGUAGES: tuple[_Lang, ...] = (
    _Lang(
        name="python",
        suffixes=(".py",),
        patterns=(
            re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE),
            re.compile(r"^class\s+([A-Za-z_]\w*)\b", re.MULTILINE),
        ),
    ),
    _Lang(
        name="dart",
        suffixes=(".dart",),
        patterns=(
            re.compile(r"^(?:abstract\s+|sealed\s+)?class\s+([A-Za-z_]\w*)\b", re.MULTILINE),
            re.compile(r"^(?:mixin|enum)\s+([A-Za-z_]\w*)\b", re.MULTILINE),
        ),
    ),
    _Lang(
        name="javascript",
        suffixes=(".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"),
        patterns=(
            re.compile(
                r"^export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
                re.MULTILINE,
            ),
            re.compile(r"^export\s+(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.MULTILINE),
            re.compile(r"^export\s+const\s+([A-Za-z_$][\w$]*)\s*[=:]", re.MULTILINE),
        ),
    ),
    _Lang(
        name="go",
        suffixes=(".go",),
        patterns=(
            re.compile(r"^func\s+(?:\([^)]*\)\s+)?([A-Z][A-Za-z0-9_]*)\s*\(", re.MULTILINE),
            re.compile(r"^type\s+([A-Z][A-Za-z0-9_]*)\s+", re.MULTILINE),
        ),
    ),
    _Lang(
        name="rust",
        suffixes=(".rs",),
        patterns=(
            re.compile(r"^pub\s+(?:async\s+)?fn\s+([a-zA-Z_]\w*)", re.MULTILINE),
            re.compile(r"^pub\s+(?:struct|enum|trait)\s+([A-Z]\w*)", re.MULTILINE),
        ),
    ),
    # No tree-sitter grammar for Swift is wired in treesitter.py, so this is
    # the only extraction path -- without it a Swift-only repo (e.g. a macOS/
    # iOS app) gets zero symbol-tier atoms from its actual code, no matter
    # how much of it there is (found ingesting a real Swift codebase: 17
    # source files, "no source files matched").
    #
    # Unlike every other language here, patterns allow leading indentation
    # (`^[ \t]*` rather than a bare `^`). Swift's convention nests almost
    # every `func` inside a `struct`/`class`/`extension` body -- a column-0-
    # only anchor, which is fine for Python/Go/Rust's common top-level style,
    # would catch type declarations but miss nearly all individual methods,
    # which is most of what's worth knowing in a real Swift codebase.
    _Lang(
        name="swift",
        suffixes=(".swift",),
        patterns=(
            re.compile(
                r"^[ \t]*(?:(?:public|private|internal|fileprivate|open)\s+)?"
                r"(?:(?:final|static|override|mutating|class)\s+)*func\s+([A-Za-z_]\w*)\s*[(<]",
                re.MULTILINE,
            ),
            re.compile(
                r"^[ \t]*(?:(?:public|private|internal|fileprivate|open)\s+)?"
                r"(?:final\s+)?(?:class|struct|enum|protocol)\s+([A-Za-z_]\w*)\b",
                re.MULTILINE,
            ),
            re.compile(r"^[ \t]*extension\s+([A-Za-z_][\w.]*)\b", re.MULTILINE),
        ),
    ),
    # Also no tree-sitter grammar wired -- same gap as Swift, found the same
    # way (a real PHP repo's symbols pass reported nothing while its .php
    # files were silently invisible to the suffix filter). Allows leading
    # indentation for the same reason as Swift: PHP's OO-heavy codebases
    # (Laravel/Symfony/WordPress-style) indent every method inside a class,
    # even though procedural PHP -- this codebase's own style -- already
    # declares at column 0.
    _Lang(
        name="php",
        suffixes=(".php",),
        patterns=(
            re.compile(
                r"^[ \t]*(?:(?:public|private|protected|static|final|abstract)\s+)*"
                r"function\s+([A-Za-z_]\w*)\s*\(",
                re.MULTILINE,
            ),
            re.compile(
                r"^[ \t]*(?:final\s+|abstract\s+)?(?:class|interface|trait)\s+([A-Za-z_]\w*)\b",
                re.MULTILINE,
            ),
        ),
    ),
)

_ALL_SUFFIXES: tuple[str, ...] = tuple(s for lang in _LANGUAGES for s in lang.suffixes)


def _lang_for(suffix: str) -> _Lang | None:
    s = suffix.lower()
    for lang in _LANGUAGES:
        if s in lang.suffixes:
            return lang
    return None


def _regex_extract(lang: _Lang, text: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for pat in lang.patterns:
        for m in pat.finditer(text):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            out.append((name, "symbol"))
    return out


def _extract(
    suffix: str, fallback_lang: _Lang, text: str
) -> tuple[list[tuple[str, str]], str, str]:
    """Returns (symbols as (name, kind) pairs, liveness kind, label for the
    summary text). Tries tree-sitter first; regex is the fallback path."""
    ts_key = treesitter.language_for_suffix(suffix)
    if ts_key is not None:
        symbols = treesitter.extract_symbols(ts_key, text)
        if symbols is not None:
            return [(s.name, s.kind) for s in symbols], "ast", ts_key
    return _regex_extract(fallback_lang, text), "regex", fallback_lang.name


def run(ctx: IngestContext) -> IngestResult:
    res = IngestResult(name="symbols")
    files = iter_files(
        ctx.root,
        suffixes=_ALL_SUFFIXES,
        exclude=ctx.exclude,
        max_kb=ctx.max_file_kb,
        changed=ctx.changed_files,
    )
    if not files:
        res.notes.append("no source files matched")
        return res

    ast_langs: set[str] = set()
    regex_langs: set[str] = set()

    for path in files:
        if res.atoms_inserted >= MAX_ATOMS:
            res.notes.append(f"capped at {MAX_ATOMS} atoms; remaining files skipped")
            res.truncated = True  # don't let the indexed sha advance past skipped files
            break
        lang = _lang_for(path.suffix)
        if not lang:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            res.atoms_skipped += 1
            continue
        rel = ctx.rel_path(path)

        symbols, live_kind, label = _extract(path.suffix, lang, text)
        (ast_langs if live_kind == "ast" else regex_langs).add(label)

        for name, kind_word in symbols:
            summary_10 = f"{label} {kind_word} {name} in {Path(rel).name}"
            summary_50 = (
                f"{label} {kind_word} `{name}` declared in {rel}. Public "
                f"surface — referenced from {Path(rel).parent or 'root'}."
            )
            if live_kind == "ast":
                live = Liveness(kind="ast", target=rel, pattern=name)
            else:
                live = Liveness(kind="regex", target=rel, pattern=token_pattern(name))
            assert_fact(
                ctx.conn,
                type="glossary",
                topic_key=f"symbol:{rel}:{name}",
                summary_10w=summary_10,
                summary_50w=summary_50,
                source_kind="manifest",
                source_ref=f"{rel}:0",
                liveness=live,
                repo_id=ctx.repo_id,
                workspace_id=ctx.workspace_id,
            )
            res.atoms_inserted += 1
            if res.atoms_inserted >= MAX_ATOMS:
                break

    if ast_langs:
        res.notes.append(f"ast-parsed via tree-sitter: {', '.join(sorted(ast_langs))}")
    if regex_langs:
        res.notes.append(
            f"regex fallback (grammar not installed): {', '.join(sorted(regex_langs))}"
        )
    return res
