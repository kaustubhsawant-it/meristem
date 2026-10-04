"""Team sync protocol (SPEC §16) — git-mergeable export/import of the shared
atom store.

`.meristem/atoms.sqlite` is a binary file. Two devs' concurrent commits to it
are a hard git conflict resolvable only by discarding one side wholesale —
not a "team sync protocol", just a coin flip that throws away a teammate's
memory. This module exports the *shared substrate* to a line-oriented,
git-mergeable JSONL file and merges it with a deterministic, order-independent
per-field policy, so a real git merge (or our custom merge driver) never has
to leave `<<<<<<<` markers in it.

Two export modes share every merge policy and row-identity rule below —
`mode = "git-jsonl"` (`export_jsonl`/`import_jsonl`) writes one file; a store
too large to comfortably rewrite/diff/parse whole on every sync instead uses
`mode = "git-jsonl-sharded"` (`export_jsonl_sharded`/`import_jsonl_sharded`),
which partitions the same rows across many small self-contained files keyed
by `shard_of` so a change to one atom only ever touches its one shard.

Shared substrate (exported): atoms, atom_summaries, edges, edge_evidence.
Deliberately NOT exported — per-machine operational state, not knowledge:
  jobs            — local ingest queue
  retrieval_log   — one dev's query history
  session_state   — session bookkeeping
  embedding_ledger— local daily cost cap counter
  candidates      — pending write-path review queue (not memory until accepted)
  repo_state      — `root_path` is an absolute local filesystem path and
                    `repo_id` (cli._repo_id) is a hash of it, so this table
                    can never agree across two clones; syncing it would mint
                    a duplicate row per dev instead of one row per repo.

Row identity (what makes two rows "the same row" across two stores):
  atoms          — `id` (content hash of type+topic_key+claim; SPEC §3, ids.py)
  atom_summaries — `(atom_id, resolution)`
  edges          — `(src_id, dst_id, kind, valid_from)` — NOT `id`, which is a
                   local `INTEGER PRIMARY KEY` autoincrement, meaningless
                   across two independently grown databases.
  edge_evidence  — `(edge natural key, kind, payload)` — has no id of its own.

Merge policy per mutable column — each is commutative, associative and
idempotent, so the merged result never depends on which side is "ours":
  valid_to, superseded_by  — earliest non-null close wins (a row can't un-close)
  liveness_kind/target/pattern — prefer the more precise predicate
                                 (ast > regex > sql > none/absent); one dev
                                 missing a tree-sitter grammar for a language
                                 the other has installed is the real case
                                 this exists for (SPEC §16 tree-sitter pillar)
  liveness_last_ok         — latest wins (freshest positive check)
  pinned, archived         — sticky true (either side's deliberate action holds)
  confirm_by               — earliest horizon wins (the more cautious value)
  valid_in_refs            — union
  created_at               — earliest      updated_at — latest
  everything else on `atoms` — identical by construction (same content hash);
    a deterministic fallback (None-safe min) breaks a genuine divergence
  edges.weight/confidence  — max (strongest evidence seen)
  edges.status             — 'rejected' sticky; else 'live' beats 'suggested'
  edges.valid_to           — earliest non-null close wins
  atom_summaries.text      — closer to the resolution's target word count wins
                             (no timestamp to prefer "freshest"; "longer" isn't
                             "more complete" when the target is a token budget)
  atom_summaries.token_count — always re-derived from the winning text, never
                             copied — it is a pure function of text, not
                             independent state

What this module does NOT do: resolve a genuine disagreement between two
*different* claims on the same topic (e.g. two devs each ran `meristem
decide` offline on the same question and reached different answers). That
is not a merge-mechanics problem — `atoms.assert_fact` already has an
answer for it (auto-supersede + a CONTRADICTS edge, SPEC §14.5). `import_jsonl`
calls `reconcile_topics` after loading rows, which runs that same policy
across whatever the merge produced: closes every topic down to one live
atom and raises CONTRADICTS between the survivor and each closed duplicate,
so the disagreement is surfaced rather than silently decided by whoever's
row sorted first.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field

from . import atoms as atoms_mod
from . import guard

SYNCED_TABLES = ("atoms", "atom_summaries", "edges", "edge_evidence")

# ast beats regex beats sql beats none/absent — SPEC §16's tree-sitter pillar:
# a language whose grammar isn't importable falls back to a regex predicate,
# so if one dev has the grammar and the other doesn't, prefer the precise one.
_LIVENESS_RANK = {"ast": 3, "regex": 2, "sql": 1, "none": 0, None: -1}


# ---------------------------------------------------------------------------
# small null-safe combinators
# ---------------------------------------------------------------------------

def _min_or_none(x, y):
    if x is None:
        return y
    if y is None:
        return x
    return min(x, y)


def _max_or_none(x, y):
    if x is None:
        return y
    if y is None:
        return x
    return max(x, y)


def _stable_pick(x, y):
    """Deterministic, order-independent tiebreak for fields that should
    already be identical (same content hash) but might not be."""
    return min((x, y), key=lambda v: (v is None, v))


# ---------------------------------------------------------------------------
# row <-> dict, keys, sort order
# ---------------------------------------------------------------------------

_log = logging.getLogger("meristem.guard")


def _row_key(table: str, row: dict) -> tuple:
    if table == "atoms":
        return (row["id"],)
    if table == "atom_summaries":
        return (row["atom_id"], row["resolution"])
    if table == "edges":
        return (row["src_id"], row["dst_id"], row["kind"], row["valid_from"])
    if table == "edge_evidence":
        return (
            row["edge_src"], row["edge_dst"], row["edge_kind"], row["edge_valid_from"],
            row["kind"], row["payload"],
        )
    raise ValueError(f"unknown synced table: {table}")


def _sort_key(table: str, key: tuple) -> tuple:
    return (table, tuple("" if k is None else k for k in key))


def _serialize(table: str, key: tuple, row: dict) -> str:
    payload = dict(row)
    payload["_table"] = table
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _collect_rows(conn: sqlite3.Connection) -> list[tuple[str, dict]]:
    """Every row of the shared substrate, as (table, row) pairs — the one read
    path shared by the monolithic and sharded export."""
    rows: list[tuple[str, dict]] = []

    for r in conn.execute("SELECT * FROM atoms"):
        rows.append(("atoms", dict(r)))

    for r in conn.execute("SELECT * FROM atom_summaries"):
        rows.append(("atom_summaries", dict(r)))

    edge_key_by_id: dict[int, tuple] = {}
    for r in conn.execute("SELECT * FROM edges"):
        d = dict(r)
        edge_key_by_id[d["id"]] = (d["src_id"], d["dst_id"], d["kind"], d["valid_from"])
        del d["id"]
        rows.append(("edges", d))

    for r in conn.execute("SELECT * FROM edge_evidence"):
        d = dict(r)
        key = edge_key_by_id.get(d["edge_id"])
        if key is None:
            continue  # orphaned row; FK-enforced so this shouldn't happen — skip defensively
        del d["edge_id"]
        d["edge_src"], d["edge_dst"], d["edge_kind"], d["edge_valid_from"] = key
        rows.append(("edge_evidence", d))

    return rows


def _guard_rows(
    rows: list[tuple[str, dict]], policy: guard.Policy
) -> tuple[list[tuple[str, dict]], list[tuple[str, str]]]:
    """Drop rows whose text trips the trust guard (see guard.py).

    An atom whose topic or any summary carries a finding is withheld whole:
    its summaries, every edge touching it and those edges' evidence go with it
    (an edge to a missing atom would not import). A lone evidence row that
    trips the guard is withheld by itself, charged to its edge's src atom.
    Returns the kept rows and a sorted, de-duplicated list of
    (atom_id, finding_kind) — kinds only, never matched text.
    """
    withheld: set[tuple[str, str]] = set()
    dropped_atoms: set[str] = set()

    def flag(atom_id: str, text: str) -> bool:
        ks = guard.kinds_in(text, policy=policy)
        withheld.update((atom_id, k) for k in ks)
        return bool(ks)

    for table, row in rows:
        if table == "atoms" and flag(row["id"], row["topic_key"] or ""):
            dropped_atoms.add(row["id"])
        elif table == "atom_summaries" and flag(row["atom_id"], row["text"] or ""):
            dropped_atoms.add(row["atom_id"])

    kept: list[tuple[str, dict]] = []
    for table, row in rows:
        if table == "atoms" and row["id"] in dropped_atoms:
            continue
        if table == "atom_summaries" and row["atom_id"] in dropped_atoms:
            continue
        if table == "edges" and (row["src_id"] in dropped_atoms or row["dst_id"] in dropped_atoms):
            continue
        if table == "edge_evidence":
            if row["edge_src"] in dropped_atoms or row["edge_dst"] in dropped_atoms:
                continue
            if flag(row["edge_src"], row["payload"] or ""):
                continue
        kept.append((table, row))
    return kept, sorted(withheld)


def _apply_guard(
    rows: list[tuple[str, dict]],
    policy: guard.Policy | None,
    withheld: list[tuple[str, str]] | None,
) -> list[tuple[str, dict]]:
    policy = policy if policy is not None else guard.Policy()
    if not policy.enabled:
        return rows
    kept, found = _guard_rows(rows, policy)
    if withheld is not None:
        withheld.extend(found)
    elif found:
        # No caller-supplied list: say so on stderr (logging's last-resort
        # handler) rather than dropping rows silently. Ids and kinds only.
        for atom_id, kind in found:
            _log.warning("export withheld %s: contains %s", atom_id, kind)
    return kept


def _render(rows: list[tuple[str, dict]]) -> str:
    """Sorted, one-object-per-line JSON for `rows`. Sorted by (table, natural
    key) so the result is byte-stable across two stores holding identical
    data — git diffs then show real changes only, never reordering noise."""
    lines = [
        ((table,) + _row_key(table, row), _serialize(table, (), row)) for table, row in rows
    ]
    lines.sort(key=lambda t: t[0])
    if not lines:
        return ""
    return "\n".join(s for _, s in lines) + "\n"


def export_jsonl(
    conn: sqlite3.Connection,
    *,
    policy: guard.Policy | None = None,
    withheld: list[tuple[str, str]] | None = None,
) -> str:
    """Dump the shared substrate as one sorted, one-object-per-line JSON file.

    `[sync] mode = "git-jsonl"` (the default). Every export rewrites the whole
    store — simple, and fine at the scale most workspaces reach. For a store
    too large to comfortably re-diff/re-merge/re-parse in full on every sync,
    see `export_jsonl_sharded` (`mode = "git-jsonl-sharded"`).

    The trust guard runs first: atoms whose text carries a secret or personal
    data are withheld (see `_guard_rows`). `withheld`, if given, is extended
    with (atom_id, kind) pairs; otherwise they are logged as warnings.
    `policy` defaults to guard enabled with no allow-patterns.
    """
    return _render(_apply_guard(_collect_rows(conn), policy, withheld))


def shard_of(atom_id: str, prefix_len: int) -> str:
    """Deterministic shard key for an atom id: the leading `prefix_len` hex
    chars of its content hash (the id's fixed-width last 12 chars — ids.py —
    regardless of the type prefix's own length, so this needs no per-type
    knowledge). Mirrors git's own `.git/objects/xx/` fan-out, same reason:
    bound the size of any one file so touching one atom never has to rewrite,
    re-diff or re-merge every atom in the store (SPEC §16)."""
    return atom_id[-12:][:prefix_len]


def _row_shard(table: str, row: dict, prefix_len: int) -> str:
    """Which shard `row` belongs to. `atom_summaries`/`edges`/`edge_evidence`
    ride along with the atom that owns them; an edge belongs to its `src_id`'s
    shard — one deterministic choice, independent of where `dst_id` lands."""
    if table == "atoms":
        return shard_of(row["id"], prefix_len)
    if table == "atom_summaries":
        return shard_of(row["atom_id"], prefix_len)
    if table == "edges":
        return shard_of(row["src_id"], prefix_len)
    if table == "edge_evidence":
        return shard_of(row["edge_src"], prefix_len)
    raise ValueError(f"unknown synced table: {table}")


DEFAULT_SHARD_PREFIX_LEN = 2  # 16**2 = 256 shards — see shard_of()


def export_jsonl_sharded(
    conn: sqlite3.Connection,
    prefix_len: int = DEFAULT_SHARD_PREFIX_LEN,
    *,
    policy: guard.Policy | None = None,
    withheld: list[tuple[str, str]] | None = None,
) -> dict[str, str]:
    """Like `export_jsonl`, but partitions the shared substrate into
    independent shard files keyed by `shard_of` — the incremental/streaming
    export mode (SPEC §16, `[sync] mode = "git-jsonl-sharded"`).

    Each returned shard's text is itself a complete, self-contained,
    byte-stable snapshot in the exact sorted grouped-JSONL format
    `export_jsonl` produces for the whole store — no compaction, log replay or
    cursor state is ever needed, because a shard IS the current state of the
    rows it owns, not a diff against a prior export. That is what bounds the
    cost this mode exists to avoid: changing one atom only ever touches the
    one shard it (or its owning edge/summary) hashes into, so a caller that
    only rewrites shards whose text actually changed keeps git diff, the merge
    driver, and a teammate's `import` on pull all O(shard), not O(store) —
    regardless of how large the rest of the workspace's memory has grown.

    Only shards with at least one row are present in the result — an empty
    shard is never materialized as a file.
    """
    by_shard: dict[str, list[tuple[str, dict]]] = {}
    for table, row in _apply_guard(_collect_rows(conn), policy, withheld):
        by_shard.setdefault(_row_shard(table, row, prefix_len), []).append((table, row))
    return {shard: _render(rows) for shard, rows in by_shard.items()}


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def _parse_grouped(text: str) -> dict[str, dict[tuple, dict]]:
    grouped: dict[str, dict[tuple, dict]] = {t: {} for t in SYNCED_TABLES}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        table = row.pop("_table")
        if table not in grouped:
            continue  # forward-compat: ignore tables a newer writer added
        grouped[table][_row_key(table, row)] = row
    return grouped


# ---------------------------------------------------------------------------
# per-table merge policy (pure — operates on plain dicts)
# ---------------------------------------------------------------------------

def merge_atom_row(a: dict, b: dict) -> dict:
    out = dict(a)

    if a["valid_to"] is None and b["valid_to"] is None:
        out["valid_to"], out["superseded_by"] = None, None
    elif a["valid_to"] is None:
        out["valid_to"], out["superseded_by"] = b["valid_to"], b["superseded_by"]
    elif b["valid_to"] is None or a["valid_to"] <= b["valid_to"]:
        out["valid_to"], out["superseded_by"] = a["valid_to"], a["superseded_by"]
    else:
        out["valid_to"], out["superseded_by"] = b["valid_to"], b["superseded_by"]

    out["liveness_last_ok"] = _max_or_none(a["liveness_last_ok"], b["liveness_last_ok"])
    out["pinned"] = int(bool(a["pinned"]) or bool(b["pinned"]))
    out["archived"] = int(bool(a["archived"]) or bool(b["archived"]))
    out["confirm_by"] = _min_or_none(a["confirm_by"], b["confirm_by"])
    out["created_at"] = min(a["created_at"], b["created_at"])
    out["updated_at"] = max(a["updated_at"], b["updated_at"])

    refs_a = set(json.loads(a["valid_in_refs"]) if a["valid_in_refs"] else [])
    refs_b = set(json.loads(b["valid_in_refs"]) if b["valid_in_refs"] else [])
    out["valid_in_refs"] = json.dumps(sorted(refs_a | refs_b))

    if _LIVENESS_RANK.get(a["liveness_kind"], -1) >= _LIVENESS_RANK.get(b["liveness_kind"], -1):
        out["liveness_kind"] = a["liveness_kind"]
        out["liveness_target"] = a["liveness_target"]
        out["liveness_pattern"] = a["liveness_pattern"]
    else:
        out["liveness_kind"] = b["liveness_kind"]
        out["liveness_target"] = b["liveness_target"]
        out["liveness_pattern"] = b["liveness_pattern"]

    # Fields that should already be identical (same content hash covers
    # type+topic_key+claim) — a deterministic, None-safe fallback in case
    # they genuinely diverge (e.g. valid_from stamped by two independent
    # `now()` calls that ingested the same fact seconds apart).
    for col in (
        "confidence", "repo_id", "workspace_id", "valid_from", "asserted_at",
        "decision_status", "decision_class", "tier", "source_kind",
        "source_ref", "source_lines",
    ):
        if a[col] != b[col]:
            out[col] = _stable_pick(a[col], b[col])

    return out


def _approx_tokens(text: str) -> int:
    # Same heuristic as atoms._approx_tokens/handoff._approx_tokens/
    # router._approx_tokens — each module keeps its own private copy rather
    # than share one, an established convention in this codebase.
    return max(1, (len(text) + 3) // 4)


def merge_summary_row(a: dict, b: dict) -> dict:
    """Regenerable content, but not "pick the longer one": `resolution`
    (10/50/250) is a target *word count* — the content hash of every atom is
    keyed over `summary_50w` (atoms.py), and retrieval spends a token budget
    per resolution assuming it holds. A summary that overshoots the target
    isn't "more complete", it's a broken budget — so the field that actually
    needs merging is `text`, and the merge prefers whichever side's word
    count is closer to the target, not whichever is longer.

    `token_count` is never copied from either side — it is always re-derived
    from the winning `text`, the same way `atoms._upsert_summary` computes it
    fresh on every write rather than accepting it as an independent input.
    """
    target = a["resolution"]
    a_text, b_text = a.get("text") or "", b.get("text") or ""
    a_dist = abs(len(a_text.split()) - target)
    b_dist = abs(len(b_text.split()) - target)
    if a_dist < b_dist:
        winner = a
    elif b_dist < a_dist:
        winner = b
    else:
        winner = a if _stable_pick(a_text, b_text) == a_text else b
    out = dict(winner)
    out["token_count"] = _approx_tokens(out.get("text") or "")
    return out


def merge_edge_row(a: dict, b: dict) -> dict:
    out = dict(a)
    out["weight"] = max(a["weight"], b["weight"])
    out["confidence"] = max(a["confidence"], b["confidence"])
    statuses = {a["status"], b["status"]}
    if "rejected" in statuses:
        out["status"] = "rejected"
    elif "live" in statuses:
        out["status"] = "live"
    else:
        out["status"] = a["status"]
    out["valid_to"] = _min_or_none(a["valid_to"], b["valid_to"])
    out["created_at"] = min(a["created_at"], b["created_at"])
    if a["source"] != b["source"]:
        out["source"] = _stable_pick(a["source"], b["source"])
    return out


# ---------------------------------------------------------------------------
# merge two exported texts (pure — this is what the git merge driver calls)
# ---------------------------------------------------------------------------

_MERGE_FNS = {
    "atoms": merge_atom_row,
    "atom_summaries": merge_summary_row,
    "edges": merge_edge_row,
    "edge_evidence": None,  # identity rows only — no mutable fields to merge
}


def merge_jsonl_texts(ours: str, theirs: str) -> str:
    """Pure, order-independent merge of two exported JSONL texts. Never
    leaves a conflict — every row has a deterministic resolution."""
    a = _parse_grouped(ours)
    b = _parse_grouped(theirs)

    lines: list[tuple[tuple, str]] = []
    for table in SYNCED_TABLES:
        merge_fn = _MERGE_FNS[table]
        ka, kb = a[table], b[table]
        for key in set(ka) | set(kb):
            if key in ka and key in kb:
                row = merge_fn(ka[key], kb[key]) if merge_fn else ka[key]
            else:
                # `key` came from the union of both key sets, so exactly one of
                # these hits — the walrus keeps mypy from widening to Optional.
                row = ka[key] if key in ka else kb[key]
            lines.append((_sort_key(table, key), _serialize(table, key, row)))

    lines.sort(key=lambda t: t[0])
    if not lines:
        return ""
    return "\n".join(s for _, s in lines) + "\n"


# ---------------------------------------------------------------------------
# import into a live sqlite store
# ---------------------------------------------------------------------------

@dataclass
class ImportStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    conflicts: list[tuple[str, str]] = field(default_factory=list)  # (topic_key, kept_atom_id)


def _insert_generic(conn: sqlite3.Connection, table: str, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
        [row[c] for c in cols],
    )


def _update_generic(
    conn: sqlite3.Connection, table: str, row: dict, key_cols: tuple[str, ...]
) -> None:
    set_cols = [c for c in row if c not in key_cols]
    conn.execute(
        f"UPDATE {table} SET {','.join(f'{c}=?' for c in set_cols)} "
        f"WHERE {' AND '.join(f'{c}=?' for c in key_cols)}",
        [row[c] for c in set_cols] + [row[c] for c in key_cols],
    )


def _import_atoms(conn: sqlite3.Connection, rows: dict[tuple, dict], stats: ImportStats) -> None:
    for row in rows.values():
        existing = conn.execute("SELECT * FROM atoms WHERE id = ?", (row["id"],)).fetchone()
        if existing is None:
            _insert_generic(conn, "atoms", row)
            stats.inserted += 1
            continue
        merged = merge_atom_row(dict(existing), row)
        if merged == dict(existing):
            stats.unchanged += 1
        else:
            _update_generic(conn, "atoms", merged, ("id",))
            stats.updated += 1


def _import_summaries(conn: sqlite3.Connection, rows: dict[tuple, dict]) -> None:
    for (atom_id, resolution), row in rows.items():
        existing = conn.execute(
            "SELECT * FROM atom_summaries WHERE atom_id = ? AND resolution = ?",
            (atom_id, resolution),
        ).fetchone()
        if existing is None:
            # atom_summaries has no FK-safe guarantee the atom landed first if
            # a caller ever invokes this out of order; atoms are always
            # imported first by import_jsonl, so this is just defense.
            if conn.execute("SELECT 1 FROM atoms WHERE id = ?", (atom_id,)).fetchone():
                _insert_generic(conn, "atom_summaries", row)
        else:
            merged = merge_summary_row(dict(existing), row)
            if merged != dict(existing):
                _update_generic(conn, "atom_summaries", merged, ("atom_id", "resolution"))


def _import_edges(
    conn: sqlite3.Connection, rows: dict[tuple, dict], stats: ImportStats
) -> dict[tuple, int]:
    edge_id_by_key: dict[tuple, int] = {}
    for key, row in rows.items():
        src_id, dst_id, kind, valid_from = key
        existing = conn.execute(
            "SELECT * FROM edges WHERE src_id=? AND dst_id=? AND kind=? AND valid_from=?",
            key,
        ).fetchone()
        if existing is None:
            if not (
                conn.execute("SELECT 1 FROM atoms WHERE id=?", (src_id,)).fetchone()
                and conn.execute("SELECT 1 FROM atoms WHERE id=?", (dst_id,)).fetchone()
            ):
                continue  # endpoint missing locally even after atom import — skip defensively
            _insert_generic(conn, "edges", row)
            edge_id_by_key[key] = conn.execute(
                "SELECT id FROM edges WHERE src_id=? AND dst_id=? AND kind=? AND valid_from=?", key
            ).fetchone()["id"]
            stats.inserted += 1
        else:
            merged = merge_edge_row(dict(existing), row)
            edge_id_by_key[key] = existing["id"]
            if merged != dict(existing):
                _update_generic(conn, "edges", merged, ("src_id", "dst_id", "kind", "valid_from"))
                stats.updated += 1
            else:
                stats.unchanged += 1
    return edge_id_by_key


def _import_evidence(
    conn: sqlite3.Connection, rows: dict[tuple, dict], edge_id_by_key: dict[tuple, int]
) -> None:
    for row in rows.values():
        edge_key = (row["edge_src"], row["edge_dst"], row["edge_kind"], row["edge_valid_from"])
        edge_id = edge_id_by_key.get(edge_key)
        if edge_id is None:
            continue  # the edge it's evidence for didn't land — skip defensively
        already = conn.execute(
            "SELECT 1 FROM edge_evidence WHERE edge_id=? AND kind=? AND payload=?",
            (edge_id, row["kind"], row["payload"]),
        ).fetchone()
        if already:
            continue
        conn.execute(
            "INSERT INTO edge_evidence (edge_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (edge_id, row["kind"], row["payload"], row["created_at"]),
        )


def reconcile_topics(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Close every topic left with >1 live atom down to one survivor (latest
    `valid_from`, tie-broken by id) and raise a CONTRADICTS edge between the
    survivor and each closed duplicate. Mirrors `atoms.assert_fact`'s own
    conflict detection (SPEC §14.5) — skipped when every atom in the group is
    a NATURAL_EVOLUTION_SOURCES commit/schema_snapshot/manifest re-ingestion,
    since that is drift, not a disagreement.

    Returns (topic_key, kept_atom_id) for every genuine conflict surfaced.
    """
    from . import edges as edges_mod

    conflicts: list[tuple[str, str]] = []
    now = int(time.time())
    dup_groups = conn.execute(
        """SELECT type, topic_key, workspace_id FROM atoms
            WHERE valid_to IS NULL
            GROUP BY type, topic_key, workspace_id
           HAVING COUNT(*) > 1"""
    ).fetchall()

    for g in dup_groups:
        rows = conn.execute(
            """SELECT * FROM atoms
                WHERE type=? AND topic_key=? AND workspace_id=? AND valid_to IS NULL
                ORDER BY valid_from DESC, id ASC""",
            (g["type"], g["topic_key"], g["workspace_id"]),
        ).fetchall()
        keep, rest = rows[0], rows[1:]
        for dup in rest:
            conn.execute(
                "UPDATE atoms SET valid_to=?, superseded_by=?, updated_at=? WHERE id=?",
                (now, keep["id"], now, dup["id"]),
            )
            natural = (
                dup["source_kind"] in atoms_mod.NATURAL_EVOLUTION_SOURCES
                and keep["source_kind"] in atoms_mod.NATURAL_EVOLUTION_SOURCES
            )
            if not natural:
                # Edge is advisory — never fail the reconcile over it.
                with contextlib.suppress(Exception):
                    edges_mod.upsert_edge(
                        conn, src_id=keep["id"], dst_id=dup["id"],
                        kind="CONTRADICTS", source="llm_extract",
                    )
                conflicts.append((g["topic_key"], keep["id"]))
    return conflicts


