"""Atom CRUD with bi-temporal semantics.

Atoms are the unit of knowledge (SPEC §3). An atom is identified by content
hash over `(type, topic_key, summary_50w)`. Asserting a fact with the same
`topic_key` but a different claim auto-supersedes the prior live atom — the
old row stays in history (valid_to + superseded_by are set) and the new row
becomes the live truth.

Public API:
    assert_fact(...)   — insert (and optionally auto-supersede prior topic)
    supersede(...)     — explicit two-step supersede
    archive(...)       — soft delete
    pin(...) / unpin   — protection from consolidation
    get_atom(...)      — atom + summaries + (optional) include archived
    query_atoms(...)   — filterable list (live by default)
    write_summary(...) — upsert a resolution

All writes that span >1 statement use `with conn:` for atomicity.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, get_args

from .ids import atom_id

# ---------------------------------------------------------------------------
# Type aliases — match schema.sql CHECKs
# ---------------------------------------------------------------------------

AtomType = Literal[
    "invariant", "schema_fact", "decision", "convention",
    "dependency", "owner", "runtime", "build_recipe", "glossary",
]
# The same names at runtime. Derived from the Literal via typing.get_args
# rather than retyped, so a new atom type cannot be added to one and forgotten
# in the other — the schema's own CHECK is the third copy and the only one that
# was previously enforcing anything.
ATOM_TYPES: tuple[str, ...] = get_args(AtomType)
DecisionStatus = Literal["OPEN", "CLOSED", "DEFERRED"]
# Which kind of decision an atom is (schema v3). A `constraint` shapes future
# work and must surface before related work starts; a `change` merely records
# that something happened. Sharing one type made "what decisions constrain this
# goal?" answer with commit subject lines at a 76:0 ratio.
DecisionClass = Literal["constraint", "change"]
SourceKind = Literal[
    "commit", "transcript", "schema_snapshot", "sentry_event",
    "screenshot", "readme", "manifest", "llm_extract", "manual",
]
LivenessKind = Literal["regex", "ast", "sql", "none"]
Resolution = Literal[10, 50, 250]

# SPEC §14.5 — source_kinds whose supersedes are mechanical re-ingestion
# (auto-generated, re-run on every sweep), not knowledge in conflict. A new
# commit/schema-snapshot/manifest atom replacing a prior one is normal
# versioning, so we do NOT raise a CONTRADICTS edge for these. Conflict
# detection fires only for human/LLM knowledge assertions.
NATURAL_EVOLUTION_SOURCES: frozenset[str] = frozenset(
    {"commit", "schema_snapshot", "manifest"}
)
Tier = Literal["module", "file", "symbol"]  # SPEC §14.1 — coarse→fine


@dataclass(frozen=True)
class Liveness:
    kind: LivenessKind
    target: str | None = None        # path / query
    pattern: str | None = None       # regex / symbol name (ast) / SQL


@dataclass
class Atom:
    id: str
    type: str
    topic_key: str
    decision_status: str | None
    decision_class: str | None
    tier: str
    source_kind: str
    source_ref: str | None
    source_lines: str | None
    valid_from: int
    valid_to: int | None
    asserted_at: int
    superseded_by: str | None
    liveness_kind: str | None
    liveness_target: str | None
    liveness_pattern: str | None
    liveness_last_ok: int | None
    confidence: float
    repo_id: str | None
    workspace_id: str
    valid_in_refs: list[str]
    pinned: bool
    archived: bool
    # SPEC §14.5 — runtime-only (never persisted): ids of live atoms this
    # assert just superseded with a divergent claim. Empty unless assert_fact
    # raised CONTRADICTS edges. Lets callers surface the conflict instead of
    # the supersede being silent.
    conflicts: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Atom:
        refs = json.loads(r["valid_in_refs"]) if r["valid_in_refs"] else []
        return cls(
            id=r["id"],
            type=r["type"],
            topic_key=r["topic_key"],
            decision_status=r["decision_status"],
            decision_class=r["decision_class"],
            tier=r["tier"],
            source_kind=r["source_kind"],
            source_ref=r["source_ref"],
            source_lines=r["source_lines"],
            valid_from=r["valid_from"],
            valid_to=r["valid_to"],
            asserted_at=r["asserted_at"],
            superseded_by=r["superseded_by"],
            liveness_kind=r["liveness_kind"],
            liveness_target=r["liveness_target"],
            liveness_pattern=r["liveness_pattern"],
            liveness_last_ok=r["liveness_last_ok"],
            confidence=r["confidence"],
            repo_id=r["repo_id"],
            workspace_id=r["workspace_id"],
            valid_in_refs=refs,
            pinned=bool(r["pinned"]),
            archived=bool(r["archived"]),
        )


# ---------------------------------------------------------------------------
# assert_fact
# ---------------------------------------------------------------------------

def assert_fact(
    conn: sqlite3.Connection,
    *,
    type: AtomType,
    topic_key: str,
    summary_50w: str,
    summary_10w: str | None = None,
    summary_250w: str | None = None,
    source_kind: SourceKind,
    source_ref: str | None = None,
    source_lines: str | None = None,
    decision_status: DecisionStatus | None = None,
    decision_class: DecisionClass | None = None,
    tier: Tier = "symbol",
    valid_from: int | None = None,
    liveness: Liveness | None = None,
    confidence: float = 1.0,
    repo_id: str | None = None,
    workspace_id: str = "default",
    valid_in_refs: list[str] | None = None,
    auto_supersede: bool = True,
    pinned: bool = False,
    detect_conflict: bool = True,
) -> Atom:
    """Assert (insert) an atom. Returns the live Atom.

    If `auto_supersede` is True (default) and a live atom exists with the same
    `(type, topic_key, workspace_id)`, that atom's `valid_to` is closed and
    `superseded_by` is pointed at the new id — UNLESS the computed id is the
    same (same claim text), in which case this is a no-op idempotent assert
    and the existing live atom is returned unchanged.

    Conflict detection (SPEC §14.5): when `detect_conflict` is True and the
    supersede closes a live atom with a *divergent* claim, a symmetric
    `CONTRADICTS` edge is raised between old and new, and the closed ids are
    reported on the returned atom's `conflicts` field — so the supersede is
    surfaced rather than silent. Skipped for `NATURAL_EVOLUTION_SOURCES`
    (commit/schema_snapshot/manifest), where supersede is normal re-ingestion.
    The single-live-atom-per-topic invariant is unchanged: the old atom is
    still closed.
    """
    now = int(time.time())
    vf = valid_from if valid_from is not None else now
    refs = valid_in_refs if valid_in_refs is not None else ["main"]
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0,1], got {confidence}")
    if type == "decision" and decision_status is None:
        raise ValueError("decision atoms require decision_status")
    if type != "decision" and decision_status is not None:
        raise ValueError("decision_status only valid for type=decision")
    if type != "decision" and decision_class is not None:
        raise ValueError("decision_class only valid for type=decision")
    if decision_class is not None and decision_class not in ("constraint", "change"):
        raise ValueError(f"decision_class must be constraint|change, got {decision_class!r}")
    if type == "decision" and decision_class is None:
        # Derive rather than default blindly. The git ingester is the only
        # producer of source_kind='commit', and a commit records what happened
        # rather than forbidding anything — so it is a change record. Everything
        # else was asserted deliberately by a human or agent, which is what a
        # constraint is.
        decision_class = "change" if source_kind == "commit" else "constraint"

    new_id = atom_id(type, topic_key, summary_50w)

    with conn:
        # Idempotent: same id already live → return it
        existing = conn.execute(
            "SELECT * FROM atoms WHERE id = ? AND valid_to IS NULL", (new_id,)
        ).fetchone()
        if existing:
            return Atom.from_row(existing)

        # Insert new atom first so the supersede UPDATE's FK target exists.
        conn.execute(
            """INSERT INTO atoms (
                id, type, topic_key, decision_status, decision_class, tier,
                source_kind, source_ref, source_lines,
                valid_from, valid_to, asserted_at, superseded_by,
                liveness_kind, liveness_target, liveness_pattern, liveness_last_ok,
                confidence, repo_id, workspace_id, valid_in_refs,
                pinned, archived, created_at, updated_at
            ) VALUES (?,?,?,?,?,?, ?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?)""",
            (
                new_id, type, topic_key, decision_status, decision_class, tier,
                source_kind, source_ref, source_lines,
                vf, None, now, None,
                (liveness.kind if liveness else None),
                (liveness.target if liveness else None),
                (liveness.pattern if liveness else None),
                None,
                confidence, repo_id, workspace_id, json.dumps(refs),
                int(pinned), 0, now, now,
            ),
        )
        # Auto-supersede prior live atom(s) on same topic (skip the row we just
        # inserted in case it matched the predicate by coincidence). Capture the
        # closed ids first so we can raise CONTRADICTS edges for them below — a
        # divergent claim (different id ⇒ different summary) on the same topic
        # is a conflict, not a silent overwrite.
        superseded_ids: list[str] = []
        if auto_supersede:
            superseded_ids = [
                r["id"]
                for r in conn.execute(
                    """SELECT id FROM atoms
                        WHERE type = ? AND topic_key = ? AND workspace_id = ?
                          AND valid_to IS NULL AND id != ?""",
                    (type, topic_key, workspace_id, new_id),
                )
            ]
            conn.execute(
                """UPDATE atoms
                      SET valid_to = ?, superseded_by = ?, updated_at = ?
                    WHERE type = ? AND topic_key = ? AND workspace_id = ?
                      AND valid_to IS NULL AND id != ?""",
                (now, new_id, now, type, topic_key, workspace_id, new_id),
            )
        # Summaries
        _upsert_summary(conn, new_id, 50, summary_50w)
        if summary_10w is not None:
            _upsert_summary(conn, new_id, 10, summary_10w)
        if summary_250w is not None:
            _upsert_summary(conn, new_id, 250, summary_250w)

    # Conflict detection (SPEC §14.5) — runs AFTER the atom/supersede txn has
    # committed: upsert_edge opens its own `with conn:` and nesting it inside
    # the block above would prematurely commit. Edge creation is advisory
    # metadata; a failure here must never undo a committed supersede, so we
    # isolate each edge and swallow its error.
    conflicts: list[str] = []
    if (
        detect_conflict
        and superseded_ids
        and source_kind not in NATURAL_EVOLUTION_SOURCES
    ):
        from . import edges as _edges

        for old_id in superseded_ids:
            try:
                _edges.upsert_edge(
                    conn,
                    src_id=new_id,
                    dst_id=old_id,
                    kind="CONTRADICTS",
                    source="llm_extract",
                )
                conflicts.append(old_id)
            except Exception:  # noqa: BLE001 — edge is advisory; never fail the assert
                pass

    row = conn.execute("SELECT * FROM atoms WHERE id = ?", (new_id,)).fetchone()
    atom = Atom.from_row(row)
    atom.conflicts = conflicts
    return atom


# ---------------------------------------------------------------------------
# supersede / archive / pin
# ---------------------------------------------------------------------------

def supersede(conn: sqlite3.Connection, *, old_id: str, new_id: str) -> None:
    """Explicitly mark `old_id` as superseded by `new_id`. Both must exist."""
    now = int(time.time())
    with conn:
        if not conn.execute("SELECT 1 FROM atoms WHERE id = ?", (old_id,)).fetchone():
            raise KeyError(f"atom not found: {old_id}")
        if not conn.execute("SELECT 1 FROM atoms WHERE id = ?", (new_id,)).fetchone():
            raise KeyError(f"atom not found: {new_id}")
        conn.execute(
            """UPDATE atoms
                  SET valid_to = COALESCE(valid_to, ?),
                      superseded_by = ?,
                      updated_at = ?
                WHERE id = ?""",
            (now, new_id, now, old_id),
        )


def archive(conn: sqlite3.Connection, atom_id_: str, *, on: bool = True) -> None:
    with conn:
        cur = conn.execute(
            "UPDATE atoms SET archived = ?, updated_at = ? WHERE id = ?",
            (int(on), int(time.time()), atom_id_),
        )
        if cur.rowcount == 0:
            raise KeyError(f"atom not found: {atom_id_}")


def pin(conn: sqlite3.Connection, atom_id_: str, *, on: bool = True) -> None:
    with conn:
        cur = conn.execute(
            "UPDATE atoms SET pinned = ?, updated_at = ? WHERE id = ?",
            (int(on), int(time.time()), atom_id_),
        )
        if cur.rowcount == 0:
            raise KeyError(f"atom not found: {atom_id_}")


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------

@dataclass
class AtomView:
    """Atom + its summaries — the read-side shape callers actually want."""
    atom: Atom
    summaries: dict[int, str] = field(default_factory=dict)


def get_atom(
    conn: sqlite3.Connection, atom_id_: str, *, include_archived: bool = False
) -> AtomView | None:
    row = conn.execute("SELECT * FROM atoms WHERE id = ?", (atom_id_,)).fetchone()
    if not row:
        return None
    if not include_archived and row["archived"]:
        return None
    atom = Atom.from_row(row)
    rows = conn.execute(
        "SELECT resolution, text FROM atom_summaries WHERE atom_id = ?",
        (atom_id_,),
    ).fetchall()
    return AtomView(atom=atom, summaries={r["resolution"]: r["text"] for r in rows})


def query_atoms(
    conn: sqlite3.Connection,
    *,
    type: AtomType | None = None,
    topic_key: str | None = None,
    decision_status: DecisionStatus | None = None,
    decision_class: DecisionClass | None = None,
    repo_id: str | None = None,
    workspace_id: str | None = None,
    pinned: bool | None = None,
    live_only: bool = True,
    include_archived: bool = False,
    limit: int = 50,
    order_by: str = "asserted_at DESC",
) -> list[Atom]:
    """Filtered atom list. Defaults give you the working set: live + unarchived."""
    where: list[str] = []
    params: list[object] = []
    if type is not None:
        where.append("type = ?")
        params.append(type)
    if topic_key is not None:
        where.append("topic_key = ?")
        params.append(topic_key)
    if decision_status is not None:
        where.append("decision_status = ?")
        params.append(decision_status)
    if decision_class is not None:
        # COALESCE so pre-v3 rows that never got classified still answer a
        # constraint query rather than vanishing from it.
        where.append("COALESCE(decision_class, 'constraint') = ?")
        params.append(decision_class)
    if repo_id is not None:
        where.append("repo_id = ?")
        params.append(repo_id)
    if workspace_id is not None:
        where.append("workspace_id = ?")
        params.append(workspace_id)
    if pinned is not None:
        where.append("pinned = ?")
        params.append(int(pinned))
    if live_only:
        where.append("valid_to IS NULL")
    if not include_archived:
        where.append("archived = 0")

    # `order_by` is constrained to a known allow-list to avoid SQL injection.
    allowed_order = {
        "asserted_at DESC", "asserted_at ASC",
        "valid_from DESC", "valid_from ASC",
        "confidence DESC", "type ASC",
    }
    if order_by not in allowed_order:
        raise ValueError(f"order_by must be one of {sorted(allowed_order)}")
    if not isinstance(limit, int) or limit < 1 or limit > 10_000:
        raise ValueError("limit must be in [1, 10000]")

    sql = "SELECT * FROM atoms"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order_by} LIMIT ?"
    params.append(limit)

    return [Atom.from_row(r) for r in conn.execute(sql, params).fetchall()]


def history(conn: sqlite3.Connection, topic_key: str, type_: AtomType) -> list[Atom]:
    """Return all versions (live + historical) of a topic, oldest first."""
    rows = conn.execute(
        """SELECT * FROM atoms
            WHERE topic_key = ? AND type = ?
            ORDER BY valid_from ASC, asserted_at ASC""",
        (topic_key, type_),
    ).fetchall()
    return [Atom.from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

def write_summary(
    conn: sqlite3.Connection, atom_id_: str, resolution: Resolution, text: str
) -> None:
    """Upsert a summary at a given resolution. Validates atom existence."""
    with conn:
        if not conn.execute("SELECT 1 FROM atoms WHERE id = ?", (atom_id_,)).fetchone():
            raise KeyError(f"atom not found: {atom_id_}")
        _upsert_summary(conn, atom_id_, resolution, text)


def _upsert_summary(
    conn: sqlite3.Connection, atom_id_: str, resolution: int, text: str
) -> None:
    if resolution not in (10, 50, 250):
        raise ValueError(f"resolution must be one of (10, 50, 250), got {resolution}")
    token_count = _approx_tokens(text)
    conn.execute(
        """INSERT INTO atom_summaries (atom_id, resolution, text, token_count)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(atom_id, resolution) DO UPDATE SET
             text = excluded.text,
             token_count = excluded.token_count""",
        (atom_id_, resolution, text, token_count),
    )


def _approx_tokens(text: str) -> int:
    # Rough heuristic: 1 token ≈ 4 chars of English. Good enough for budgets;
    # the real embedder will recompute when the embedding layer lands.
    return max(1, (len(text) + 3) // 4)


def topics_in(
    conn: sqlite3.Connection,
    *,
    type_: AtomType | None = None,
    workspace_id: str | None = None,
) -> Iterable[str]:
    """Iterate distinct topic_keys (live atoms only)."""
    sql = "SELECT DISTINCT topic_key FROM live_atoms WHERE 1=1"
    params: list[object] = []
    if type_:
        sql += " AND type = ?"
        params.append(type_)
    if workspace_id:
        sql += " AND workspace_id = ?"
        params.append(workspace_id)
    sql += " ORDER BY topic_key"
    for r in conn.execute(sql, params):
        yield r["topic_key"]
