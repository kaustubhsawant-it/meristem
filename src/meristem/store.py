"""SQLite store — open + initialize via schema.sql.

Embedding tables (sqlite-vss virtual table) are NOT created here; they are
loaded at runtime by the retrieval layer (see SPEC §7). This module owns only
the relational schema and basic CRUD helpers.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from .paths import schema_sql_text

# schema.sql uses STRICT tables and `unixepoch()`, both added in SQLite 3.38
# (2022-02). Below it, `init_schema` fails with a bare syntax error pointing at
# whichever line the parser gave up on — a message that names neither SQLite nor
# a version, on a machine where nothing the user did was wrong. The bundled
# sqlite is a property of the Python build, so this is a real state to land in
# and not a hypothetical.
MIN_SQLITE_VERSION = (3, 38, 0)


def require_sqlite(version: tuple[int, int, int] | None = None) -> None:
    """Fail loudly, and early, on a SQLite too old for `schema.sql`.

    Raised at connect time rather than checked at init time: `meristem init` is not
    the only entry point, and a read command hitting this deserves the same
    named cause as a write. The CI smoke test prints the version, which fixes
    diagnosing a CI failure but not the user-installs-it case — this is that.
    """
    have = version or tuple(int(p) for p in sqlite3.sqlite_version.split(".")[:3])
    if have < MIN_SQLITE_VERSION:
        want = ".".join(str(p) for p in MIN_SQLITE_VERSION)
        raise RuntimeError(
            f"Meristem needs SQLite >= {want}; this Python is linked against "
            f"{sqlite3.sqlite_version}. schema.sql uses STRICT tables and "
            f"unixepoch(), both introduced in {want}. Upgrade Python, or "
            f"rebuild it against a newer libsqlite3."
        )


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (and create) a Meristem sqlite db.

    Uses Python's default deferred-isolation mode so `with conn:` brackets a
    real transaction. Callers SHOULD wrap multi-statement writes in
    `with conn:` to get atomic supersede semantics.

    Runs additive schema migrations on open (idempotent, guarded) so that
    read-only commands — `route`, `query`, `digest`, the MCP server — never
    hit a column added by a later schema version on a DB created under an
    older one. Previously only `init_schema` (run by `init`/`ingest`) migrated,
    so a pre-`tier` atoms.sqlite would fail with "no such column: tier".
    """
    require_sqlite()
    if not isinstance(db_path, str) or db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> int:
    """Apply schema.sql idempotently. Returns the schema_version after apply.

    Migrations run BEFORE the schema script: schema.sql creates indexes/views
    that reference columns added by later versions (e.g. `idx_atoms_tier` on
    `atoms.tier`). On a DB created under an older schema, `CREATE TABLE IF NOT
    EXISTS atoms` is a no-op so the column is absent, and those dependent
    objects would fail ("no such column: tier") unless the column is ALTERed in
    first. On a fresh DB the migrations are skipped (no tables yet) and the
    script creates everything with current columns.
    """
    _migrate(conn)
    conn.executescript(schema_sql_text())
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent additive migrations for DBs created under an older schema.

    Runs ahead of schema.sql so columns exist before the script's dependent
    indexes/views are (re)created. Each step is guarded on the target table
    existing and on the column/constraint being absent, so it is a safe no-op
    on both a fresh DB and an already-migrated one.
    """
    if not _table_exists(conn, "atoms"):
        return  # fresh DB — schema.sql creates everything with current columns
    # FIRST: a dangling view blocks `ALTER TABLE` outright — SQLite refuses the
    # rename with "error in view live_edges: no such table: main._edges_old".
    # So a store damaged by an older build cannot be migrated *at all* until the
    # view is healed, which would have wedged those stores on the old schema
    # forever. Repair, then migrate.
    repair_dangling_views(conn)
    repair_dangling_fks(conn)
    _migrate_v2_tier(conn)
    _migrate_v2_edges_rolls_up(conn)  # repairs again internally: its rebuild re-breaks views
    _migrate_v3_decision_class(conn)
    _migrate_v4_retrieval_relevance(conn)
    _migrate_v5_capture(conn)
    _migrate_v6_ingest_notes(conn)
    _migrate_v7_hook_heartbeat(conn)


def _migrate_v4_retrieval_relevance(conn: sqlite3.Connection) -> None:
    """v4: `retrieval_log` gains the relevance columns.

    Purely additive ADD COLUMNs on an audit table — no backfill is possible or
    attempted. Rows written before v4 keep NULL relevances, and that NULL is
    meaningful: those turns were served with no relevance gate at all, so they
    must not be mistaken for turns that scored zero. `tools/replay_retrieval.py`
    exists precisely because the historical rows cannot be scored in place.
    """
    if not _table_exists(conn, "retrieval_log"):
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(retrieval_log)")}
    additions = {
        "relevances": "TEXT",
        "top_relevance": "REAL",
        "n_suppressed": "INTEGER",
        "min_relevance": "REAL",
    }
    missing = {c: t for c, t in additions.items() if c not in cols}
    if not missing:
        return
    with conn:
        for col, decl in missing.items():
            conn.execute(f"ALTER TABLE retrieval_log ADD COLUMN {col} {decl}")


def _migrate_v5_capture(conn: sqlite3.Connection) -> None:
    """v5: `atoms.confirm_by` — the reconfirmation horizon (SPEC §18).

    Additive. Existing atoms get NULL, which is correct rather than merely
    convenient: every atom already in a store came from an ingester or from
    `meristem decide`, and both are code-anchored, so liveness already covers them.
    A horizon only means something for a fact no predicate can check.

    The `candidates` table is created here as well as in schema.sql. It is
    tempting to leave it to the script — `CREATE TABLE IF NOT EXISTS` is
    idempotent — but schema.sql only runs on `init`/`ingest`/`migrate`, while
    `connect()` runs the migrations on *every* open. Without this, `meristem
    capture` and `propose_fact` raise "no such table: candidates" on every
    pre-v5 workspace, which means the Stop hook fails silently on exactly the
    established projects this feature exists to serve.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(atoms)")}
    if "confirm_by" not in cols:
        with conn:
            conn.execute("ALTER TABLE atoms ADD COLUMN confirm_by INTEGER")
    if not _table_exists(conn, "candidates"):
        # Pulled from schema.sql rather than duplicated, so the two definitions
        # cannot drift.
        ddl = {
            m.group(1): m.group(0) for m in _TABLE_DDL_RE.finditer(schema_sql_text())
        }
        stmt = ddl.get("candidates")
        if stmt:
            with conn:
                conn.execute(stmt)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_candidates_status"
                    " ON candidates(status, score DESC)"
                )


