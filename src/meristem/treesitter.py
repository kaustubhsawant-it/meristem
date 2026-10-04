"""Tree-sitter symbol extraction (SPEC §16 — tree-sitter symbol graphs).

Runs a per-language node query over the real syntax tree instead of grepping
source text, so a `def` inside a triple-quoted string or a commented-out
`class Foo:` can never mint a phantom symbol atom, and staleness detection
matches an identifier node's exact text rather than a `\\b`-boundary regex —
the class of bug that made punctuation-prefixed names (npm scopes, `$`-prefixed
JS identifiers) read as permanently-dead drift, twice (see `token_pattern`).

Each grammar is a standalone, prebuilt-wheel package (`tree-sitter-python`,
`tree-sitter-javascript`, ...), not the SPEC §12 stack table's original
`tree_sitter_languages` — that package pins an ABI the current `tree-sitter`
core has moved past (`Language.__init__()` rejects its capsules outright).
The per-language packages are tree-sitter org's own replacement and the ones
actually exercised here; SPEC §12 carries a dated correction.

A language with no grammar importable right now — package not installed, or
`tree-sitter` core itself missing — degrades rather than breaks: callers get
`None` and fall back to the regex extractor in `ingesters/symbols.py`, same
shape as `embeddings.py`'s `_vec_lib()` probe for `sqlite_vec`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Symbol type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str  # capture name from the grammar's query: function/class/const/...


# ---------------------------------------------------------------------------
# Per-language grammar specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Grammar:
    module: str  # importable package, e.g. "tree_sitter_python"
    lang_attr: str  # attribute on the module producing the language capsule
    query: str  # tree-sitter query source; capture names become Symbol.kind
    node_filter: Callable[[str], bool] | None = None  # keep captured text?


# Exported-surface only, matching the regex extractor it replaces: JS/TS keep
# only `export`ed top-level declarations, Go/Rust keep only capitalized/`pub`
# ones. Python and Dart have no such marker in the language, so (as before)
# every module-level definition counts.
_PY_QUERY = """
(module (function_definition name: (identifier) @function))
(module (decorated_definition definition: (function_definition name: (identifier) @function)))
(module (class_definition name: (identifier) @class))
(module (decorated_definition definition: (class_definition name: (identifier) @class)))
"""

_DART_QUERY = """
(class_definition name: (identifier) @class)
(mixin_declaration (identifier) @class)
(enum_declaration name: (identifier) @class)
"""

_JS_QUERY = """
(export_statement (function_declaration name: (identifier) @function))
(export_statement (class_declaration name: (identifier) @class))
(export_statement (lexical_declaration (variable_declarator name: (identifier) @const)))
"""

# TypeScript's grammar types a class/interface/type-alias name as
# `type_identifier`, not the plain `identifier` JS uses for the same slot —
# a real grammar difference, not a typo; using `identifier` here makes the
# query pattern impossible to satisfy and `Query()` raises.
_TS_QUERY = """
(export_statement (function_declaration name: (identifier) @function))
(export_statement (class_declaration name: (type_identifier) @class))
(export_statement (interface_declaration name: (type_identifier) @interface))
(export_statement (type_alias_declaration name: (type_identifier) @type))
"""

_GO_QUERY = """
(function_declaration name: (identifier) @function)
(method_declaration name: (field_identifier) @function)
(type_declaration (type_spec name: (type_identifier) @class))
"""

_RUST_QUERY = """
(function_item (visibility_modifier) name: (identifier) @function)
(struct_item (visibility_modifier) name: (type_identifier) @class)
(enum_item (visibility_modifier) name: (type_identifier) @class)
(trait_item (visibility_modifier) name: (type_identifier) @class)
"""

_UPPERCASE_FIRST: Callable[[str], bool] = lambda name: name[:1].isupper()  # noqa: E731

_GRAMMARS: dict[str, _Grammar] = {
    "python": _Grammar(module="tree_sitter_python", lang_attr="language", query=_PY_QUERY),
    "dart": _Grammar(module="tree_sitter_dart", lang_attr="language", query=_DART_QUERY),
    "javascript": _Grammar(module="tree_sitter_javascript", lang_attr="language", query=_JS_QUERY),
    "typescript": _Grammar(
        module="tree_sitter_typescript", lang_attr="language_typescript", query=_TS_QUERY
    ),
    "tsx": _Grammar(module="tree_sitter_typescript", lang_attr="language_tsx", query=_TS_QUERY),
    "go": _Grammar(
        module="tree_sitter_go", lang_attr="language", query=_GO_QUERY,
        node_filter=_UPPERCASE_FIRST,
    ),
    "rust": _Grammar(module="tree_sitter_rust", lang_attr="language", query=_RUST_QUERY),
}

_SUFFIX_LANG: dict[str, str] = {
    ".py": "python",
    ".dart": "dart",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
}

# lang_key -> (Language, Query) once loaded, or False if unavailable. Probed
# once per key, mirroring embeddings.py's `_VEC_LIB` sentinel.
# `False` marks a grammar we tried and could not load, so it is never retried.
# Typed as Any rather than object because the tuple holds tree_sitter Language
# and Query instances, which every consumer immediately passes back into
# tree_sitter APIs — `object` made those calls uncheckable AND un-callable.
_LOADED: dict[str, Any] = {}


def language_for_suffix(suffix: str) -> str | None:
    """The grammar key for a file suffix (``.py`` -> ``"python"``), or None if
    no grammar in this module covers it."""
    return _SUFFIX_LANG.get(suffix.lower())


def _load(lang_key: str) -> tuple[Any, Any] | None:
    if lang_key in _LOADED:
        loaded = _LOADED[lang_key]
        return loaded if loaded is not False else None
    spec = _GRAMMARS.get(lang_key)
    if spec is None:
        _LOADED[lang_key] = False
        return None
    try:
        from tree_sitter import Language, Query

        module = importlib.import_module(spec.module)
        capsule = getattr(module, spec.lang_attr)()
        language = Language(capsule)
        query = Query(language, spec.query)
    except ImportError:
        _LOADED[lang_key] = False
        return None
    _LOADED[lang_key] = (language, query)
    return (language, query)


def is_available(lang_key: str) -> bool:
    """True if this language's grammar (and tree-sitter core) can be loaded
    right now. Checked per-key, not assumed from `tree-sitter` core alone —
    a base install can have the core and be missing one grammar package."""
    return _load(lang_key) is not None


def available_languages() -> frozenset[str]:
    return frozenset(key for key in _GRAMMARS if is_available(key))


def extract_symbols(lang_key: str, text: str) -> list[Symbol] | None:
    """Declared symbols in `text`, parsed as `lang_key`. None if that
    language's grammar isn't loadable right now — never raises for that
    reason; a genuinely malformed source file still yields whatever partial
    tree tree-sitter's error recovery produces, which is the honest signal a
    hard failure here would otherwise discard."""
    loaded = _load(lang_key)
    if loaded is None:
        return None
    language, query = loaded
    from tree_sitter import Parser, QueryCursor

    parser = Parser(language)
    tree = parser.parse(text.encode("utf-8", errors="replace"))
    node_filter = _GRAMMARS[lang_key].node_filter

    seen: set[str] = set()
    out: list[Symbol] = []
    cursor = QueryCursor(query)
    for _pattern_idx, captures in cursor.matches(tree.root_node):
        for kind, nodes in captures.items():
            for node in nodes:
                name = node.text.decode("utf-8", errors="replace") if node.text else ""
                if not name or name in seen:
                    continue
                if node_filter and not node_filter(name):
                    continue
                seen.add(name)
                out.append(Symbol(name=name, kind=kind))
    return out


def symbol_declared(lang_key: str, text: str, name: str) -> bool | None:
    """Is `name` still declared per a live re-parse? None if this language's
    grammar can't be loaded right now (the `ast` liveness predicate's
    "unverifiable" case — see `liveness.py`)."""
    symbols = extract_symbols(lang_key, text)
    if symbols is None:
        return None
    return any(s.name == name for s in symbols)
