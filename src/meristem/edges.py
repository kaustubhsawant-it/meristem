"""Typed edges over atoms (SPEC §4).

Eleven edge kinds, bi-temporal validity, status ∈ {live, suggested, rejected},
weighted [0,1]. Edges may be symmetric (`directed=0`) — for symmetric kinds
we canonicalize (src_id, dst_id) so we store exactly one row regardless of
insert order.

Public API:
    upsert_edge(...)        — insert or refresh a live edge (idempotent)
    expire_edge(...)        — bi-temporal close (valid_to)
    reject_edge(...)        — flip suggested→rejected
    add_evidence(...)       — append a provenance breadcrumb
    graph_neighbors(...)    — BFS over live edges, kind-filtered
    list_edges(...)         — filterable listing
"""

from __future__ import annotations

import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

EdgeKind = Literal[
    "MIRRORS", "IMPLEMENTS", "SUPERSEDES", "CONTRADICTS",
    "LOCATED_IN", "REFERENCES", "DEPENDS_ON", "OWNS", "BLOCKS",
    "CO_CHANGED", "DERIVED_FROM", "ROLLS_UP",
]
EdgeSource = Literal[
    "shared_ref", "embed_sim", "commit_couple", "llm_extract",
    "git_blame", "manual", "schema_diff",
]
EdgeStatus = Literal["live", "suggested", "rejected"]

# Edges that are inherently symmetric — we canonicalize endpoints to dedupe.
# MIRRORS is here too: discovery._rule_mirrors emits pairs via combinations()
# over atoms ordered by id, so which side lands in src vs dst is an accident
# of insertion order, not a meaningful direction (fixed 2026-08-11 — was
# previously in DIRECTED_KINDS, which silently dropped the reverse-direction
# hop in retrieval._load_edges; see SPEC §4 correction note).
SYMMETRIC_KINDS: frozenset[str] = frozenset({"CONTRADICTS", "CO_CHANGED", "MIRRORS"})

# Edges whose source-side is directional and ordered.
# ROLLS_UP is directed child→parent (a file/symbol atom rolls up into its module).
DIRECTED_KINDS: frozenset[str] = frozenset({
    "SUPERSEDES", "OWNS", "BLOCKS", "DERIVED_FROM", "IMPLEMENTS", "LOCATED_IN",
    "REFERENCES", "DEPENDS_ON", "ROLLS_UP",
})

# Directed edges whose *reverse* direction is still worth walking at retrieval
# time. BLOCKS is stored decision→code and must stay that way (a decision
# constrains code, never the reverse, and `link_blocks` depends on the
# direction) — but the question the edge exists to answer, "what closed
# decisions constrain the file I am about to touch?", seeds on the CODE and
# needs the hop backward.
#
# Without this, `link_blocks`'s own contract — "makes a constraint reachable by
# graph traversal from the code it governs, so PPR surfaces it when work
# approaches those paths" — did not hold: `retrieval._load_edges` added only
# the src→dst adjacency for directed kinds, so a decision whose text shared no
# vocabulary with the query was unreachable from the very code it blocks. The
# synthetic eval corpus scored 3/3 on policy questions anyway, because its
# decision text happened to share a word stem with each query — a lexical seed
# hit masquerading as a graph hop.
#
# Deliberately NOT solved by moving BLOCKS into SYMMETRIC_KINDS: that set also
# canonicalizes endpoints on write (`dst < src` swap), which would erase the
# stored direction. Same class of bug as MIRRORS-in-DIRECTED_KINDS, 2026-08-11.
REVERSE_TRAVERSABLE_KINDS: frozenset[str] = frozenset({"BLOCKS"})


@dataclass
class Edge:
    id: int
    src_id: str
    dst_id: str
    kind: str
    directed: bool
    weight: float
    confidence: float
    valid_from: int
    valid_to: int | None
    source: str
    status: str
    created_at: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Edge:
        return cls(
            id=r["id"],
            src_id=r["src_id"],
            dst_id=r["dst_id"],
            kind=r["kind"],
            directed=bool(r["directed"]),
            weight=r["weight"],
            confidence=r["confidence"],
            valid_from=r["valid_from"],
            valid_to=r["valid_to"],
            source=r["source"],
            status=r["status"],
            created_at=r["created_at"],
        )