def import_jsonl(conn: sqlite3.Connection, text: str) -> ImportStats:
    """Merge an exported JSONL text into `conn`. Never destructive — every
    row present locally but absent from `text` is left untouched (import is a
    union, not a replace)."""
    return import_jsonl_sharded(conn, [text])


def import_jsonl_sharded(conn: sqlite3.Connection, texts: list[str]) -> ImportStats:
    """Merge one or more exported JSONL texts into `conn` — one text per shard
    file for `mode = "git-jsonl-sharded"` (`import_jsonl` is just the
    single-text case). Rows from every text are merged together *before*
    `reconcile_topics` runs, once, at the end — not once per text — because
    two atoms sharing a `topic_key` land in different shards by construction
    (the shard key is an atom's own content hash, unrelated to its
    topic_key), so a genuine conflict is only visible once the whole imported
    set is loaded."""
    grouped: dict[str, dict[tuple, dict]] = {t: {} for t in SYNCED_TABLES}
    for text in texts:
        for table, rows in _parse_grouped(text).items():
            grouped[table].update(rows)
    stats = ImportStats()
    with conn:
        _import_atoms(conn, grouped["atoms"], stats)
        _import_summaries(conn, grouped["atom_summaries"])
        edge_id_by_key = _import_edges(conn, grouped["edges"], stats)
        _import_evidence(conn, grouped["edge_evidence"], edge_id_by_key)
        stats.conflicts = reconcile_topics(conn)
    return stats
