"""Invariant ingester — machine-checkable constraints already in the code.

Four of the nine atom types had no producer at all. `invariant` was the worst
of them: `/meristem:check` spawns an "invariant auditor" sub-agent, SPEC §15's
`/meristem:plan` is specified to "retrieve invariants, closed decisions, owners",
and `schema.sql` defines an `invariants_live` view — all of it operating on an
empty set. Every clean `/meristem:check` run in Meristem's history proved less than it
appeared to, because there was nothing to audit against.

This adapter mines invariants that are *already declared and enforced* in the
repo, so each atom is true by construction and carries a predicate that can
actually re-verify it:

    NOT NULL    → "<table>.<col> is required"
    UNIQUE      → "<table>.<col> must be unique"
    CHECK(...)  → "<table>.<col> is constrained to <expr>"
    REFERENCES  → "<table>.<col> must reference <other>(<col>)"
    PRIMARY KEY → "<table> is keyed by <col>"

The deliberate limit: these are *declared* invariants, not inferred ones. An
LLM pass would find more (business rules that live in prose), but it would also
propose claims whose predicates cannot run — feeding the exact "unverifiable
atom" problem the liveness work exists to close. Declared constraints are the
subset Meristem can stand behind, so they are the subset it asserts.

Liveness: the predicate is built from the column's own declaration text, with
whitespace relaxed, so it matches the real DDL and nothing else. Drop the
constraint and the next liveness pass flips the atom stale — which is the
schema-drift detection that makes the memory self-invalidating.
"""

from __future__ import annotations

import re

from ..atoms import Liveness, assert_fact
from .base import IngestContext, IngestResult, iter_files
from .schema_sql import _CREATE_TABLE_RE, _split_top_level

# Column-level constraint keywords, in the order we report them.
_NOT_NULL_RE = re.compile(r"\bNOT\s+NULL\b", re.IGNORECASE)
_UNIQUE_RE = re.compile(r"\bUNIQUE\b", re.IGNORECASE)
_PRIMARY_KEY_RE = re.compile(r"\bPRIMARY\s+KEY\b", re.IGNORECASE)
_CHECK_RE = re.compile(r"\bCHECK\s*\((.+)\)", re.IGNORECASE | re.DOTALL)
_REFERENCES_RE = re.compile(
    r"\bREFERENCES\s+\"?(\w+)\"?\s*(?:\(\s*\"?(\w+)\"?\s*\))?", re.IGNORECASE
)
_COL_NAME_RE = re.compile(r'^\s*"?([A-Za-z_][A-Za-z0-9_]*)"?\s')

# Table-level clauses we skip when looking for column definitions.
_TABLE_LEVEL_RE = re.compile(
    r"^(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT)\b", re.IGNORECASE
)

MAX_INVARIANTS = 400  # keep one pathological schema from swamping the corpus


def _liveness_pattern(decl: str) -> str:
    """A regex matching this exact column declaration, whitespace-relaxed.

    Built from the declaration itself rather than a generic
    "<column>.*NOT NULL" so it cannot match a same-named column on a different
    table. Whitespace is collapsed to `\\s+` because formatters reflow DDL and
    a reformat is not a schema change.
    """
    return r"\s+".join(re.escape(tok) for tok in decl.split())


def _invariants_for_column(table: str, decl: str) -> list[tuple[str, str, str]]:
    """(kind, topic_suffix, human claim) for each constraint on one column."""
    m = _COL_NAME_RE.match(decl)
    if not m:
        return []
    col = m.group(1)
    out: list[tuple[str, str, str]] = []
    if _PRIMARY_KEY_RE.search(decl):
        out.append(("primary_key", f"{col}.pk", f"`{table}` is keyed by `{col}`."))
    if _NOT_NULL_RE.search(decl):
        out.append((
            "not_null", f"{col}.not_null",
            f"`{table}.{col}` is required — NOT NULL is enforced by the schema.",
        ))
    if _UNIQUE_RE.search(decl):
        out.append((
            "unique", f"{col}.unique",
            f"`{table}.{col}` must be unique — duplicates are rejected by the schema.",
        ))
    if (chk := _CHECK_RE.search(decl)):
        expr = " ".join(chk.group(1).split())[:120]
        out.append((
            "check", f"{col}.check",
            f"`{table}.{col}` is constrained to {expr} — enforced by a CHECK.",
        ))
    if (ref := _REFERENCES_RE.search(decl)):
        target = ref.group(1)
        target_col = ref.group(2) or "id"
        out.append((
            "foreign_key", f"{col}.fk",
            f"`{table}.{col}` must reference `{target}({target_col})` — "
            f"a foreign key enforces it.",
        ))
    return out


def run(ctx: IngestContext) -> IngestResult:
    res = IngestResult(name="invariants")
    files = iter_files(
        ctx.root,
        suffixes=(".sql",),
        exclude=ctx.exclude,
        max_kb=ctx.max_file_kb,
        changed=ctx.changed_files,
    )
    if not files:
        res.notes.append("no .sql files")
        return res

    for path in files:
        if res.atoms_inserted >= MAX_INVARIANTS:
            res.notes.append(f"capped at {MAX_INVARIANTS} invariants")
            res.truncated = True  # don't let the indexed sha advance past skipped files
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            res.notes.append(f"{path.name}: {exc}")
            res.atoms_skipped += 1
            continue
        rel = ctx.rel_path(path)
        for table_m in _CREATE_TABLE_RE.finditer(text):
            table = table_m.group(2)
            for decl in _split_top_level(table_m.group(3)):
                decl = decl.strip()
                if not decl or _TABLE_LEVEL_RE.match(decl):
                    continue
                claims = _invariants_for_column(table, decl)
                if not claims:
                    continue
                pattern = _liveness_pattern(decl)
                for kind, suffix, claim in claims:
                    assert_fact(
                        ctx.conn,
                        type="invariant",
                        topic_key=f"invariant:{table}.{suffix}",
                        summary_10w=f"{table}.{suffix.split('.')[0]} {kind}",
                        summary_50w=claim,
                        summary_250w=f"{claim} Declared in {rel} as: {decl}",
                        source_kind="schema_snapshot",
                        source_ref=rel,
                        liveness=Liveness(kind="regex", target=rel, pattern=pattern),
                        repo_id=ctx.repo_id,
                        workspace_id=ctx.workspace_id,
                    )
                    res.atoms_inserted += 1
                    if res.atoms_inserted >= MAX_INVARIANTS:
                        break
    return res