def _migrate_v2_tier(conn: sqlite3.Connection) -> None:
    """v2 (SPEC §14.1): add `atoms.tier`. SQLite ADD COLUMN can't carry a CHECK,
    but the NOT NULL DEFAULT backfills existing rows to the leaf tier."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(atoms)")}
    if "tier" not in cols:
        with conn:
            conn.execute(
                "ALTER TABLE atoms ADD COLUMN tier TEXT NOT NULL DEFAULT 'symbol'"
            )


_VIEW_DDL_RE = re.compile(
    r"CREATE\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+AS.*?;",
    re.IGNORECASE | re.DOTALL,
)
_VIEW_REF_RE = re.compile(r'\bFROM\s+"?([A-Za-z_]\w*)"?', re.IGNORECASE)
_TABLE_DDL_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(.*?\)\s*STRICT\s*;",
    re.IGNORECASE | re.DOTALL,
)
_FK_REF_RE = re.compile(r'\bREFERENCES\s+"?([A-Za-z_]\w*)"?', re.IGNORECASE)


def _relations(conn: sqlite3.Connection) -> set[str]:
    return {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }


def repair_dangling_fks(conn: sqlite3.Connection) -> list[str]:
    """Rebuild tables whose FOREIGN KEY points at a table that no longer exists.

    The v2 edges migration renames `edges` to `_edges_old` before rebuilding it.
    SQLite rewrites *every* reference to the renamed table — including the
    foreign key in `edge_evidence`, which becomes
    ``REFERENCES "_edges_old"(id)``. Dropping `_edges_old` then leaves that FK
    pointing at nothing, so any INSERT into `edge_evidence` fails with
    "no such table: main._edges_old".

    The migration's own comment claims it kept those FKs valid by preserving the
    id sequence. It preserved the ids; what it could not preserve was the target
    *name*. The practical effect on a real workspace was that every edge carrying
    evidence — which is every edge the discovery pass creates — failed to insert,
    so the graph could not be built at all.

    A FK cannot be altered in place, so the table is rebuilt from schema.sql and
    its rows copied across on the columns the two definitions share.
    """
    # A dangling view blocks every ALTER TABLE in the database, including the
    # rebuild below — and the migration that breaks FKs breaks views in the same
    # stroke, so the two are always damaged together. Heal views first so this
    # function is safe to call on its own, not only via `_migrate`.
    repair_dangling_views(conn)
    relations = _relations(conn)
    ddl = {m.group(1): m.group(0) for m in _TABLE_DDL_RE.finditer(schema_sql_text())}
    broken = [
        r["name"]
        for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")
        if r["sql"]
        and r["name"] in ddl
        and any(ref not in relations for ref in set(_FK_REF_RE.findall(r["sql"])))
    ]
    if not broken:
        return []

    fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")  # no-op inside a txn, so it sits outside
    repaired: list[str] = []
    try:
        for name in broken:
            old_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({name})")}
            tmp = f"_repair_{name}"
            with conn:
                conn.execute(f"ALTER TABLE {name} RENAME TO {tmp}")
                conn.execute(ddl[name])
                new_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({name})")]
                shared = [c for c in new_cols if c in old_cols]
                if shared:
                    cols = ", ".join(shared)
                    conn.execute(f"INSERT INTO {name} ({cols}) SELECT {cols} FROM {tmp}")
                conn.execute(f"DROP TABLE {tmp}")
            repaired.append(name)
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if fk_on else 'OFF'}")
    return repaired


def repair_dangling_views(conn: sqlite3.Connection) -> list[str]:
    """Recreate views whose backing table was renamed out from under them.

    `ALTER TABLE ... RENAME TO` rewrites references inside dependent views —
    that is SQLite doing the helpful thing. But `_migrate_v2_edges_rolls_up`
    renames `edges` to `_edges_old`, rebuilds `edges`, and then DROPs
    `_edges_old`. The rename had already rewritten `live_edges` to select from
    `_edges_old`, so the drop leaves the view pointing at nothing.

    The damage is silent and permanent: `SELECT ... FROM live_edges` raises
    "no such table: main._edges_old" forever after. It was found on a real
    workspace where it had disabled `doctor`'s `edges.density` check — the one
    check that would have reported the missing graph. `CREATE VIEW IF NOT
    EXISTS` in schema.sql cannot heal it, because the broken view *does* exist.

    Views are derived objects with their definitions in schema.sql, so dropping
    and recreating a broken one loses nothing. Returns the names repaired.
    """
    tables = _relations(conn)
    broken: list[str] = []
    for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='view'"):
        sql = row["sql"] or ""
        refs = set(_VIEW_REF_RE.findall(sql))
        if any(ref not in tables for ref in refs):
            broken.append(row["name"])
    if not broken:
        return []

    ddl = {m.group(1): m.group(0) for m in _VIEW_DDL_RE.finditer(schema_sql_text())}
    repaired: list[str] = []
    with conn:
        for name in broken:
            if name not in ddl:
                continue  # not ours to recreate; leave it rather than destroy it
            conn.execute(f"DROP VIEW IF EXISTS {name}")
            conn.execute(ddl[name])
            repaired.append(name)
    return repaired


def _migrate_v3_decision_class(conn: sqlite3.Connection) -> None:
    """v3: add `atoms.decision_class` and classify the decisions already stored.

    Additive ADD COLUMN, so no table rebuild and no row is touched outside the
    backfill UPDATE. The backfill is the interesting part: existing stores hold
    one `decision` atom per git commit (topic_key `commit:<sha>`, minted by the
    git ingester) mixed in with any genuine constraints, and the two are only
    distinguishable by provenance. `source_kind='commit'` is the reliable
    discriminator — the git ingester is the only producer that sets it — with
    the topic_key prefix as a belt-and-braces fallback for rows written before
    provenance was consistent.

    Anything else is left as 'constraint': a human or agent asserted it
    deliberately, which is exactly what a constraint is. Being wrong in that
    direction is the safe one — a mislabelled constraint is merely noisy, while
    a mislabelled change record would hide a real constraint from retrieval.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(atoms)")}
    if "decision_class" in cols:
        return
    with conn:
        conn.execute("ALTER TABLE atoms ADD COLUMN decision_class TEXT")
        # The backfill reads three other columns, and this migration runs on
        # every `store.connect()` — including against hand-built or partial
        # `atoms` tables. A missing column here would raise inside connect() and
        # break every read command, so classify only what we can actually see.
        # Adding the column is the part that must always happen; classifying is
        # best-effort and re-runnable.
        if {"type", "source_kind", "topic_key"} <= cols:
            conn.execute(
                """UPDATE atoms
                      SET decision_class = CASE
                            WHEN source_kind = 'commit' OR topic_key LIKE 'commit:%'
                              THEN 'change'
                            ELSE 'constraint'
                          END
                    WHERE type = 'decision' AND decision_class IS NULL"""
            )


