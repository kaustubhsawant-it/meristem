-- Meristem atom store schema
-- SQLite 3.38+ (for STRICT tables and JSONB)
-- Vector extension: sqlite-vss (loaded at runtime by meristem CLI)
--
-- Versioned via schema_version table. Migrations are inline in store.py (_migrate_vN_* functions).

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
) STRICT;

INSERT OR IGNORE INTO schema_version (version, applied_at)
  VALUES (1, unixepoch());
-- v2 (2026-05-27): hierarchical atoms (SPEC §14.1) — atoms.tier + ROLLS_UP edge.
INSERT OR IGNORE INTO schema_version (version, applied_at)
  VALUES (2, unixepoch());
-- v3 (2026-07-25): atoms.decision_class — separates constraints from change
-- records. Both were type='decision', so "what decisions constrain this goal?"
-- answered with commit subject lines.
INSERT OR IGNORE INTO schema_version (version, applied_at)
  VALUES (3, unixepoch());
-- v4 (2026-08-05): retrieval_log records relevance. Retrieval computed a PPR
-- score, discarded it at the router boundary, and logged only which atoms were
-- injected -- so there was no way to ask whether an injection was any good, and
-- no baseline against which to judge a change to retrieval. The log now stores
-- the query-atom cosines, the best one seen, and how many atoms the gate
-- dropped, which turns it from an audit trail into a regression corpus.
INSERT OR IGNORE INTO schema_version (version, applied_at)
  VALUES (4, unixepoch());
-- v5 (2026-08-05): the write path. `candidates` queues facts extracted from
-- what the user said, for review before they become atoms; `atoms.confirm_by`
-- gives a predicate-less fact (a domain rule no regex can check) an expiry it
-- must be reconfirmed against. Until v5 the only non-derivable knowledge in the
-- substrate was whatever someone stopped and typed by hand -- two atoms in ten
-- weeks on a real workspace, against 317 minted automatically from git.
INSERT OR IGNORE INTO schema_version (version, applied_at)
  VALUES (5, unixepoch());
-- v6 (2026-08-12): repo_state.last_atoms_skipped/last_ingest_notes. Adapter
-- `.notes`/`.atoms_skipped` (a swallowed TOMLDecodeError, a capped adapter, a
-- lookback fallback) existed only as IngestResult fields, printed once to
-- whoever's terminal ran `ingest` and gone -- a malformed config file just
-- showed a lower atom count with zero explanation, and `doctor` (a later,
-- separate invocation) had no way to see any of it. Persisting the last run's
-- summary here is what lets `doctor` surface it after the fact.
INSERT OR IGNORE INTO schema_version (version, applied_at)
  VALUES (6, unixepoch());