def _canonical_endpoints(src: str, dst: str, kind: str) -> tuple[str, str]:
    """For symmetric kinds, sort endpoints so (a→b) and (b→a) collapse."""
    if kind in SYMMETRIC_KINDS and dst < src:
        return dst, src
    return src, dst


# ---------------------------------------------------------------------------
# upsert / expire / reject
# ---------------------------------------------------------------------------

def upsert_edge(
    conn: sqlite3.Connection,
    *,
    src_id: str,
    dst_id: str,
    kind: EdgeKind,
    source: EdgeSource,
    weight: float = 1.0,
    confidence: float = 1.0,
    valid_from: int | None = None,
    status: EdgeStatus = "live",
    evidence: list[tuple[str, str]] | None = None,
) -> Edge:
    """Insert (or refresh) a live edge.

    If a live edge already exists with the same (src, dst, kind), we refresh
    its weight + confidence + status (max-merge weight/confidence) and append
    any new evidence rows — never re-insert a duplicate. Endpoints are
    canonicalized for symmetric kinds.
    """
    if src_id == dst_id:
        raise ValueError("self-edges are not allowed")
    if not 0.0 <= weight <= 1.0:
        raise ValueError(f"weight must be in [0,1], got {weight}")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0,1], got {confidence}")

    src_id, dst_id = _canonical_endpoints(src_id, dst_id, kind)
    now = int(time.time())
    vf = valid_from if valid_from is not None else now
    directed = 0 if kind in SYMMETRIC_KINDS else 1

    with conn:
        for endpoint in (src_id, dst_id):
            if not conn.execute(
                "SELECT 1 FROM atoms WHERE id = ?", (endpoint,)
            ).fetchone():
                raise KeyError(f"atom not found: {endpoint}")

        existing = conn.execute(
            """SELECT * FROM edges
                WHERE src_id = ? AND dst_id = ? AND kind = ?
                  AND valid_to IS NULL
                ORDER BY id DESC LIMIT 1""",
            (src_id, dst_id, kind),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE edges
                      SET weight = MAX(weight, ?),
                          confidence = MAX(confidence, ?),
                          status = ?
                    WHERE id = ?""",
                (weight, confidence, status, existing["id"]),
            )
            edge_id = existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO edges (
                    src_id, dst_id, kind, directed, weight, confidence,
                    valid_from, valid_to, source, status, created_at
                ) VALUES (?,?,?,?,?,?, ?,?, ?,?, ?)""",
                (
                    src_id, dst_id, kind, directed, weight, confidence,
                    vf, None, source, status, now,
                ),
            )
            edge_id = cur.lastrowid

        if evidence:
            for ekind, payload in evidence:
                conn.execute(
                    """INSERT INTO edge_evidence (edge_id, kind, payload)
                       VALUES (?, ?, ?)""",
                    (edge_id, ekind, payload),
                )

    row = conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
    return Edge.from_row(row)


def link_blocks(conn: sqlite3.Connection, atom_id: str, prefixes: list[str]) -> int:
    """BLOCKS-link `atom_id` (a decision) to every live atom under each path
    prefix in `prefixes`. Returns the count linked.

    Makes a constraint reachable by graph traversal from the code it governs,
    so PPR surfaces it when work approaches those paths — the connection
    `meristem decide --blocks` and the MCP `assert_fact` write path both need,
    factored out so a future change to how BLOCKS gets derived only has one
    place to happen rather than the two call sites drifting independently.
    """
    linked = 0
    for prefix in prefixes:
        rows = conn.execute(
            """SELECT id FROM live_atoms
                WHERE id != ? AND source_ref IS NOT NULL
                  AND (source_ref = ? OR source_ref LIKE ?)""",
            (atom_id, prefix, f"{prefix}%"),
        ).fetchall()
        for r in rows:
            try:
                upsert_edge(
                    conn, src_id=atom_id, dst_id=r["id"], kind="BLOCKS",
                    source="manual", weight=0.9,
                    evidence=[("file_path", prefix)],
                )
                linked += 1
            except (KeyError, ValueError):
                continue
    return linked