def _migrate_v2_edges_rolls_up(conn: sqlite3.Connection) -> None:
    """v2 (SPEC §14.1): widen the `edges.kind` CHECK to accept `ROLLS_UP`.

    A column CHECK can't be altered in place, so a pre-v2 `edges` table silently
    rejects the new kind — a module-atom ingest would fail with a CHECK
    violation when writing roll-up edges. The supported fix is rebuild:
    rename → create-widened → copy → drop → recreate indexes, preserving every
    row and the id sequence (so `edge_evidence.edge_id` FKs stay valid). FKs are
    toggled off for the swap and restored after; the toggle sits OUTSIDE any
    transaction because `PRAGMA foreign_keys` is a no-op inside one.

    No-op when `edges` is absent (fresh DB) or its DDL already lists ROLLS_UP.
    """
    if not _table_exists(conn, "edges"):
        return
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'"
    ).fetchone()
    if row and row[0] and "ROLLS_UP" in row[0]:
        return  # already widened (fresh schema or prior migration)

    fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            # SQL is left-aligned in the heredoc (whitespace is insignificant to
            # SQLite) so the index/CHECK lines stay under the 100-col limit.
            conn.executescript(
                """
ALTER TABLE edges RENAME TO _edges_old;

CREATE TABLE edges (
  id          INTEGER PRIMARY KEY,
  src_id      TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  dst_id      TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK(kind IN (
                'MIRRORS','IMPLEMENTS','SUPERSEDES','CONTRADICTS',
                'LOCATED_IN','REFERENCES','DEPENDS_ON','OWNS','BLOCKS',
                'CO_CHANGED','DERIVED_FROM','ROLLS_UP'
              )),
  directed    INTEGER NOT NULL DEFAULT 0,
  weight      REAL NOT NULL DEFAULT 1.0 CHECK(weight BETWEEN 0.0 AND 1.0),
  confidence  REAL NOT NULL DEFAULT 1.0,
  valid_from  INTEGER NOT NULL,
  valid_to    INTEGER,
  source      TEXT NOT NULL CHECK(source IN (
                'shared_ref','embed_sim','commit_couple','llm_extract',
                'git_blame','manual','schema_diff'
              )),
  status      TEXT NOT NULL DEFAULT 'live'
                CHECK(status IN ('live','suggested','rejected')),
  created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
  UNIQUE(src_id, dst_id, kind, valid_from)
) STRICT;

INSERT INTO edges
  SELECT id, src_id, dst_id, kind, directed, weight, confidence,
         valid_from, valid_to, source, status, created_at
    FROM _edges_old;

DROP TABLE _edges_old;

CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src_id, kind) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst_id, kind) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind, weight DESC) WHERE valid_to IS NULL;
"""
            )
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if fk_on else 'OFF'}")
    # The rename above rewrote every dependent view to point at `_edges_old`,
    # which the script then dropped. Heal them in the same call rather than
    # leaving the store broken until something happens to reopen it. The same
    # rename also rewrote edge_evidence's FK to point at _edges_old.
    repair_dangling_views(conn)
    repair_dangling_fks(conn)