-- v7 (2026-08-19): hook_heartbeat — per-machine proof that a Claude Code hook
-- process actually ran. The ambient layer (SessionStart digest, per-turn
-- router, PostToolUse watcher, Stop capture, PreCompact handoff) was wired
-- only as external ~/.claude/hooks/*.js scripts, invisible to this repo, and
-- went dead for 8 days after the DLMS->Meristem rename with zero signal
-- anywhere -- `retrieval_log`/`session_state` both read 0, indistinguishable
-- from "installed but idle". This table is written on every hook invocation,
-- BEFORE the handler decides whether to speak, so `meristem doctor` can tell
-- "never invoked" from "invoked, silent" from "invoked, wrong workspace".
-- Deliberately excluded from `meristem export` (sync_protocol.SYNCED_TABLES) --
-- this is per-machine operational state, not shared substrate.
INSERT OR IGNORE INTO schema_version (version, applied_at)
  VALUES (7, unixepoch());

-- ---------------------------------------------------------------------------
-- 1. ATOMS — the unit of knowledge
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS atoms (
  id              TEXT PRIMARY KEY,            -- content-hash stable ID
  type            TEXT NOT NULL CHECK(type IN (
                    'invariant','schema_fact','decision','convention',
                    'dependency','owner','runtime','build_recipe','glossary'
                  )),
  topic_key       TEXT NOT NULL,               -- groups versions of "the same fact"
  decision_status TEXT CHECK(decision_status IN ('OPEN','CLOSED','DEFERRED')),

  -- Which KIND of decision this is (v3). Two categorically different things
  -- shared type='decision' and drowned each other out:
  --   'constraint' — forbids or shapes future work ("no verified badge; legal
  --                  liability; is_verified stays false"). Rare, load-bearing,
  --                  MUST surface before related work starts.
  --   'change'     — a record that something happened ("commit a33e92c did X").
  --                  Auto-generated one per SHA, unbounded, CLOSED by
  --                  definition, constrains nothing.
  -- Retrieval and context injection default to constraints; change records stay
  -- queryable as history. NULL means "not yet classified" (pre-v3 rows).
  decision_class  TEXT CHECK(decision_class IN ('constraint','change')),

  -- Hierarchy tier (SPEC §14.1) — coarse→fine. Module atoms summarize a
  -- subsystem and hold ROLLS_UP edges to their file/symbol children.
  tier            TEXT NOT NULL DEFAULT 'symbol'
                    CHECK(tier IN ('module','file','symbol')),

  -- Provenance
  source_kind     TEXT NOT NULL CHECK(source_kind IN (
                    'commit','transcript','schema_snapshot','sentry_event',
                    'screenshot','readme','manifest','llm_extract','manual'
                  )),
  source_ref      TEXT,                        -- SHA | session_id | hash | path
  source_lines    TEXT,                        -- "10-25" or NULL

  -- Bi-temporal validity
  valid_from      INTEGER NOT NULL,            -- when fact became true (epoch)
  valid_to        INTEGER,                     -- when superseded (NULL = live)
  asserted_at     INTEGER NOT NULL,            -- when stored
  superseded_by   TEXT REFERENCES atoms(id),

  -- Liveness predicate (runnable check for staleness)
  liveness_kind   TEXT CHECK(liveness_kind IN ('regex','ast','sql','none')),
  liveness_target TEXT,                        -- file path / query
  liveness_pattern TEXT,                       -- regex / AST path / SQL
  liveness_last_ok INTEGER,                    -- last time it passed

  -- Confidence + workspace scoping
  confidence      REAL NOT NULL DEFAULT 1.0,
  repo_id         TEXT,                        -- NULL = workspace-global
  workspace_id    TEXT NOT NULL DEFAULT 'default',
  valid_in_refs   TEXT NOT NULL DEFAULT '["main"]',  -- JSON array of refs

  -- Pin / archive
  pinned          INTEGER NOT NULL DEFAULT 0,
  archived        INTEGER NOT NULL DEFAULT 0,

  -- v5: reconfirmation horizon for facts no predicate can check.
  -- Liveness answers "does the code still say this?", which is unanswerable for
  -- a domain rule like "the nightly job runs for every environment except
  -- staging" -- no regex can verify it and it will still be asserted long
  -- after it stops being true. `confirm_by` is the honest substitute: past
  -- this timestamp the atom is surfaced MARKED as unconfirmed rather than
  -- silently trusted. NULL means no horizon (code-derived atoms, which
  -- liveness already covers).
  confirm_by      INTEGER,

  created_at      INTEGER NOT NULL DEFAULT (unixepoch()),
  updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
) STRICT;

CREATE INDEX IF NOT EXISTS idx_atoms_topic ON atoms(topic_key);
CREATE INDEX IF NOT EXISTS idx_atoms_live  ON atoms(valid_to) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_atoms_type  ON atoms(type);
CREATE INDEX IF NOT EXISTS idx_atoms_repo  ON atoms(repo_id);
CREATE INDEX IF NOT EXISTS idx_atoms_tier  ON atoms(tier) WHERE valid_to IS NULL;

-- ---------------------------------------------------------------------------
-- 2. ATOM SUMMARIES — multi-resolution (10w / 50w / 250w)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS atom_summaries (
  atom_id     TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  resolution  INTEGER NOT NULL CHECK(resolution IN (10, 50, 250)),
  text        TEXT NOT NULL,
  token_count INTEGER NOT NULL,
  PRIMARY KEY (atom_id, resolution)
) STRICT;

-- ---------------------------------------------------------------------------
-- 3. EMBEDDINGS (sqlite-vss virtual table populated separately)
-- ---------------------------------------------------------------------------

-- Embeddings live in a sibling virtual table created by sqlite-vss at runtime:
--   CREATE VIRTUAL TABLE atom_vss USING vss0(embedding(384));
-- Mapping table keeps atom_id ↔ rowid stable.
CREATE TABLE IF NOT EXISTS atom_embeddings (
  atom_id     TEXT PRIMARY KEY REFERENCES atoms(id) ON DELETE CASCADE,
  vss_rowid   INTEGER UNIQUE NOT NULL,
  model       TEXT NOT NULL,             -- 'bge-small-en-v1.5'
  resolution  INTEGER NOT NULL,          -- which summary was embedded
  hash        TEXT NOT NULL,             -- of summary text — skip re-embed if same
  embedded_at INTEGER NOT NULL
) STRICT;

-- ---------------------------------------------------------------------------
-- 4. EDGES — typed graph
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS edges (
  id          INTEGER PRIMARY KEY,
  src_id      TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  dst_id      TEXT NOT NULL REFERENCES atoms(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK(kind IN (
                'MIRRORS','IMPLEMENTS','SUPERSEDES','CONTRADICTS',
                'LOCATED_IN','REFERENCES','DEPENDS_ON','OWNS','BLOCKS',
                'CO_CHANGED','DERIVED_FROM','ROLLS_UP'
              )),
  directed    INTEGER NOT NULL DEFAULT 0,  -- 0 = symmetric (store once)
  weight      REAL NOT NULL DEFAULT 1.0 CHECK(weight BETWEEN 0.0 AND 1.0),
  confidence  REAL NOT NULL DEFAULT 1.0,

  -- Bi-temporal
  valid_from  INTEGER NOT NULL,
  valid_to    INTEGER,

  -- Provenance
  source      TEXT NOT NULL CHECK(source IN (
                'shared_ref','embed_sim','commit_couple','llm_extract',
                'git_blame','manual','schema_diff'
              )),
  status      TEXT NOT NULL DEFAULT 'live'
                CHECK(status IN ('live','suggested','rejected')),

  created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
  UNIQUE(src_id, dst_id, kind, valid_from)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src_id, kind) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst_id, kind) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind, weight DESC) WHERE valid_to IS NULL;

-- ---------------------------------------------------------------------------
-- 5. EDGE EVIDENCE — why an edge exists
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS edge_evidence (
  edge_id    INTEGER NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,              -- 'commit_sha'|'file_path'|'symbol'|'llm_quote'
  payload    TEXT NOT NULL,
  created_at INTEGER NOT NULL DEFAULT (unixepoch())
) STRICT;

CREATE INDEX IF NOT EXISTS idx_edge_evidence_edge ON edge_evidence(edge_id);

-- ---------------------------------------------------------------------------
-- 6. INGESTION JOB QUEUE
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS jobs (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,             -- 'commit'|'schema_diff'|'transcript'|'manual'
  payload      TEXT NOT NULL,             -- JSON blob (sha, paths, etc.)
  status       TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','running','done','failed')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  error        TEXT,
  enqueued_at  INTEGER NOT NULL DEFAULT (unixepoch()),
  started_at   INTEGER,
  finished_at  INTEGER
) STRICT;

CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs(enqueued_at) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- 7. SESSION / REPO STATE
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS repo_state (
  repo_id            TEXT PRIMARY KEY,
  workspace_id       TEXT NOT NULL DEFAULT 'default',
  root_path          TEXT NOT NULL,
  last_indexed_sha   TEXT,
  last_indexed_at    INTEGER,
  last_branch        TEXT,
  -- Aggregate `IngestResult.atoms_skipped`/`.notes` across every adapter run for
  -- this repo on the most recent completed `ingest` (v6). `last_ingest_notes` is
  -- a JSON array of strings, NULL when the last run had none.
  last_atoms_skipped INTEGER NOT NULL DEFAULT 0,
  last_ingest_notes  TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS session_state (
  session_id      TEXT PRIMARY KEY,
  started_at      INTEGER NOT NULL,
  ended_at        INTEGER,
  ended_reason    TEXT,                   -- 'capacity'|'user'|'tick_complete'|'halt_safety'
  context_pct_end REAL,
  handoff_path    TEXT,
  branch          TEXT,
  last_commit_sha TEXT
) STRICT;

-- ---------------------------------------------------------------------------
-- 8. EMBEDDING LEDGER — cost discipline
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS embedding_ledger (
  day          TEXT PRIMARY KEY,          -- 'YYYY-MM-DD'
  tokens_used  INTEGER NOT NULL DEFAULT 0,
  call_count   INTEGER NOT NULL DEFAULT 0,
  budget_cap   INTEGER NOT NULL DEFAULT 50000
) STRICT;

-- ---------------------------------------------------------------------------
-- 9. RETRIEVAL LOG — for routing classifier improvement + audit
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS retrieval_log (
  id              INTEGER PRIMARY KEY,
  ts              INTEGER NOT NULL DEFAULT (unixepoch()),
  session_id      TEXT,
  query           TEXT NOT NULL,
  classified_as   TEXT,                   -- 'navigational'|'semantic'|'contradictory'|'implementation'
  atoms_returned  TEXT,                   -- JSON array of atom IDs
  injected_tokens INTEGER,
  user_feedback   TEXT,                   -- 'good'|'bad'|'unused' (post-hoc)
  -- v4: the relevance record. `relevances` is a JSON array of query-atom
  -- cosines parallel to `atoms_returned`; `top_relevance` is the best cosine
  -- seen BEFORE gating (so a silenced turn still records how close it came);
  -- `n_suppressed` counts atoms the gate dropped; `min_relevance` pins the
  -- floor in force at the time, since changing it changes what the row means.
  relevances      TEXT,
  top_relevance   REAL,
  n_suppressed    INTEGER,
  min_relevance   REAL
) STRICT;

-- ---------------------------------------------------------------------------
-- 9b. CANDIDATES — proposed facts awaiting review (SPEC §18)
-- ---------------------------------------------------------------------------
-- Extraction proposes; a human disposes. Nothing here is retrievable: a
-- candidate is not memory until someone accepts it. The queue exists so the
-- capture heuristics can be permissive without the cost of a wrong fact being
-- silently believed later.
CREATE TABLE IF NOT EXISTS candidates (
  fingerprint   TEXT PRIMARY KEY,     -- hash of normalised text; rejection sticks
  text          TEXT NOT NULL,
  proposed_type TEXT NOT NULL,
  score         INTEGER NOT NULL,
  signals       TEXT,                 -- JSON array of matched marker names
  source        TEXT NOT NULL,        -- 'transcript' | 'agent' | 'cli'
  source_ref    TEXT,                 -- transcript filename, etc.
  session_id    TEXT,
  created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','accepted','rejected')),
  reviewed_at   INTEGER,
  atom_id       TEXT REFERENCES atoms(id)   -- set when accepted
) STRICT;

CREATE INDEX IF NOT EXISTS idx_candidates_status
  ON candidates(status, score DESC);

-- ---------------------------------------------------------------------------
-- 9c. HOOK HEARTBEAT — proof a Claude Code hook fired (v7)
-- ---------------------------------------------------------------------------
-- One row per event, upserted on every invocation of `meristem hook <event>`
-- regardless of outcome. Per-machine, not per-session, so `invocations`
-- accumulates across the workspace's whole lifetime rather than resetting.
CREATE TABLE IF NOT EXISTS hook_heartbeat (
  event            TEXT PRIMARY KEY CHECK(event IN (
                     'session-start','prompt','post-tool','stop','pre-compact'
                   )),
  last_invoked_at  TEXT NOT NULL,      -- ISO-8601 UTC, e.g. 2026-08-19T12:00:00Z
  last_outcome     TEXT NOT NULL,      -- 'injected'|'silent'|'skipped_trivial'|
                                        -- 'legacy_dlms_workspace'|'captured'|
                                        -- 'written'|'timeout'|'error:<Type>'
  invocations      INTEGER NOT NULL DEFAULT 1
) STRICT;

-- ---------------------------------------------------------------------------
-- 10. VIEWS — convenience for common queries
-- ---------------------------------------------------------------------------

CREATE VIEW IF NOT EXISTS live_atoms AS
  SELECT * FROM atoms
   WHERE valid_to IS NULL AND archived = 0;

CREATE VIEW IF NOT EXISTS live_edges AS
  SELECT * FROM edges
   WHERE valid_to IS NULL AND status = 'live';

CREATE VIEW IF NOT EXISTS invariants_live AS
  SELECT * FROM live_atoms WHERE type = 'invariant';

CREATE VIEW IF NOT EXISTS closed_decisions AS
  SELECT * FROM live_atoms WHERE type = 'decision' AND decision_status = 'CLOSED';

-- Decisions that actually constrain future work. This is what "what decisions
-- block this goal?" should read — commit records are excluded, since a commit
-- reports what happened rather than forbidding anything.
CREATE VIEW IF NOT EXISTS constraints_live AS
  SELECT * FROM live_atoms
   WHERE type = 'decision' AND COALESCE(decision_class, 'constraint') = 'constraint';