def expire_edge(conn: sqlite3.Connection, edge_id: int) -> None:
    """Close an edge's bi-temporal window. Idempotent on already-expired rows."""
    with conn:
        cur = conn.execute(
            """UPDATE edges
                  SET valid_to = COALESCE(valid_to, ?)
                WHERE id = ?""",
            (int(time.time()), edge_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"edge not found: {edge_id}")


def reject_edge(conn: sqlite3.Connection, edge_id: int) -> None:
    with conn:
        cur = conn.execute(
            "UPDATE edges SET status = 'rejected' WHERE id = ?", (edge_id,)
        )
        if cur.rowcount == 0:
            raise KeyError(f"edge not found: {edge_id}")


def add_evidence(
    conn: sqlite3.Connection, edge_id: int, *, kind: str, payload: str
) -> None:
    with conn:
        if not conn.execute("SELECT 1 FROM edges WHERE id = ?", (edge_id,)).fetchone():
            raise KeyError(f"edge not found: {edge_id}")
        conn.execute(
            "INSERT INTO edge_evidence (edge_id, kind, payload) VALUES (?, ?, ?)",
            (edge_id, kind, payload),
        )


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------

def list_edges(
    conn: sqlite3.Connection,
    *,
    atom_id: str | None = None,
    kind: EdgeKind | None = None,
    status: EdgeStatus | None = "live",
    live_only: bool = True,
    limit: int = 200,
) -> list[Edge]:
    where: list[str] = []
    params: list[object] = []
    if atom_id is not None:
        where.append("(src_id = ? OR dst_id = ?)")
        params += [atom_id, atom_id]
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if live_only:
        where.append("valid_to IS NULL")
    sql = "SELECT * FROM edges"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY weight DESC, id DESC LIMIT ?"
    params.append(limit)
    return [Edge.from_row(r) for r in conn.execute(sql, params).fetchall()]


@dataclass
class Hop:
    atom_id: str
    depth: int
    via_edge: int | None       # edge_id taken to arrive (None for seed)
    via_kind: str | None
    weight_acc: float          # product of weights along the path


def graph_neighbors(
    conn: sqlite3.Connection,
    seed_id: str,
    *,
    hops: int = 1,
    kinds: list[EdgeKind] | None = None,
    min_weight: float = 0.0,
    max_results: int = 200,
) -> list[Hop]:
    """BFS over live edges from `seed_id`. Symmetric kinds traverse both ways;
    directional kinds traverse src→dst only. Results exclude the seed.

    Returns hops in BFS order. Cycles are avoided per-node (first reach wins).
    """
    if hops < 1:
        raise ValueError("hops must be >= 1")
    if not conn.execute("SELECT 1 FROM atoms WHERE id = ?", (seed_id,)).fetchone():
        raise KeyError(f"atom not found: {seed_id}")

    kind_filter = ""
    params: list[object] = []
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        kind_filter = f" AND kind IN ({placeholders})"
        params.extend(kinds)

    visited: dict[str, Hop] = {seed_id: Hop(seed_id, 0, None, None, 1.0)}
    queue: deque[str] = deque([seed_id])
    results: list[Hop] = []

    while queue and len(results) < max_results:
        cur = queue.popleft()
        cur_hop = visited[cur]
        if cur_hop.depth >= hops:
            continue

        # Outgoing (directional) AND symmetric-from-either-side
        sql = (
            "SELECT * FROM edges "
            "WHERE valid_to IS NULL AND status = 'live' "
            "  AND weight >= ? "
            "  AND (src_id = ? OR (directed = 0 AND dst_id = ?))"
            + kind_filter
        )
        rows = conn.execute(sql, [min_weight, cur, cur, *params]).fetchall()
        for r in rows:
            nxt = r["dst_id"] if r["src_id"] == cur else r["src_id"]
            if nxt in visited:
                continue
            hop = Hop(
                atom_id=nxt,
                depth=cur_hop.depth + 1,
                via_edge=r["id"],
                via_kind=r["kind"],
                weight_acc=cur_hop.weight_acc * r["weight"],
            )
            visited[nxt] = hop
            results.append(hop)
            queue.append(nxt)
            if len(results) >= max_results:
                break

    return results