def _migrate_v6_ingest_notes(conn: sqlite3.Connection) -> None:
    """v6: `repo_state.last_atoms_skipped`/`.last_ingest_notes`.

    Additive ADD COLUMNs. Existing rows get 0/NULL, correctly read as "no notes
    recorded for this repo yet" rather than "last ingest was clean" — `doctor`'s
    `ingest.notes` check only warns on a nonzero/non-null value, so a
    pre-v6 row stays silent until the next `ingest` actually populates it.
    """
    if not _table_exists(conn, "repo_state"):
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(repo_state)")}
    additions = {
        "last_atoms_skipped": "INTEGER NOT NULL DEFAULT 0",
        "last_ingest_notes": "TEXT",
    }
    missing = {c: t for c, t in additions.items() if c not in cols}
    if not missing:
        return
    with conn:
        for col, decl in missing.items():
            conn.execute(f"ALTER TABLE repo_state ADD COLUMN {col} {decl}")


def _migrate_v7_hook_heartbeat(conn: sqlite3.Connection) -> None:
    """v7: `hook_heartbeat` — per-machine proof a Claude Code hook process ran.

    Same reasoning as `_migrate_v5_capture`'s `candidates` table: `connect()`
    runs on every open, while `init_schema` (via `init`/`ingest`/`migrate`)
    only runs on some. Without this, a pre-v7 workspace that upgrades its CLI
    gets "no such table: hook_heartbeat" from the very hook process this
    table exists to make observable -- the same silent-death failure mode
    this table was built to end.
    """
    if _table_exists(conn, "hook_heartbeat"):
        return
    ddl = {m.group(1): m.group(0) for m in _TABLE_DDL_RE.finditer(schema_sql_text())}
    stmt = ddl.get("hook_heartbeat")
    if stmt:
        with conn:
            conn.execute(stmt)


