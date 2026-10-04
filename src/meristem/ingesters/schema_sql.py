"""SQL schema ingester — CREATE TABLE statements → schema_fact atoms.

Scans *.sql files under the workspace root, regex-extracts each CREATE TABLE
declaration plus its column list, and emits one atom per table. Topic_key
is ``table:<name>`` so a renamed/dropped table auto-supersedes the prior
fact on the next ingest.

Liveness predicate is a regex against the source file checking that the
``CREATE TABLE <name>`` declaration still exists. When a table is dropped
or the file is renamed, the next liveness pass flips the ``⚠ stale`` count
in the Memory Pulse statusline (SPEC §13) — exactly the schema-drift
detection that's the killer feature.
"""

from __future__ import annotations

import re

from ..atoms import Liveness, assert_fact
from .base import IngestContext, IngestResult, iter_files

# Tolerant enough for the SQL dialects we expect (Postgres, SQLite, MySQL).
# Captures: 1=table_name (with optional schema.qualifier), 2=column body.
_CREATE_TABLE_RE = re.compile(
    r"""CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?
        (?:"?([A-Za-z_][A-Za-z0-9_]*"?\.)?
            "?([A-Za-z_][A-Za-z0-9_]*)"?)\s*
        \((.*?)\)\s*(?:WITHOUT\s+ROWID|STRICT|;)
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

_COL_RE = re.compile(r"^\s*\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s+([A-Za-z]\w*(?:\([^)]*\))?)")

# Tables per ingest run, across every .sql file combined. `iter_files` has no
# count limit of its own (only a per-file size cap), so a large schema tree
# had nothing bounding total atoms — mirrors invariants.py's MAX_INVARIANTS,
# which walks the same files for the same CREATE TABLE declarations.
MAX_TABLES = 300


def _split_top_level(body: str) -> list[str]:
    """Split a CREATE TABLE body on commas that are not inside parentheses.

    Shared with the `invariants` adapter, which needs the same declarations this
    one parses but keeps the constraint clauses instead of discarding them.
    """
    depth = 0
    buf: list[str] = []
    parts: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _columns(body: str) -> list[tuple[str, str]]:
    """Best-effort column extraction. Skips constraint clauses + nested commas."""
    cols: list[tuple[str, str]] = []
    for part in _split_top_level(body):
        line = part.strip()
        if not line:
            continue
        if re.match(r"^(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT)\b", line, re.IGNORECASE):
            continue
        m = _COL_RE.match(line)
        if m:
            cols.append((m.group(1), m.group(2)))
    return cols


def run(ctx: IngestContext) -> IngestResult:
    res = IngestResult(name="schema")
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
        if res.atoms_inserted >= MAX_TABLES:
            res.notes.append(f"capped at {MAX_TABLES} tables")
            res.truncated = True
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            res.notes.append(f"{path.name}: {exc}")
            res.atoms_skipped += 1
            continue
        rel = ctx.rel_path(path)
        for m in _CREATE_TABLE_RE.finditer(text):
            name = m.group(2)
            cols = _columns(m.group(3))
            col_preview = ", ".join(f"{n} {t}" for n, t in cols[:6])
            more = f" (+{len(cols) - 6} more)" if len(cols) > 6 else ""
            summary_50 = (
                f"Table `{name}` defined in {rel} with {len(cols)} columns: "
                f"{col_preview}{more}."
            )
            summary_10 = f"table {name} ({len(cols)} cols)"
            summary_250 = summary_50 + " " + "; ".join(
                f"{n}:{t}" for n, t in cols
            )
            assert_fact(
                ctx.conn,
                type="schema_fact",
                topic_key=f"table:{name}",
                summary_10w=summary_10,
                summary_50w=summary_50,
                summary_250w=summary_250,
                source_kind="schema_snapshot",
                source_ref=rel,
                liveness=Liveness(
                    kind="regex",
                    target=rel,
                    # Mirror _CREATE_TABLE_RE's tolerance: case-insensitive (the
                    # liveness runner only applies re.MULTILINE, so the flag must
                    # be inline) and an optional `schema.` qualifier — otherwise
                    # lowercase / `public.<name>` DDL fails its own liveness check.
                    pattern=rf"(?i)CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\"?\w+\"?\.)?\"?{re.escape(name)}\"?",
                ),
                repo_id=ctx.repo_id,
                workspace_id=ctx.workspace_id,
            )
            res.atoms_inserted += 1
    return res