def register_repo(
    conn: sqlite3.Connection,
    repo_id: str,
    root_path: str,
    workspace_id: str = "default",
) -> None:
    conn.execute(
        """INSERT INTO repo_state (repo_id, workspace_id, root_path)
           VALUES (?, ?, ?)
           ON CONFLICT(repo_id) DO UPDATE SET root_path = excluded.root_path""",
        (repo_id, workspace_id, root_path),
    )


def record_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    started_at: int,
    ended_at: int | None = None,
    ended_reason: str | None = None,
    context_pct_end: float | None = None,
    handoff_path: str | None = None,
    branch: str | None = None,
    last_commit_sha: str | None = None,
) -> None:
    """Record one session in `session_state`, keyed by session_id.

    `session_state` was declared in schema v1 and, until now, written by
    nothing at all — every store on every machine read 0 rows, while the
    project's own tracking recorded "retrieval_log/session_state populated"
    as met. Dead schema is not harmless here: `schema.sql` itself reasons about
    "`retrieval_log`/`session_state` both read 0" as the signal that the
    ambient layer is dead, so a table nothing writes makes that diagnosis
    permanently ambiguous.

    The data was never missing — it was going to `.meristem/handoffs/*.md`
    instead, whose front-matter carries exactly these columns. This is the
    same fact written where it can be queried: how many sessions, how they
    ended, at what context percentage. That is also the per-session halt
    telemetry that was otherwise missing.

    Upsert rather than insert: a session may be handed off more than once
    (a mid-session `meristem handoff`, then a pre-compact hook), and the
    later write is the more complete one.
    """
    conn.execute(
        """INSERT INTO session_state
               (session_id, started_at, ended_at, ended_reason,
                context_pct_end, handoff_path, branch, last_commit_sha)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
                ended_at        = excluded.ended_at,
                ended_reason    = excluded.ended_reason,
                context_pct_end = excluded.context_pct_end,
                handoff_path    = excluded.handoff_path,
                branch          = excluded.branch,
                last_commit_sha = excluded.last_commit_sha""",
        (
            session_id, started_at, ended_at, ended_reason,
            context_pct_end, handoff_path, branch, last_commit_sha,
        ),
    )


def mark_indexed(
    conn: sqlite3.Connection,
    repo_id: str,
    sha: str | None,
    branch: str | None,
    *,
    atoms_skipped: int = 0,
    notes: list[str] | None = None,
) -> None:
    conn.execute(
        """UPDATE repo_state
              SET last_indexed_sha    = ?,
                  last_indexed_at     = ?,
                  last_branch         = ?,
                  last_atoms_skipped  = ?,
                  last_ingest_notes   = ?
            WHERE repo_id = ?""",
        (
            sha, int(time.time()), branch, atoms_skipped,
            json.dumps(notes) if notes else None, repo_id,
        ),
    )


def atom_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM live_atoms").fetchone()
    return int(row["c"]) if row else 0


def repo_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM repo_state ORDER BY repo_id"))


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0
