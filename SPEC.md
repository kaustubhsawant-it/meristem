# Meristem — Living Memory Substrate

A project-agnostic, bi-temporal knowledge substrate for Claude Code. Designed for
maintenance-phase development of multi-repo systems. Drop-in portable.

This document is the single source of truth for design decisions. It is updated
in place as decisions firm up. Older revisions live in git history.

Status: design locked, build in progress (2026-05-22).

**Renamed from DLMS (2026-08-11).** "DLMS" collided with DLMS/COSEM (IEC 62056),
the internationally standardized smart-meter/device-management communication
protocol. Identifiers and prose below use the current name; dated historical
notes (`Correction (date)` paragraphs, `Status (date)` lines) keep whatever
name was accurate at the time they were written.

---

## 1. Philosophy

Maintenance is a control loop, not a project. The user is past the build phase
on a working multi-app system; the bottleneck is **orientation** (where to
change), not **specification** (what to build). Meristem exists to make Claude an
oriented collaborator from token zero.

Five sentences that define the system:

1. **Commits are atoms** — ingestion unit, not files.
2. **BM25 is bread, embeddings are luxuries** — embed only summaries.
3. **Tree-sitter is the universal parser** — 200+ languages, no per-project setup.
4. **Memory is JSONL on git** — teammates pull memory like code.
5. **Atoms come in three sizes** — hook picks the size that fits the budget.

## 2. The OODAR loop

One *tick* = one OODAR cycle = one commit, target <30 minutes.

| Phase | Time | Action |
|---|---|---|
| Observe | ~2m | git state, recent atoms, drift detector |
| Orient | ~5-10m | KB query — invariants, decisions, owners (load-bearing) |
| Decide | ~2m | smallest change that achieves intent |
| Act | ~10-15m | one atomic commit |
| Verify | ~3-15m | run the app, not just tests (UI ticks are longer) |
| **Remember** | ~2m | **write atom back to KB — the engine that makes the system compound** |

Skip Orient → break invariants. Skip Remember → KB rots. Both are hard gates.

## 3. Atom taxonomy (closed, 9 types)

`invariant`, `schema_fact`, `decision` (OPEN/CLOSED/DEFERRED),
`convention`, `dependency`, `owner`, `runtime`, `build_recipe`, `glossary`.

Each atom has: provenance (source kind + ref), bi-temporal validity
(`valid_from`/`valid_to`), liveness predicate (grep/AST/SQL pattern that
re-validates the atom), multi-resolution summaries (10w/50w/250w), embedding.

**`decision_class` (schema v3, 2026-07-25).** `decision` carries a second
discriminator, `constraint` | `change`, because two categorically different
things shared the type:

- **constraint** — forbids or shapes future work ("no verified badge; legal
  liability; `is_verified` stays false"). Rare, load-bearing, must surface
  *before* related work starts.
- **change** — records that something happened ("commit a33e92c did X").
  Auto-generated one per SHA, unbounded, `CLOSED` by definition, constrains
  nothing.

The git ingester mints the second kind, so without the split "what decisions
constrain this goal?" answered with commit subject lines — 76 to 0 in a real
workspace. Context injection and the SessionStart digest default to
constraints; change records stay queryable as history. The discriminator was
chosen over a tenth atom type to keep this taxonomy closed at 9.

**Producers.** Every type now has one: `invariant` from declared schema
constraints (`invariants` adapter), `owner` from git history (`owners`),
`runtime`/`dependency`/`build_recipe` from manifests, `schema_fact` from DDL,
`decision` from commits (change) and `meristem decide` / ADR ingest (constraint),
`glossary` from docs and symbols. `convention` is the one type still without a
producer.

## 4. Edge types (closed, 12 kinds)

```
MIRRORS       — service A writes col X ↔ service B reads col X (cross-repo schema coupling)
IMPLEMENTS    — code symbol fulfills invariant/decision
SUPERSEDES    — newer decision overrides older (directed DAG)
CONTRADICTS   — atoms disagree (symmetric, flagged for human)
LOCATED_IN    — atom pertains to file/dir path
REFERENCES    — atom mentions symbol/path (weaker than LOCATED_IN)
DEPENDS_ON    — runtime/build dependency
OWNS          — person/role → area/file (directed)
BLOCKS        — decision forbids change to target (directed)
CO_CHANGED    — empirical: edited in same commit ≥3 times
DERIVED_FROM  — summary 50w ← raw 250w; atom ← source doc (directed)
ROLLS_UP      — file/symbol atom → its subsystem parent (schema v2, §14.1 hierarchical atoms)
```

**Auto-discovery rules** (no manual wikilinks):
1. Shared file refs → `REFERENCES` (weight 0.6) — **implemented** (`discovery`)
2. Same symbol cross-app → `MIRRORS` (weight <= 0.8, scaled down as the
   mirror group grows toward `MAX_MIRROR_GROUP` — a name shared by exactly 2
   repos is stronger evidence than one shared by 7) — **implemented**
3. Commit-couple ≥3 → `CO_CHANGED` (weight = min(1, count/10)) — **implemented**
4. Embedding cosine ≥ 0.88 → **suggested** edge, requires LLM/human accept — *not built*
5. LLM extraction during ingestion → typed edges with quoted evidence — *not built*
6. `git blame` → `OWNS` edge from person to file — **implemented** (`owners`)

**Correction (2026-08-11): rule 2's "implemented" claim above didn't cover
direction.** `_rule_mirrors` emits `(src, dst)` via `combinations()` over
atoms in id-sorted order — an accident of iteration order, not a meaningful
direction — but `edges.SYMMETRIC_KINDS` only listed `CONTRADICTS` and
`CO_CHANGED`, so MIRRORS was stored `directed=1`. `retrieval._load_edges`
only adds the reverse-direction hop for undirected edges, so a query seeded
from the *dst* side of a MIRRORS pair had no edge to walk at all — exactly
the "MIRRORS partners that pure k-NN misses" capability this section
advertises, silently working from only one endpoint. Fix: `edges.py` now
includes `"MIRRORS"` in `SYMMETRIC_KINDS` (and no longer in
`DIRECTED_KINDS`), so it canonicalizes and dedupes on insert exactly like
`CONTRADICTS`/`CO_CHANGED`. Regression-tested in both directions
(`test_mirrors_edge_canonicalized`, `test_mirrors_traversable_from_either_endpoint`).

These run in `src/meristem/discovery.py` as a post-ingest pass, after every
adapter for a root has finished (so all atoms it might connect exist). Rules
1–3 and 6 are deterministic and stdlib-only, keeping LLMs out of the hot path
(§1). Rule 4 belongs to `meristem embed` (it needs vectors); rule 5 needs a model
call. Every rule is capped so one hub file cannot produce a quadratic edge
explosion that swamps PPR.

**This pass is load-bearing, not an enhancement.** Until it was wired
(2026-07-25) nothing emitted edges except `ROLLS_UP` from the `modules` adapter
and `SUPERSEDES`/`CONTRADICTS` from `assert_fact` — so populated workspaces ran
with **zero edges**, `_forward_reachable` returned only the seeds, all rank mass
leaked back to the personalization vector, and retrieval silently degenerated to
embedding k-NN. Any change that leaves the graph empty removes the entire
difference between Meristem and a vector store; `meristem doctor`'s `edges.density`
check fails on a populated-but-edgeless store for exactly this reason.

**Paths recorded on atoms are relative to the WORKSPACE root**, not the ingest
root, because that is what liveness resolves against. Recording them
ingest-relative breaks multi-root workspaces two ways at once: roots sharing an
internal layout mint colliding atom ids (one app's symbol silently absorbed
into another's repo), and no `liveness_target` resolves, so lazy read-time
verification drops every atom carrying a predicate. See
`IngestContext.rel_path`.

**Retrieval algorithm — Personalized PageRank (HippoRAG-style):**
Seed with top-K embedding hits + structural hits (atoms `LOCATED_IN` current
file). Run PPR with α=0.15, 20 iterations, kind-weighted transition matrix
(`MIRRORS`=1.0, `IMPLEMENTS`=0.9, `REFERENCES`=0.5, `CO_CHANGED`=0.4).
Returns atoms ranked by graph-flow, not raw similarity — surfaces `BLOCKS`
decisions and `MIRRORS` partners that pure k-NN misses.

**"Show me why" output**: every retrieved atom comes with its provenance trail
(hops + edge kinds + evidence). Claude cites verbatim:
*"Surfaced because 2 hops from auth_service.py via MIRRORS to the
session-token-rotation invariant (commit a33e92c)."*

**Visualization**: `meristem graph export --seed file.py --k 2 --format mermaid`
regenerates the Obsidian-feel view on demand, auto-built, typed edges colored
by kind.

## 5. Skills

| Skill | OODAR phase | Use when |
|---|---|---|
| `/meristem:tweak` | compressed cycle | ≤2 files, no schema/auth — typos, colors, copy |
| `/meristem:patch` | Orient+Decide+Act | multi-file, needs invariants. Spawns up to 3 sub-agents when complex |
| `/meristem:check` | Verify+Remember | runs against staged diff. 2 sub-agents: invariant audit + test impact |
| `/meristem:trace` | Observe | KB introspection: "where does X live, who writes Y" |
| `/meristem:handoff` | Remember | write a resumption handoff before context runs out |
| `/meristem:resume` | Orient | restore the last handoff in a fresh session |
| `/meristem:plan` | Orient+Decide | constrain a plan with graph facts before any edit (§15) |

Each skill ends with `Next:` line + edge cases enumerated (severity-tagged).

## 6. Hooks (Claude Code surfaces)

- `SessionStart` — digest: branch + dirty + recent + top-5 atoms + halt contract
- `UserPromptSubmit` — classify → route → inject (≤3K tokens per turn)
- `PostToolUse(Edit|Write)` — invariant-watcher (zero-cost happy path)
- `PreCompact` — generate handoff (per Team Q spec)

**Correction (2026-08-19): hooks now ship in-package.** The five hooks above
(plus `Stop` capture, omitted from the original list) previously lived only
as `~/.claude/hooks/dlms-*.js` on one machine and had gone silently dead since
the 2026-08-11 rename — nothing detected it for 8 days. They're now pure-Python,
in-process, and installed via `meristem hooks install` (project-local
`.claude/settings.json` by default, `--global` for `~/.claude/settings.json`),
dispatched through `meristem hook <event>` (event ∈ session-start | prompt |
post-tool | stop | pre-compact), reading the hook's JSON payload from stdin.
Schema v7 adds `hook_heartbeat` (per-machine, never exported by `meristem
export`): every invocation records that it fired — event, outcome, timestamp —
*before* deciding whether to no-op, so a silent no-op is no longer
indistinguishable from a hook that was never wired at all. `meristem doctor`'s
`hooks.heartbeat` check reads that table and distinguishes never-invoked /
wrong-workspace / stale.

**Housekeeping (2026-09-30).** SessionStart renders a `Housekeeping:` block
carrying the two nudges a human has to act on: `staleness` (§19) and the
pending-review count (§18). Both were computed by `build_digest` and both
reached a session only by accident — pending review because a field nobody
rendered, staleness only as a capped `repo.freshness` health line that worse
findings could push out. When the staleness line is present the duplicate
`repo.freshness` health line is dropped (a *fail*-severity one still counts
toward the DEGRADED verdict). When the index is behind and
`[freshness] auto_sync_on_session_start` is on (default), SessionStart also
spawns a background `meristem sync --quiet` (§19.6); the notice stays, annotated
`background sync started`.

Sessions run for days, so a one-shot SessionStart notice goes stale.
UserPromptSubmit therefore re-surfaces the block, headed `Housekeeping (changed
since last shown)`, when its content differs from what this session was last
shown — facts captured mid-session, or drift that appeared later. "Content" is
the pending count plus whether the index is stale at all, deliberately not the
commit count: otherwise every commit would re-fire the nudge between the commit
and its post-commit sync catching up. One nudge per stale episode; a sync that
catches up resets it, and a change that is only a clearing says nothing. The
check is a `COUNT(*)` and one `git rev-list` per root, no embedder, and runs
ahead of (and independent from) the recall path. It is keyed on `session_id`
and stays silent without one. The last-shown record is per-user operational
state, not store content: `housekeeping_seen.json` in the per-user state dir
(`~/.meristem/`, or `$XDG_STATE_HOME/meristem/` when set), keyed by workspace
then session, pruned to the most recent sessions per workspace. It never lives
inside a repo; a legacy `<workspace>/.meristem/housekeeping_seen.json` from
0.2.0 is deleted the next time the hook writes.

## 7. Token budget (Sonnet 200K, target 10% bootstrap = 20K)

| Layer | Tokens |
|---|---|
| Claude Code overhead | ~6K |
| SessionStart digest (skeleton + top symbols + invariants + decisions) | ~4K |
| Per-turn injection (capped) | ≤3K |
| **Bootstrap total** | **~13K (6.5% of Sonnet)** |

Scales free on Opus 1M (~1%). Holds at 1M LOC because top-200 PageRank
symbols capture ~80% of inbound-edge mass.

## 8. Branching + backup

- `main` always deployable. Short-lived branches off main, squash-merge.
- **Tags ARE stable** — no long-lived stable/develop branches.
- Submodules: pinned exact SHAs; never `branch=main` tracking.
- Per-app tags (`app-v1.2.3`), parent deploy-date tags (`deploy-2026-05-22`).

Backup layers (revised 2026-05-22, Supabase Pro deferred):
1. Git push every session (non-negotiable)
2. **External SSD — full disk image + project backups** (user's chosen layer)
3. `pg_dump` of Supabase to SSD weekly via cron — under Free-tier constraints,
   self-managed backups are the only DB recovery path
4. 1Password vault for secrets, printed recovery codes off-site

**Important caveat**: without Supabase Pro, there is no point-in-time recovery
and no Supabase-side daily snapshots beyond 7 days. User accepts this risk and
mitigates with weekly `pg_dump` + SSD storage. Document a *tested* restore
procedure (spin up scratch Supabase, restore dump, verify load) — untested
backups are not backups.

## 9. Handoff protocol (anti-hallucination)

> **Implementation status (0.3.0).** This section is the design. What exists:
> `meristem handoff` writes `LATEST.md`, and the `/meristem:handoff` and
> `/meristem:resume` skills read and write it. What does not exist: the
> SessionStart "RESUMPTION BLOCK" injection and its marker, any measurement of
> context capacity, and any code that blocks a tool call. The halt contract is a
> text instruction given to the agent, a protocol the agent is asked to follow,
> not an enforced mechanism. See `docs/05-ambient-layer.md`.

**Triggers**: any of —
- `PreCompact` hook fires (harness reports ≥80% context)
- Skill exits cleanly (`tick_complete`)
- User invokes `/meristem:handoff`
- Halt-safety triggers (≥85% capacity)
- `Stop` hook with dirty files and no handoff this session (auto-stub)

**Storage**: `.meristem/handoffs/<UTC-ts>-<session_id>.md` + symlink `LATEST.md`.
Committed to git by default (tribal knowledge). `LATEST.md` is gitignored
(machine-local pointer).

**Schema** (YAML frontmatter + capped narrative):
```yaml
session_id, started_at, ended_at, ended_reason, context_pct_at_end
branch, last_commit_sha, dirty_files
current_tick: {id, title, status, phase}
atoms_consulted: [ids only — bodies fetched on demand]
atoms_drafted: [pending Remember-phase write]
open_questions: [things user hasn't answered]
next_action: [literal verb+target, one sentence]
failed_assumptions: [things that turned out wrong]
do_not_redo: [tried, didn't work — prevent loops]
edge_cases_pending: [from last patch's enumerator, unverified]
```

The narrative section is hard-capped at 400 tokens and lints out any sentence
recoverable from `git log -p` or an atom ID. If regenerable, deleted.

**Resumption (SessionStart logic)**:
```
if LATEST.md exists and age < 24h:
    inject "RESUMPTION BLOCK" (cap 2K tokens):
      current_tick, next_action, failed_assumptions, do_not_redo,
      atom IDs (not bodies), live `git diff --stat`
    marker <!-- MERISTEM-RESUME --> so Claude treats as authoritative
else:
    inject normal digest
```

**Halt-before-hallucination (≥85% capacity)**:
Agentic skills enter safe-halt: tool calls blocked except write-handoff +
git status + atom-draft. Skill emits:
`HALT: context 86%. Wrote handoff <path>. Resume with /meristem:resume in fresh session.`
Read-only skills may continue but refuse new atom drafts. Override:
`--force-continue` (logged; user owns risk).

**StatusLine** shows `ctx 72% | tick TICK-114 | atoms 4` so pressure is visible.

## 10. Sub-agent teams within skills

| Skill | Default | Spawn when… | Cap |
|---|---|---|---|
| `:tweak` | 0 | NEVER (single-context only) | 0 |
| `:patch` | 0 | repos_touched>1 OR schema_changed OR diff>100 LOC OR cross_cutting_hits≥3 | 3 |
| `:check` | 2 | ALWAYS (invariant audit + test impact) | 2 |
| `:trace` | N | one per workspace root matching query | 5 |
| any | +1 | `--deep` flag | — |

**Team shapes:**

`:patch` complex:
- **A1 Implementer** — primary repo only, writes diff
- **A2 Cross-repo verifier** — partitioned to OTHER repos, checks MIRRORS-edges
- **A3 Edge-case enumerator** — runs LAST, sees staged diff + 2-hop neighborhood

Spawn order: A1 → (A2 ‖ A3 in parallel after staging). Partition by repo to
prevent file-collision.

`:check`:
- **B1 Invariant auditor** — verifies each invariant on touched atoms post-diff
- **B2 Test impact analyzer** — maps touched lines → covering tests, flags gaps

`:trace`:
- **C1..Cn** — one per workspace root, each searches independently, parent
merges by symbol

**Sub-agent JSON contract** (parent enforces; reject finding if violated):
```json
{
  "agent": "edge_enumerator",
  "scope": "<path or repo>",
  "findings": [{
    "kind": "boundary_input|race|cross_repo_impact|schema_migration|auth_permission|error_path|backwards_compat|perf_hotpath|invariant_risk|test_gap",
    "severity": "high|med|low",
    "claim": "<one sentence>",
    "evidence": ["file:line", "atom_id"],
    "suggested_action": "<one sentence>"
  }]
}
```

**Validation gates**: every `evidence[]` resolves (file exists, line in range,
atom_id in Meristem) — fail = reject, don't demote. Dedupe by (kind, file,
line_range±5). If 0 findings survive validation, surface honestly: *"no edge
cases detected (N candidates rejected as unverifiable)."*

## 11. Edge-case enumeration (every change)

**Algorithm** (deterministic, runs as skill's penultimate step):
1. **Symbol diff** — parse patch, extract changed symbol names
2. **Caller scan** — ripgrep across project + sibling repos (per `.meristem/mirrors.yml`)
3. **Invariant proximity** — grep `// invariant:`, `assert`, schema constraints ±20 lines
4. **Test coverage gap** — per touched line, check if any test file references symbol
5. **Cross-repo mirrors** — consult atoms tagged `MIRRORS:<symbol>`
6. Rank by (callers × invariant proximity), keep top 5

**Pre-filter by file fingerprint** — CSS change skips auth/race/schema entirely.
Schema change mandates schema_migration + cross_repo_impact + backwards_compat.

**Output format** (each line ≤80 chars, severity-prefixed, max 5; rest collapsed
to `+N more, run :check --deep`):
```
✓ Patched: 3 files, +47 -12 across api, web
  Atoms touched: session.token_ttl, SessionService.refresh
  Agents: implementer, cross-repo (1 finding), edge-enum (3)

Edge cases to consider:
  • [high] web client caches refresh token in localStorage — verify rotation
    → web/src/services/session_service.ts:142
  • [med]  session_revocations RLS depends on user_id shape
    → check policy on table after migration
  • [low]  No test covers token_expired=true branch — smoke manually

Next: review staged diff, run /meristem:check before commit
```

Edge-case surfacing is **mandatory output**, not optional. Empty list says so
explicitly.

---

## 12. Runtime: Python 3.11+

**Decision (2026-05-22)**: the entire Meristem toolchain is Python. Rationale:

| Component | Why Python wins |
|---|---|
| Tree-sitter (200+ grammars) | `py-tree-sitter` + `tree_sitter_languages` — pre-built |
| Embedder (`bge-small-en-v1.5`) | `sentence-transformers` / `fastembed` first-class |
| Cross-encoder reranker | `sentence-transformers` cross-encoders native |
| sqlite-vss vector search | Python bindings stable |
| MCP server | `FastMCP` framework — clean stdio server in ~150 LOC |
| Reference impls (GraphRAG, HippoRAG, Letta, Mem0, Graphiti) | all Python |

Node was the only serious alternative (MCP servers there are slightly more
mature historically), but the Python ML stack asymmetry decides it. One
language, one codebase, one `uv` venv.

**Stack lock-in**:
- Python 3.11+ (match statements, native exception groups)
- `uv` for dependency management (10x faster than pip)
- `FastMCP` for the MCP server
- `sqlite-utils` + `sqlite-vss` for the store
- `sentence-transformers` for embed + rerank
- `tree-sitter` + per-language grammar packages for AST (see correction below)
- `typer` for the CLI
- `pytest` for tests
- `ruff` for lint/format

**Correction (2026-08-12): `tree_sitter_languages` is broken against current
`tree-sitter`, not just outdated.** That package pins an older capsule ABI;
`Language(tree_sitter_languages.get_language("python"))` raises `TypeError`
against `tree-sitter>=0.22` — confirmed by hand, not by changelog-reading.
§16's tree-sitter pillar instead depends directly on tree-sitter org's own
per-language packages (`tree-sitter-python`, `tree-sitter-javascript`,
`tree-sitter-typescript`, `tree-sitter-go`, `tree-sitter-rust`, plus the
community `tree-sitter-dart`) — all ship prebuilt `abi3` wheels covering
Python 3.10–3.14 on Linux/macOS/Windows, so this is a straight `pip install`,
never a compile. See `treesitter.py`.

## 13. Memory Pulse — the status indicator

A sibling status line beneath Claude Code's existing context widget. Five
glyphs, one line, ambient feedback:

```
Meristem │ 🧠 142 · ⏵Orient · 🟢→3 · ⚠ 1 stale · ✦ tick 3
```

| Glyph | Meaning | Source |
|---|---|---|
| `🧠 N`        | live atom count | `SELECT count(*) FROM live_atoms` |
| `⏵<phase>`   | current OODAR phase | tool pattern heuristic (see below) |
| `<color>→N` | atoms injected this turn | retrieval log; color = class |
| `⚠ N stale`  | atoms with failed liveness today | `liveness_last_ok < today` |
| `⟳ N behind` | commits the index has not seen (§19) | `git rev-list --count <last_indexed>..HEAD` |
| `N to review` | captured facts pending in the review queue (§18) | `SELECT count(*) FROM candidates WHERE status='pending'` |
| `✦ tick N`   | OODAR ticks completed today | `git log --since=midnight --oneline | wc -l` |

`⚠ N stale`, `⟳ N behind` and `N to review` render only when non-zero. The pulse is glanced at,
not read, so a glyph that is always present stops being seen.

**OODAR auto-detect heuristic** (uses last 5 tool calls in session):
- `Read|Grep|Glob` dominant → **Observe**
- MCP `query_facts|graph_neighbors` recent → **Orient**
- No tools, only assistant text → **Decide**
- `Edit|Write` recent → **Act**
- `Bash` recent (test/run pattern) → **Verify**
- `assert_fact|supersede` recent → **Remember**

**Color codes for injected-atoms class:**
- 🔵 navigational (`where is X`, `show me Y`)
- 🟢 semantic (`why does X`, `is it safe`)
- 🔴 contradictory (`but we said`, `didn't we decide`)
- 🟣 implementation (`add`, `fix`, `refactor`)

**Killer feature: `⚠ N stale`** — when an atom's liveness predicate fails
(column dropped, file renamed, function signature changed), the count flips
visibly. The user sees schema drift the moment it happens, not after Claude
hallucinates from a stale fact. This is what makes the substrate *visibly
alive*, not just "Claude has memory."

**Compact path (0.3.0)**: `meristem statusline --compact` and the
`meristem-statusline` console script (`meristem.statusline.main`) render through
`compact_line()`. It opens the store read-only with plain `sqlite3` (no
migrations, embeddings, retrieval or CLI framework imports), reads the live-atom,
stale and pending-candidate counts, and shells out to git only for the
freshness `rev-list` and today's tick count. Any failure degrades to a smaller
line instead of an error in the status bar. Measured about 75 ms per invocation
(about 125 ms through the full CLI; docs/15-performance.md), so the design target
here is "well under 100 ms", not the earlier "sub-50ms", which a Python process
start alone makes unrealistic. `hooks install --statusline` prefers the console
script when it is on `PATH`.

**Implementation**: `meristem statusline` command reads `.meristem/atoms.sqlite`
(read-only), prints the line. Wired via Claude Code's `statusLine`
setting in `.claude/settings.json`:
```json
{ "statusLine": "meristem statusline --compact" }
```

**Refresh cadence**: on every assistant turn (cheap query). The `⚠` count
recomputes nightly during consolidation, not on every turn.

---

## 14. Scaling architecture (target: 1M LOC, no degradation)

**Core invariant: retrieval cost and per-turn context cost MUST be sublinear
in codebase size.** A 1M-LOC repo and a 10K-LOC repo should both return ~5
atoms in comparable time and token budget. We scale by bounding the working
set, not by growing it. Five pillars, in dependency order:

### 14.1 Hierarchical atoms (foundational — everything depends on this)
Atoms gain a `tier` dimension: `module` → `file` → `symbol`. A `module`-tier
atom summarizes an entire subsystem and holds `ROLLS_UP` edges to its children.
Retrieval starts at the coarsest tier whose atoms match the seed set, then
drills into a subsystem's `file`/`symbol` atoms only when the query's PPR mass
concentrates there. This bounds the candidate universe regardless of repo size.
- New edge kind: `ROLLS_UP` (child → parent tier). Weight 0.6.
- Ingestion emits module atoms from directory/package structure; symbol atoms
  remain lazy (materialized on first drill-down).

**Status: IMPLEMENTED (2026-05-27).** `tier` column + `ROLLS_UP` edge in
schema; `modules` ingester emits module atoms + roll-up edges; `retrieve()`
runs a coarse PPR pass, picks the top `drill_modules` hot modules, then flows
mass downward into their children and re-ranks (`_augment_downward`). No module
atoms → single-pass, fully backward compatible. Covered by `test_modules.py`
and `test_retrieval.py::test_drill_down_*`.

### 14.2 Incremental everything, keyed on git diffs
Never re-scan the world. The stored `last_indexed_sha` drives all maintenance:
- **Ingest**: only files in `git diff <last_sha>..HEAD` are re-parsed.
- **Embeddings**: only atoms whose `source_ref` changed are re-embedded
  (the `embedding_ledger.json` already tracks this).
- **Liveness**: PostToolUse watcher already scopes to touched files; extend the
  same diff-keying to the background sweep.
First ingest of a huge repo is slow *once*; steady state is O(diff), not O(repo).

**Status: Ingest IMPLEMENTED (2026-05-27), actually reached in practice
2026-08-06.** `git_utils.changed_files_since` unions committed diff + dirty
working tree; `IngestContext.changed_files` / `.is_changed` thread it through;
`iter_files` iterates the changed set directly (O(diff)); readme/manifest/modules
gate on it; first run (no prior sha) → full scan. Embeddings already skip-by-hash.
`owners` gates on it too as of 2026-08-11 — it was the last adapter still doing
a full `git log` rescan every run regardless of the changed set, contradicting
this claim (see the correction note in §4's auto-discovery rules, since the
same review pass that found this found MIRRORS' directionality bug).
Deletions (superseding atoms for removed files) still rely on liveness — a
dedicated reaper is a follow-up.

The mechanism was correct and **inert**: the indexed-sha gate held the sha back
on any designed adapter cap, so no repo above 200 symbols ever recorded one, and
every run was therefore a full scan. Fixed in §19.4. Two further defects found
the same day, both of which silently dropped work from the changed set:
`_run` stripped git's stdout, eating the significant leading space of
`git status --porcelain -z`'s ` M path` record — so the first
modified-but-unstaged file was mangled to a path matching nothing on disk and
never re-ingested until committed. `dirty_paths` is now parsed from unstripped
output, and `commits_between`/`hooks_dir` join it as the §19 primitives.

### 14.3 ANN vector index (replaces linear blob scan)
JSON-blob vectors with `top_k` linear cosine scan dies at ~10⁴ atoms. Swap in
`sqlite-vec` (stays inside the existing SQLite file — zero new infra; preserves
the "local-only, no network" property). hnswlib/faiss only if we outgrow it.
The `embeddings.top_k` contract stays; only its backend changes.

**Status: IMPLEMENTED (2026-05-27).** Optional `meristem[vec]` extra. Vectors are
mirrored into a `vec0` virtual table keyed by `vss_rowid`; `top_k` runs KNN
(L2→cosine on normalized vecs) only when the index is loaded AND fully synced
with the JSON sidecar, else falls back to the linear scan — so retrieval is
correct with or without the extension. `embed`/`embed_pending` keep the index
synced (`sync_vec_index` backfills JSON-only vectors). Covered by
`test_vec_index.py` (skipped without the extra) + a forced-fallback test.

### 14.4 Seed-localized, capped PPR
The current `retrieve()` already restricts the universe to the seeds' connected
component — formalize and harden this for scale:
- Cap hop depth (default 3) so a pathological hub can't pull the whole graph in.
- Precompute and cache PPR vectors for "hot" nodes (auth, schema, payment) and
  reuse them as priors.
- Keep the hand-rolled power iteration (no graph-lib dependency); add an early
  exit when rank deltas fall below ε.

**Status: hop-cap + ε early-exit IMPLEMENTED (2026-05-27).** `_forward_reachable`
caps BFS at `max_hops` (default 3); the provenance BFS is capped too; `_run_ppr`
breaks once the max per-node rank delta < `EPSILON`. Covered by hop-boundary
(parametrized 1/2/3 + default), epsilon-fires, and provenance-at-boundary tests.
Hot-node PPR-vector prior caching is a deferred optimization (needs a cache +
graph-change invalidation) — not required for the bounding guarantee.

**Documented bounds (audit, 2026-05-27):** (1) Retrieval recall is bounded to
atoms within `max_hops` of a seed — atoms farther out are intentionally dropped
(their PPR mass after ≥(1-α)^4 decay is rarely top-n anyway). `max_hops` is a
tunable `retrieve()` param for deep-graph callers. (2) ε early-exit guarantees
ranking stable up to O(ε/α) ≈ 1e-5 score ties, not bit-identical ordering —
negligible for `top_n`.

### 14.5 Confidence decay + conflict detection + lazy liveness
At scale, contradictory and stale atoms accumulate:
- **Decay**: down-rank atoms whose `confidence` hasn't been reconfirmed within a
  half-life window (time-weighted, not deleted).
- **Conflict**: when a new atom shares `topic_key` with a live atom but differs,
  raise a `CONTRADICTS` edge and surface it rather than silently superseding.
- **Lazy liveness**: verify an atom is live *at read time*, plus a background
  incremental revalidation sweep — never an eager global scan over millions.
- **Deletion reaper**: close atoms whose source path was removed from the repo.

#### The deletion reaper (2026-08-06)

Ingestion is additive: no adapter revisits a path that is no longer there, so an
atom asserted from a deleted file stays `valid_to IS NULL` forever. Verified on
a real workspace — delete a file, commit, re-ingest, and the atom for its symbol
is still live.

Lazy liveness is a partial net, and the parts it misses are the point. An atom
with a `regex` predicate pointed at a deleted file fails verification at read
time and is dropped, so it does not reach a reader. But it stays live in the
store, inflating every census `doctor` reports; it stays in the PPR universe,
and because `_seed_set` runs off `embeddings.top_k` — which does not check
liveness — it can still *seed* retrieval and shape what does come back; every
read re-runs its predicate against a missing file, forever; and atoms with **no**
predicate are not covered at all. `module:` and `owner:` atoms carry none, so a
deleted subsystem keeps a live module atom — the highest-degree node class in
the graph, which is exactly what keeps seeding drill-down into a subsystem that
no longer exists.

**Positive evidence only.** Reaping closes knowledge, so it acts on deletions
git asserts (`git diff --diff-filter=D`), never on "the path isn't on disk".
`source_ref` is not uniformly a path — `commit:` atoms carry a 40-char sha, and
no column distinguishes them — so an absence-of-file test would silently close
every commit atom in the store on its first sweep. Where `changed_files_since`
treats an unanswerable git call as "rescan everything", `deleted_files_since`
treats it as "reap nothing"; getting those two backwards closes atoms on every
git hiccup.

Directory-backed atoms need their own test, because git reports deleted files
and never deleted directories: a `module:`/`owner:` atom is closed when no
tracked file remains under its prefix.

Reaping **tombstones** (`valid_to`), never deletes — the history model in
`atoms.py` is append-only, and the fact was true; it now has an end date.
`meristem ingest` reaps the interval it just indexed, after the adapters run so a
delete-then-recreate inside one interval is not left closed. `meristem reap
--since <sha>` sweeps the backlog in a store built before any of this existed.

**Status: Decay IMPLEMENTED (2026-05-27), clock corrected (2026-07-25).**
`retrieve()` multiplies each final score by `confidence × 0.5^(age/half_life)`
(`_apply_decay`, default half-life 90d) before the top-n cut. Time-weighted,
never zeroed by age; opt-out via `decay=False`.

Age is measured from `liveness_last_ok` → `valid_from` → `asserted_at`. The
chain originally ran through `updated_at`, which records when Meristem *wrote the
row*, not when the fact became true — so a first ingest stamped every
commit-decision with the same ingest time and a two-year-old commit scored
identically to a constraint asserted a second earlier. `valid_from` carries the
commit's author timestamp, which is the honest clock: measured against a 90d
half-life, a 2-year-old commit now scores 0.0036 where it previously scored
1.0000. An atom whose predicate still passes keeps full weight regardless of
age, because `liveness_last_ok` wins the COALESCE — facts that can be
re-verified stay, facts that were merely recorded sink as they go unconfirmed.

**Status: Conflict detection IMPLEMENTED (2026-05-27).** When `assert_fact`
auto-supersedes a live atom with a *divergent* claim on the same
`(type, topic_key, workspace_id)`, it raises a symmetric `CONTRADICTS` edge
(old↔new) and reports the closed ids on the returned atom's runtime-only
`conflicts` field — the supersede is surfaced, not silent. The single-live-atom
invariant is unchanged (the old atom is still closed); edges are created after
the atom txn commits (advisory, never fail the assert). Skipped for
`NATURAL_EVOLUTION_SOURCES` (commit/schema_snapshot/manifest), where supersede
is normal re-ingestion, and via `detect_conflict=False`. Covered by
divergent-supersede, natural-evolution-skip, opt-out, idempotent, and fresh-topic
tests.

**Status: Lazy liveness IMPLEMENTED (2026-05-27) — §14.5 COMPLETE.** `retrieve()`
verifies atoms *as it emits them* when given a `repo_root`: a predicate whose
`liveness_last_ok` is older than `verify_freshness_seconds` (default 1h) is
re-run, and an atom whose predicate now fails is dropped — we never return a
fact we can't verify live. Bounded: emission stops at `top_n` live atoms, so a
dead atom is replaced by the next-best live one, never an eager global scan.
Recently-checked and predicate-less atoms pass without I/O; the one remaining
un-runnable kind, `ast`, is kept (can't-verify ≠ verified-dead) — `sql` got a
runner 2026-08-07 (below). With no `repo_root`
(unit tests, ranking-only callers) verification is skipped — identical to prior
behaviour. The CLI `query`, MCP `query_facts`, and `router.route()` all pass
`repo_root=layout.root`. Background `liveness.revalidate_sweep(repo_root,
limit=50)` re-checks only the `limit` stalest atoms (oldest `liveness_last_ok`
first), walking the staleness frontier forward across sweeps — never a global
scan. Covered by drop-dead, skip-without-root, trust-fresh, and sweep
bounded/recheck/unrunnable/empty tests.

**Correction (2026-08-07): `route()` was never actually covered by the
COMPLETE claim above.** It called `retrieve()` without `repo_root`, so
`verify = verify_live and repo_root is not None` (`retrieval.py`) was false on
every UserPromptSubmit turn — the one surface that speaks unbidden into every
prompt was also the one surface where liveness verification silently no-opped,
and every atom it returned read `liveness_state="none"` (the "no predicate"
state) regardless of whether it actually carried one, collapsing exactly the
`none`/`unverifiable` distinction this section exists to preserve. `meristem
query` and MCP `query_facts` were never affected — both already passed
`repo_root`. One-line fix (`router.py`): pass `repo_root=layout.root` into the
`retrieve()` call. Regression-tested: an atom whose predicate is made to fail
without re-ingesting is now dropped from `route()`'s output, and a passing
predicate now reports `liveness_state="verified"` instead of `"none"`.

**Liveness states (2026-07-25) — part of the output contract.** `check_atom`
returns one of four states rather than a bare bool, and no longer raises for
unimplemented predicate kinds:

| state | meaning | surfaced? |
|---|---|---|
| `verified` | predicate ran and matched | yes |
| `trusted` | a pass inside the freshness window, not re-run | yes |
| `none` | atom carries no predicate | yes |
| `unverifiable` | predicate could not be run (`ast` has no runner) | yes, **marked** |
| `failed` | predicate ran and did not match | no — atom dropped |

Only `failed` removes an atom. `unverifiable` exists because collapsing it into
either neighbour is a lie: treating it as verified — which is what swallowing
`NotImplementedError` and returning `True` did — makes an atom nothing checked
indistinguishable from one that passed, in the system whose whole promise is
that memory it cannot stand behind says so. Treating it as failed silently
discards knowledge to make the report look clean.

`Retrieved.liveness_state` carries this to callers; `meristem query` renders a
per-row glyph plus a footer, and MCP `query_facts` returns it as a field.
`meristem doctor` counts unverifiable atoms on their own line rather than folding
them into staleness, and `revalidate_sweep` reports them separately from
`failed` — a missing runner is a coverage gap, not evidence a fact rotted.

**`sql` liveness IMPLEMENTED (2026-08-07).** Self-referential — the predicate
is a `SELECT` run against Meristem's own atoms store, not a file, so it carries no
`target` (the `.target`/`repo_root`-escape handling in `_check_regex` doesn't
apply). Passes when the query returns a row, fails when it returns none.
Guarded to read-only: anything not starting with `SELECT` is rejected before
it runs, because this predicate is invoked unattended from
`revalidate_sweep` — a mutating statement disguised as a liveness check would
otherwise corrupt the store from a background pass. `sqlite3.execute()`
independently refuses multiple statements in one call, so a `SELECT …;
DELETE …` smuggling attempt fails closed as invalid SQL rather than running
the first half. `meristem decide --liveness-kind sql --liveness-pattern "SELECT
…"` is the one authoring path (MCP `assert_fact` does not expose liveness for
any kind yet — a pre-existing gap, not new to `sql`). `ast` (tree-sitter query
against parsed file) remains the one un-runnable kind, blocked on SPEC §16's
tree-sitter pillar.

**Reliability principle:** never return a fact that cannot be verified live.
"I have no live fact for that, here's what I'd read" beats a hallucinated
invariant. This is the credibility moat — a memory layer that is ever
confidently wrong gets abandoned. Capacity is the symptom; trust is the product.

### 14.6 The relevance gate — abstention (2026-08-05)

§14.5 keeps Meristem from returning facts that are *false*. It did nothing about
facts that are *true but irrelevant*, and that gap is what made the substrate
ignorable in practice.

**The defect.** `retrieve()` ranks by PPR score, and `route()` injected the
top-N unconditionally. A PPR score is a share of probability mass over the
reachable graph: it orders atoms *within* one query and carries no information
about whether any of them match it. Worse, `_seed_set` normalises the seed
cosines to sum to 1 before PPR runs, so the one number that did measure
relevance was destroyed before ranking began. The result was a memory that answered
every prompt with equal confidence, including prompts it knew nothing about.

Measured on a real 607-atom workspace (`tools/replay_retrieval.py`, 198 logged
prompts):

| signal | nonsense query | genuine query | separates? |
|---|---|---|---|
| PPR score (top-1) | 0.1124 | 0.1003 | **no — nonsense scored higher** |
| query↔atom cosine | 0.583–0.666 | 0.828–0.836 | yes, cleanly |

No PPR-score floor between 0 and 0.02 dropped a single atom, and none silenced
a single query. The score was never a usable gate; the information was not there.

**The fix.** `Retrieved.relevance` carries the cosine between the query vector
and *that atom's own* stored vector — measured over the ≤`top_n` atoms actually
being returned, so it costs a handful of dot products against vectors already in
the store and adds no model call to the hot path. `route()` drops atoms below
`[retrieval] min_relevance` and injects nothing when nothing clears it.

`relevance = -1.0` means *unknown* (no comparable vector under the querying
model) and fails the gate. Unknown-as-irrelevant is deliberate: an atom with no
comparable vector is the exact state that let a cross-model workspace serve
confident noise for two months.

**The floor is embedder-specific.** Cosine scales differ per model; 0.70 is
calibrated for `bge-small-en-v1.5` and is meaningless for another embedder.
Re-derive it with `tools/replay_retrieval.py` when changing `embedder`.

**Schema v4** adds `retrieval_log.relevances`, `top_relevance`, `n_suppressed`
and `min_relevance`. This is what turns the log from an audit trail into a
regression corpus: every turn now records how good its own answer was, so a
change to retrieval can be measured against real prompts rather than argued
about. Pre-v4 rows keep NULL — they were served with no gate and must not be
read as scoring zero. A silenced turn is still logged, with `top_relevance`
recording how close it came; silence must be auditable or it is
indistinguishable from a broken hook.

**Principle:** a memory that always answers teaches its reader to stop reading
it. Abstention is not a degraded mode — it is the feature that makes the
non-abstaining case worth attending to.

### 14.7 One gate, three doors (2026-08-06)

§14.6 built the gate inside `router.route`. Two of the three ways into the store
never went through it.

**The defect.** `meristem query` and MCP `query_facts` call `retrieval.retrieve`
directly. They therefore served ungated results for as long as the gate had
existed, and served the module-tier scaffolding atoms that §14.1 creates to make
drill-down work — atoms that win the top slot on most queries by being the
highest-degree nodes in the graph. `meristem query` also displayed only `score`,
the number §14.6 had just finished measuring as carrying no relevance
information at all. Three doors into one store, two of which disagreed with the
third about what "relevant" means.

**The fix.** The scaffolding predicate and the floor arithmetic live in
`retrieval.gate`, which returns a `Gated` verdict partitioning hits into
kept / suppressed / scaffolding. All three surfaces share it. `scaffolding` is
counted apart from `suppressed` because filtering machinery is not a relevance
judgement — and `n_suppressed` is what `doctor` reads to decide whether the
floor is calibrated, so conflating them corrupts that signal.

The *policies* still differ, deliberately:

| surface | policy | why |
|---|---|---|
| `route()` | drops silently | speaks into a prompt nobody asked to see |
| `query_facts` | drops, reports the count | its caller is an agent that treats what returns as authoritative |
| `meristem query` | shows all, marks the failures | a human typed the query and may see what was rejected |

`query_facts` returns `{atoms, suppressed, scaffolding_hidden, top_relevance,
min_relevance, silenced}` rather than a bare list. An empty list cannot say why
it is empty, and "no answer" versus "broken store" are the two readings an agent
must not confuse — one is a result, the other is a reason to retry.

### 14.8 The floor is not a constant (2026-08-06)

0.70 was derived from one embedder against one 607-atom corpus, then shipped as
a default every workspace inherits. Measured consequences: under the
deterministic hash embedder a real query tops out around 0.33, so 0.70 silences
the workspace completely; on a near-empty store a genuine match scored 0.678 and
was suppressed. `tools/replay_retrieval.py` could always re-derive it by hand.
Nobody did, across four sessions.

`meristem calibrate` is that derivation as a command. It measures two distributions
through the real retrieval path — the workspace's own logged prompts, and a
fixed nonsense probe set — and places the floor at
`nonsense_p95 + 0.25 × (genuine_p25 − nonsense_p95)`. The blend is 0.25, not
0.5, because the errors are not symmetric: a floor too low leaks noise into
every prompt, but a floor too high makes Meristem mute, and mute is the failure that
gets the substrate uninstalled rather than debugged. That same rule reproduces
§14.6's hand-calibrated 0.70 from its own published numbers (0.666 + 0.25 ×
(0.828 − 0.666) ≈ 0.707), which is the only validation datapoint available.

The probe set is **fixed, not random**: a random one would make the
recommendation jitter between runs on an unchanged store, which is
indistinguishable from the store having changed.

**It refuses more often than it answers, on purpose.** With fewer than 20
embedded atoms or 20 distinct logged prompts it returns `insufficient_data` and
no number — a floor derived from four queries carries the authority of a
measurement and the reliability of a guess. When the two distributions overlap
it returns `not_separable`: an embedder that cannot tell nonsense from a real
question has no usable floor, and emitting one would present an embedder failure
as a tuning knob.

`meristem doctor` gains `retrieval.calibration`, which reads the `top_relevance` and
`min_relevance` columns §14.6 added rather than replaying retrieval, so it costs
two indexed counts. It cannot recommend a number — that needs the probe set —
but it can say the one you have is wrong, in the two directions that matter: a
floor that silences most turns, and a floor that has never dropped anything (a
gate that never fires is the pre-§14.6 behaviour wearing a floor).

**Principle:** a threshold that ships as a constant will be inherited by every
workspace that should not have it. If a number has to be calibrated, the
calibration has to be a command, not a paragraph in a spec.

## 15. Planning mode — `/meristem:plan`

Meristem lacks a planning surface; this adds one. `/meristem:plan <goal>` uses the
memory graph to constrain a plan *before* any edit:
1. Retrieve invariants, closed decisions, owners, and affected module-tier atoms
   for the goal.
2. Emit a **dependency-ordered task plan** where each task carries the
   constraints (invariants, schema facts) it must not violate.
3. Flag any naive approach that hits a closed decision (`BLOCKS`/`CONTRADICTS`)
   up front, instead of discovering it mid-patch.
4. Persist the plan itself as a `decision`-tier atom set, so planning history is
   queryable ("why did we choose this approach?").

Plans feed existing phase/execution workflows (e.g. GSD) as constraint context —
Meristem supplies the facts, it does not own execution.

**Status: IMPLEMENTED (2026-08-14).** The `meristem-plan` skill described this
surface (and this CLI invocation) for a full session before the CLI backing
it actually existed — a planning tick hit the dead end and had to reconstruct
the workflow by hand via `query`/`why`. `meristem plan "<goal>"` now runs
`retrieval.retrieve()` (the same PPR-over-typed-edges traversal `meristem
query` uses, gated the same way per §14.6) but keeps module-tier atoms rather
than dropping them as scaffolding — here they ARE the "affected module"
signal, not retrieval machinery to hide. Kept hits partition into **surfaces**
(module-tier atoms, falling back to individual symbol atoms when nothing
rolled up — a commit or other non-`glossary` atom is never a surface, even as
a fallback: there is no code to touch there) and **constraints** (invariant,
schema_fact, or a `constraint`-class decision). Each surface's own live edges
are looked up directly (not just what PPR happened to retrieve) so a
`meristem decide --blocks <path>` constraint reaches the plan even when the
decision atom itself didn't clear the relevance floor for `goal`; a live
BLOCKS edge from a CLOSED decision, or any live CONTRADICTS edge, marks that
surface `[BLOCKED]`. No LLM runs inside the CLI — per §1, that would be
exactly the kind of unsourced claim this substrate exists to prevent — so the
"task list" is literally the retrieved surface atoms, not invented prose;
turning that into real task descriptions is what the calling skill or GSD
phase is for. `--persist` records the plan as one CLOSED `change`-class
decision atom (a plan is a record of what was considered, not itself a new
binding constraint). Owners attach the same way as invariants/decisions — a
live OWNS edge into a surface pulls the owner atom in as context (never
blocking; only BLOCKS-from-CLOSED and CONTRADICTS do that) — but get their
**own** section rather than an inline `[owner]`-tagged constraint line:
`Task.owners`/`Plan.owners` (and `--json`'s `owners` key, at both task and
top level) are populated separately from `constraints`, since who to loop in
is not a fact the approach must not violate. **2026-08-17: closed the
GSD-consumer gap** — `tools/gsd_plan_reader.py` is a pure JSON→Markdown
transform (no DB access) that turns `meristem plan --json` output into a
fragment shaped to paste into a GSD `RESEARCH.md`/`PLAN.md`, breaking Owners
and Blocking decisions out as their own headings; see
`skills/meristem-plan/SKILL.md`'s "Machine-readable output and the GSD
consumer" section and `tests/test_gsd_plan_reader.py` (including an
end-to-end test piping a real `meristem plan --json` run through it).

## 16. Adoption pillars (path to industry standard)

Sections 14–15 make Meristem *reliable*; these make it *adopted*:
- **Agent-agnostic MCP.** The MCP server is the wedge — make `query_facts` /
  `assert_fact` consumable by Cursor, Windsurf, Cline, Copilot, not just Claude
  Code. The memory layer that works everywhere wins.
  **Status: IMPLEMENTED (2026-08-17).** The server side of this was already
  true — `mcp_server.py` is plain FastMCP tool functions with no Claude-Code
  affordance, and the README's "Connecting other agents" section already
  documented client configs for Desktop/Cursor/Windsurf/Cline/Continue. What
  was missing was proof: `test_mcp_server.py` said outright it couldn't run
  the real server ("would require stdin/stdout wiring") and only exercised
  the Python functions behind the tools, never the actual JSON-RPC/stdio
  transport a non-Claude-Code client speaks. `tests/test_mcp_stdio_e2e.py`
  closes that — it spawns the real `meristem mcp` subprocess and drives it
  with the reference `mcp` SDK's `ClientSession` (the client class Cursor/
  Windsurf/Cline embed, not FastMCP's own client and nothing Claude-Code-
  specific) over the real stdio transport. It verifies: tool discovery
  returns valid JSON-Schema `inputSchema`s with the right `required` sets for
  every tool; `ping`/`assert_fact`/`query_facts` round-trip correctly end to
  end, including decoding `structuredContent`; a malformed argument (wrong
  type, the way a generic client's own bugs would show up) comes back as a
  normal `isError` result rather than dropping the connection, and the
  session survives to serve the next call. One real finding along the way,
  not a bug: `assert_fact` does not embed synchronously (embedding is the
  separate `meristem embed` batch step by design), so an atom just written
  has `relevance == -1.0` (unknown) until embedded and is gated out by any
  non-negative `min_relevance` — expected per `retrieval.gate`'s own
  documented contract, not an MCP-layer defect. **2026-08-17: closed the
  http/sse half of the gap** — `tests/test_mcp_http_e2e.py` spawns the real
  `meristem mcp --transport http` / `--transport sse` subprocess on an
  ephemeral localhost port and drives it with the reference SDK's
  `streamable_http_client`/`sse_client` (the network-transport client
  machinery a remote or browser-based agent uses, not a stdio-spawning
  local one). It runs the identical five checks as the stdio module —
  schema discovery, ping, assert/query round trip, why round trip,
  malformed-argument survival — parametrized over both transports, so
  `SUPPORTED_TRANSPORTS`/`mcp_server.run`'s `http`/`sse` dispatch (previously
  only unit-checked for validation, never driven live) is now proven to
  actually serve a real MCP session end to end. `_content_json` and the
  `workspace` fixture moved into `conftest.py` so stdio/http/sse share one
  copy instead of drifting. Not yet done: still drives the reference SDK
  client rather than literally launching a Cursor/Windsurf/Cline binary.
- **Tree-sitter symbol graphs.** Replace regex ingesters with tree-sitter for
  accurate, language-agnostic symbol atoms and edges on large polyglot repos.
  **Status: IMPLEMENTED (2026-08-12)** for python, javascript, typescript,
  tsx, go, rust, dart — the same 5 language families the regex v1 covered,
  plus a typescript/tsx split. `symbols.py` parses each file's real syntax
  tree (`treesitter.py`) instead of grepping source text, so a `def` inside a
  triple-quoted string or a commented-out `class Foo:` can no longer mint a
  phantom symbol atom. Each extracted symbol gets an `ast` liveness
  predicate — an exact identifier-node match against a live re-parse, immune
  to the `\b`-boundary regex bugs that punctuation-prefixed names (npm
  scopes, `$emit`) hit twice under the old `regex` predicate. A language
  whose grammar isn't importable falls back to the original regex extractor
  with a `regex` predicate — same graceful-degradation shape as `embed`/`vec`
  — and `meristem doctor`'s `liveness.unverifiable` check reports exactly
  which language is missing its grammar, per atom, rather than a blanket
  per-kind flag. Edges are unaffected directly — `discovery.py`'s
  `REFERENCES`/`MIRRORS` rules already run on `glossary` atoms regardless of
  how they were produced, so more accurate symbol atoms make the edges built
  from them more accurate for free. Not yet done: no new edge kind for
  call/reference graphs (the 12 edge kinds stay closed, per §4) and no LSP
  cross-file resolution (`meristem.toml`'s `[ingesters.symbols] lsp` flag is
  still aspirational).
- **Team sync protocol.** The shared `.meristem/atoms.sqlite` is committed (§8);
  formalize a merge/conflict resolution so two devs' memories combine cleanly.
  **Status: IMPLEMENTED (2026-08-12, extended 2026-08-18, 2026-08-19).**
  `atoms.sqlite` is a binary file — two
  devs' concurrent commits to it are a hard conflict resolvable only by
  discarding one side wholesale, so it is now always local-only (gitignored).
  `meristem export` writes the shared substrate (atoms, atom_summaries, edges,
  edge_evidence — never the per-machine tables: jobs, retrieval_log,
  session_state, embedding_ledger, candidates, and `repo_state`, whose
  `root_path`/`repo_id` are derived from an absolute local path and could
  never agree across two clones) to `.meristem/atoms.jsonl`, one sorted JSON
  object per row, so a git diff shows real changes only. `meristem import`
  merges it back into the local db as a union, never destructive.
  `sync_protocol.py` gives every mutable column a commutative, associative,
  order-independent merge policy — earliest-close-wins for `valid_to`,
  sticky-true for `pinned`/`archived`, latest-wins for `liveness_last_ok`,
  more-precise-wins for `liveness_kind` (an `ast` predicate from a dev with
  the tree-sitter grammar installed beats a `regex` fallback from one
  without), union for `valid_in_refs`, max for edge weight/confidence,
  sticky-rejected for edge status — so a merge never has to leave `<<<<<<<`
  markers. `edges` are keyed by `(src_id, dst_id, kind, valid_from)` for
  merge purposes, not `id`, which is a local autoincrement meaningless across
  two independently grown databases. After loading, `reconcile_topics` closes
  any topic left with more than one live atom (two devs asserted divergent
  claims offline) down to one survivor and raises a CONTRADICTS edge to the
  closed duplicate — reusing `assert_fact`'s own conflict semantics (§14.5)
  rather than silently picking a winner. `meristem merge-driver` is a git
  merge driver over the same pure function, registered as local git config by
  `meristem init` (`.gitattributes` names the driver and is committed/shared;
  the driver *command* behind that name is inherently per-clone, so `init`
  must be re-run — harmlessly idempotent — on every clone). **2026-08-18:
  closed the auto-import-on-pull half of the gap** — `meristem import
  --install-hook` writes `post-merge` and `post-checkout` hooks (mirroring
  `sync --install-hook`'s `post-commit` hook, same marker/idempotent/
  refuse-to-clobber-a-foreign-hook shape, factored into a shared
  `_write_managed_hook` helper both now call) so a dev no longer has to
  remember to run `meristem import` by hand after `git pull`. **(Hook file format revised 2026-09-30, §19.6: each managed hook now lists every workspace that uses the repo rather than hard-coding one.)** Two hooks, not
  one, because `git pull` fires post-merge but never post-checkout, while
  switching branches (including the checkout `git clone` itself performs)
  fires post-checkout but never post-merge — either can be the first moment a
  teammate's exported `atoms.jsonl` shows up in the working tree.
  `post-checkout` gates on git's own `$3=1` flag so a plain file checkout
  (`git checkout -- path`) — which git also routes through this hook, with
  `$3=0` — is a no-op rather than an unnecessary import. **2026-08-18: closed
  the atom_summaries field-merge half of the gap** — `merge_summary_row`
  (sync_protocol.py) no longer picks "whichever text is longer." `resolution`
  (10/50/250) is a target *word* count — every atom's content hash is keyed
  over `summary_50w` (atoms.py) and retrieval spends a token budget per
  resolution assuming it holds — so a summary that overshoots the target
  isn't more complete, it's a broken budget. The merge now prefers whichever
  side's word count is closer to the target, with a deterministic,
  order-independent tiebreak (`_stable_pick`, same combinator `atoms`'s own
  merge already uses) when both are equidistant. The one genuinely
  independent field, `token_count`, is never copied from either side — it is
  always re-derived from the winning `text` via a private `_approx_tokens`
  (same one-line heuristic as `atoms._approx_tokens`/`handoff._approx_tokens`/
  `router._approx_tokens`; this codebase already keeps one private copy per
  module rather than share one, so a fourth copy here matches convention
  rather than breaking it), closing off a class of bug where a stale or
  foreign token_count could ride along with merged text. **2026-08-19: closed
  the incremental/streaming-export half of the gap** — a second `[sync] mode`,
  `"git-jsonl-sharded"`, sits alongside the original `"git-jsonl"` (one file)
  for a store too large to comfortably rewrite/diff/parse whole on every
  export. `sync_protocol.export_jsonl_sharded` partitions the same four
  tables' rows across many small files under `[sync] export_dir`
  (`.meristem/atoms/`), one per `shard_prefix_len`-hex-char bucket
  (`sync_protocol.shard_of`) of an atom's own content-hash id — the same
  fan-out idea as git's `.git/objects/xx/`, chosen for the same reason.
  `atom_summaries`/`edges`/`edge_evidence` ride along in the shard of the
  atom that owns them (an edge uses its `src_id`'s shard, one deterministic
  choice independent of where `dst_id` lands). Each shard file is a complete,
  self-contained, byte-stable snapshot in the exact sorted format the
  monolithic export already used — never a delta or an append-only log — so
  no compaction or replay logic exists or is ever needed; a shard just *is*
  the current state of the rows hashed into it. `meristem export` in sharded
  mode only rewrites a shard file whose computed text actually differs from
  what's on disk, so an untouched shard produces zero git diff. This is what
  bounds the cost the whole mode exists to avoid: touching one atom only ever
  touches its one shard, so `export`, the merge driver (already file-generic —
  `merge_jsonl_texts` needed no change, since git invokes it once per
  conflicting shard file exactly as it already did for the single-file case),
  and a teammate's `import` on pull all stay O(shard) rather than O(store).
  `import_jsonl_sharded` merges rows from every shard text together *before*
  running `reconcile_topics` once at the end, not once per shard, because two
  atoms sharing a `topic_key` can hash into different shards (the shard key is
  content-hash-derived, unrelated to topic_key) — a genuine cross-shard
  conflict is only visible once the whole imported set is loaded.
  `meristem init` registers the merge driver against `export_dir/*.jsonl` in
  `.gitattributes` instead of a single literal path when this mode is active;
  every other piece of SPEC §16 (row identity, the per-field merge policy,
  `reconcile_topics`, the post-commit/post-merge/post-checkout/post-rewrite hooks) is
  shared unchanged between both modes — sharding only changes how the same
  merged rows are laid out on disk.
- **Eval harness (non-negotiable for credibility).** A small multi-hop benchmark
  measuring retrieval quality + token cost vs naive grep vs full-file-read.
  Turns "comparable to HippoRAG" into a defensible chart, not a vibe.
  **Status: IMPLEMENTED (2026-08-07, extended 2026-08-14).** `tools/eval_harness.py`
  builds a synthetic repo in-process (real ingest → edge-discovery → embed →
  retrieve, no subprocess `meristem` binary), now across **two structurally
  different corpora** (closing the "only one baseline corpus" gap): `schema`
  — three code↔schema pairs, each co-changing so a CO_CHANGED edge (or a
  direct embedding hit) is the only route from a code symbol to the schema
  constraint governing it; `policy` — three code↔decision pairs where the
  decision is asserted directly (`assert_fact` + `edges.link_blocks`, exactly
  what `meristem decide --blocks <path>` does) and reaches the code only
  through a BLOCKS edge, never CO_CHANGED, so there is no file the constraint
  could ever be grepped out of. Both corpora's constraint wording shares no
  vocabulary with the code symbol, so grep and full-file-read structurally
  cannot find either. `--embedder {hash,real}` now selects the deterministic
  hash placeholder (default) or real `bge-small-en-v1.5` via
  `SentenceTransformerEmbedder`. Result on the hash embedder: Meristem 6/6
  recall (3/3 each corpus), grep 0/6, full-file-read 0/6, avg tokens 187 / 12
  / 16 — unchanged in kind from the original 3/3 single-corpus result. Result
  on the **real embedder, across 5 runs: 3–4/6** (both corpora affected, not
  just the newer one) — noticeably below the hash placeholder's stable 6/6.
  Root cause, traced rather than assumed: the git ingester embeds each
  commit's short SHA directly into that commit atom's 50-word summary
  (`ingesters/git_commits.py`), and the synthetic corpus mints fresh commits
  (fresh SHAs) on every harness run, so the noise-atom embedding vectors
  genuinely differ run to run under a real model's fine-grained cosine
  landscape — enough to flip which atom lands in the `MERISTEM_TOP_K=8`
  cutoff for a question whose margin is thin. The hash placeholder's coarser
  scoring doesn't expose this. This is a real, reportable finding, not a
  harness bug: it says real-embedder recall on raw-identifier queries (the
  literal case this harness tests — "what a naive search types verbatim") is
  good but not saturated, and worth watching rather than asserting a single
  static number. **2026-08-17: closed the real-world-repo half of the gap**
  — `--real-repo-root <path>` runs a third, hand-verified `REAL_WORLD_QUESTIONS`
  corpus against an *existing* workspace's live `.meristem/atoms.sqlite`
  (never ingests; embedder auto-detected from what the store was actually
  embedded with, since querying bge vectors with the hash embedder — or vice
  versa — is a meaningless cosine). Run against this repo's own self-hosted
  store (`--real-repo-root .`): Meristem 3/3, grep 0/3, full-file-read 0/3,
  avg tokens 204 / 76 / 3956. Two real, previously-undetected leaks surfaced
  and got fixed while building this, not just theorized: (1) grep/full-file-
  read were walking `.meristem/` itself — since `atoms.jsonl` literally
  contains every atom's summary text, a real corpus's own memory export let
  the "naive" baseline win by reading Meristem's memory instead of searching
  code (`__pycache__` and other build/VCS noise had the same problem, just
  less dramatically); (2) once `REAL_WORLD_QUESTIONS` existed as Python data
  inside `eval_harness.py` itself, grep matched the *benchmark's own source*
  for its own gold answers — no real developer grepping their own question
  would ever stumble onto this script. Both are now excluded (`NOISE_DIRS`,
  `_SELF_PATH`) unconditionally, a no-op for the synthetic corpus (a fresh
  temp dir has neither) but load-bearing for any real-repo run. Still a
  standalone script like `replay_retrieval.py` — not wired into CI, by
  design, same as that tool; `REAL_WORLD_QUESTIONS` is intentionally small
  and expected to need occasional re-verification as this repo's own atoms
  evolve, surfacing as a lower number on the next manual run, not a red build.
- **Observability.** `meristem why <atom>` prints the full provenance trail — makes
  the no-hallucination claim demonstrable; the best demo surface.
  **Status: IMPLEMENTED (2026-08-11).** Resolves either an atom id or a
  topic_key (reports and lists ids if a topic_key is ambiguous across types
  rather than guessing). Prints source (`source_kind`/`source_ref`,
  asserted/valid-from timestamps, confidence, decision status/class where
  applicable), a liveness check **re-run at print time** — never read from a
  cached `liveness_last_ok` — so the output cannot claim more confidence than
  the atom currently earns, and every live edge touching the atom (direction,
  kind, weight, `edge_evidence` rows) with the neighbor's topic_key resolved.
  Also exposed as the `why(ref)` MCP tool (`mcp_server.py`), same resolution
  and re-check semantics as the CLI, JSON-shaped instead of printed — closing
  the last CLI/MCP asymmetry this pillar had.

Highest leverage if forced to pick two: **§14.1 hierarchical atoms** (or it
won't scale) and **§16 eval harness** (or no one will believe it scales).

## 17. Prior art & licensing (HippoRAG)

Meristem's retrieval core (§4) is an **independent implementation of the
HippoRAG idea** (Gutiérrez et al., *HippoRAG: Neurobiologically Inspired
Long-Term Memory for LLMs*, NeurIPS 2024, arXiv:2405.14831): seed a query into a
knowledge graph and rank by Personalized PageRank. It is **not a fork or
derivative of the HippoRAG codebase.** Verified divergence (checked 2026-05-27):

| | HippoRAG (`OSU-NLP-Group/HippoRAG`) | Meristem (`retrieval.py`) |
|---|---|---|
| PPR engine | `igraph.personalized_pagerank(..., implementation='prpack')` | hand-rolled power iteration, no graph lib |
| Graph store | in-memory `igraph.Graph` | typed atoms + edges in SQLite |
| Nodes | LLM-extracted entities / passages from documents | typed atoms (invariant, decision, …) |
| Edges | synonymy / fact / passage edges | 12 closed kind-weighted edge types (§4) |
| Domain | document QA / multi-hop retrieval | live code memory with liveness invalidation |
| Key symbols | `HippoRAG.run_ppr`, `graph_search_with_fact_entities` | `retrieve`, `_seed_set`, `_load_edges` |

**Licensing posture (both MIT):** HippoRAG is MIT (© 2025 OSU Natural Language
Processing); Meristem is MIT. Reimplementing a *published algorithm* in original
code is standard, legal practice — copyright protects expression (their source,
their text), not the idea. PageRank's original Stanford patent (US 6,285,999)
expired in 2019; Personalized PageRank is a long-standing academic variant. No
HippoRAG-specific patent surfaced in search (NeurIPS publication + MIT code
release signal no enforcement intent). Because no HippoRAG source was copied,
even the MIT attribution clause is not triggered — but we attribute the idea
anyway in good faith (README + this section). *Not legal advice; a patent
sanity-check and, if commercializing, a brief IP-attorney consult remain prudent.*

---

## 18. The write path — conversation capture (2026-08-05)

### 18.1 The defect

Every earlier section describes how Meristem *reads*. Nothing described how it
learns anything a repository does not already state, and the answer turned out
to be: it did not. Measured on a live workspace after ten weeks of daily use:

| source | atoms |
|---|---|
| git ingester (`decision_class = change`) | 317 |
| human, via `meristem decide` (`constraint`) | **2** |

Everything Meristem captured for free was re-derivable from the repo in
milliseconds — commits, primary keys, symbols. Everything that was *not*
derivable required stopping mid-thought to type a CLI command, so it never
happened. The substrate was a git mirror with extra steps.

The knowledge was not missing from the session. It was in `retrieval_log` —
paraphrased below to the same grammatical shape without the real workspace's
own domain content:

> "the nightly job runs for every environment except staging"
> "if a worker has no active lease then drop it from the pool"
> "every worker gets a daily 2 hour maintenance window on weekdays"

Meristem had read all 190 of those — **as queries**. Never as facts.

### 18.2 Extraction proposes, a human disposes

Capture is marker-based and stdlib-only: it scores each clause for the grammar
of a standing rule (universal quantifiers, exceptions, prohibitions, policy
modals, present-tense copula) and penalises the grammar of a one-off task
(leading imperative, deictic reference). No LLM, no model load, no network.

That is deliberately lower-precision than an LLM extractor, and affordable only
because nothing it produces is trusted. Candidates land in a `candidates` table
that `retrieve()` cannot see. A candidate is not memory. `meristem review` is the
gate; `--reject` is permanent, keyed on a normalised-text fingerprint, so a
sentence rejected once never returns however often it is said again.

Tencent's comparable layer runs an LLM extraction pass every five turns. Meristem
already sits inside the agent loop as a hook, so it pays nothing per turn — but
an extractor that itself called a model would reintroduce exactly the cost that
left `meristem route` (11s, model load per call) unwired for two months.

### 18.3 What the corpus forced

Three defects only a real corpus could surface, each measured with
`tools/replay_capture.py`:

| defect | before | after |
|---|---|---|
| Machine-authored turns — hooks, skills and subagent verdicts inject their output *as user turns* ("A session-scoped Stop hook is now active…", "Transcript evidence shows: (1)…"). Meristem learning from its own output. | 237 unique candidates across 278 transcripts | **59** |
| Pasted documents — a skill doc mined line by line ("Never use ease-in for UI animations"). The document's rules are not the project's rules. | 34 candidates from 37 messages | 5 |
| Prose-only pastes with no markdown furniture to detect. | one 8042-char paste leaking 4 | 1 |

The surviving tell for machine text is grammatical person: a user says "we never
deploy on fridays"; a machine says "the assistant", "the transcript", "the
condition". Third-person process narration is never a project rule.

Final rate: **59 unique candidates from 2108 human messages (2.9%)** across 278
real transcripts. Precision is roughly half — acceptable *because* of the review
gate, and not otherwise.

### 18.4 Reconfirmation horizons

Liveness (§14.5) answers "does the code still say this?". For a domain rule
there is no code to ask — no regex verifies "the nightly job runs for every
environment except staging", and it will still be asserted long after it stops
being true.

`atoms.confirm_by` is the honest substitute. Past the horizon the atom surfaces
**marked** `unconfirmed`, never silently trusted and never silently dropped —
the same principle as `unverifiable`: discarding knowledge to keep the report
clean is the failure this system exists to avoid. Default 90 days, matching the
retrieval decay half-life so a fact fades from ranking and falls due for
reconfirmation on the same clock. `meristem doctor` counts them on their own line.

### 18.5 Surfaces

`meristem capture --transcript <path>` (Stop hook), `meristem propose "<text>"`,
`meristem review [--accept|--reject <id>|--noise|--reject-noise]`, MCP `propose_fact`. The SessionStart
digest carries `pending_review`, because a queue nobody is told about is a queue
nobody empties — the same failure that left `meristem doctor` unread for two months.

**Principle:** the cheapest moment to record why something is true is the moment
someone says it. A memory layer that cannot learn from its own conversation is
an index, not a memory.

### 18.6 Precision measured from review labels (2026-10-01)

Every accept and reject is a label. Across the onboarded workspaces on the dev
machine that was **26 accepted vs 95 rejected** — precision about 21% — and all
121 still passed `capture.extract()`. A queue that is four-fifths noise teaches
the user to stop reviewing, which starves the one write path for non-derivable
knowledge. The corpus is private and never copied into the repo; the rules are
tested on synthetic sentences of the same grammatical shape, and
`tools/replay_capture.py --labels DB [DB ...]` re-measures locally, printing
**counts only, never candidate text**.

Hard drops (each removed rejected candidates and lost no accepted one):

| rule | drops | rejected removed |
|---|---|---|
| `leading-quote` | unit opens with `[`, `"`, `'`, `“` — a quoted template or condition | 15 |
| `narration` | agent narration: `transcript … provides/shows/evidence/indicates`, `evidence of` | 9 |
| `first-person` | `i think/guess/wish/am/was/was wondering/have gotten`, `want u` | 5 |
| `meta-conversation` | `this/the current conversation/session/chat`, `your review`, `memory note`, `waiting on you` | 3 |
| `verbless-fragment` | under 60 chars, opens on for/and/or/but/with/on/in/to/of/by, no modal or copula | 1 |

Soft penalties in `score_unit` (−2 each): `-addressed` for a request aimed at the
agent (`can u …`, `u are …`, `if u …`, `want you to …`, `ur`) — 22 rejected but
also 1 of 26 accepted, so it can only lower a score, never drop a strong rule —
and `-imperative` now also fires behind a discourse lead (`ok now update …`).
`capture.explain(unit)` returns the name of the rule that removes a unit, and is
what `review --noise` and the replay tool both report.

The learning loop: `candidates.queue_many` skips a fact whose token set (lower-
cased `[a-z0-9]+`) has Jaccard ≥ `NEAR_DUP_JACCARD` (0.6) with a **rejected**
candidate, a pending one, or an earlier fact in the same batch (first kept) — 12
of the 95 rejected were re-proposals of a rejected template with small
variations, which exact-fingerprint dedup cannot see. Accepted text is never a
blocker. Exact-fingerprint rejection is unchanged.

`meristem review --noise` lists pending candidates the current filter would not
propose (with the rule); `--reject-noise` rejects exactly those. Both are
explicit human actions — nothing runs automatically, and a candidate is only
ever disposed of by a human. Expected outcome on the measured corpus: precision
about 21% → 28–30%, with at most 1 accepted lost. The remaining rejected
candidates read exactly like accepted product requirements; lexical rules cannot
split those, which is what the learning loop and review UX are for.

### 18.7 Agent suggestions — advisory only (0.3.0)

An agent can read the review queue and say what it would do, so the human's pass
is faster. MCP tool `pending_facts(limit)` returns pending candidates (`id` is the
8-char fingerprint prefix `meristem review` shows, plus text, score,
`proposed_type`, the `noise_rule` that would drop it now, and any earlier
suggestion). `suggest_review(id, verdict, reason)` records `accept` or `reject`
with a reason (whitespace-collapsed, 500 chars) and returns "suggestion only".

The invariant: **a suggestion never disposes of a candidate.** Neither tool changes
`candidates.status`; the candidate stays pending and `meristem review` prints
`agent suggests <verdict>: <reason>` beside it, where a human still accepts or
rejects. Suggestions are stored in a sidecar, `.meristem/review_suggestions.json`
(keyed by full fingerprint, written atomically, gitignored like other per-machine
operational state), not in the schema: they are advisory, per-machine and need no
migration. A missing or corrupt file reads as "no suggestions".

## 19. Freshness — staying level with the repo (2026-08-06)

### 19.1 The defect

§14.5 keeps Meristem from returning facts that are false. §14.6 keeps it from
returning facts that are irrelevant. Neither adds anything the repo grew after
the last `meristem ingest`, and nothing else did either: **liveness can only ever
shrink a store.** Every read path was automatic — SessionStart digest,
UserPromptSubmit router, PostToolUse watcher — while the one path that adds
knowledge from code was a command a human had to remember to type.

Measured on a live workspace on 2026-08-05: last indexed 2026-07-30, against a
HEAD **56 commits ahead**. The store answered every query with unchanged
confidence throughout, because nothing in the system knew the difference.

Wall-clock age could not have caught it, and `meristem doctor` proved it: the check
warned at "not re-indexed in 30 days", which is the wrong question in both
directions. A root indexed 40 days ago with no commits since is perfectly
current. A root indexed this morning can already be 56 commits stale.

### 19.2 Drift is measured in commits

`freshness.RepoDrift` reports, per registered root, `git rev-list --count
<last_indexed_sha>..HEAD`. Four states are kept distinct because collapsing any
pair of them is a lie:

| state | meaning | drifted? |
|---|---|---|
| `n behind` | commits the index has not seen | yes, n > 0 |
| `unknown_base` | the stored sha no longer resolves (rebased, force-pushed) | yes — and **not** catchable up incrementally |
| `never_indexed` | no ingest has ever completed | yes |
| not a git repo / root missing | drift has no referent here | no |

`commits_behind is None` means *unknown*, never *zero*. A sha git cannot resolve
is not a repo that happens to be current, and rendering it as 0 is exactly how a
store that can never be incrementally caught up reports itself healthy.

**Dirty files do not count as drift.** They are reported alongside the number
and `meristem sync` picks them up when it runs, but a dirty working tree is the
normal state of active development — letting it fire the signal would make the
signal constant, and a constant signal is one people stop reading. That is
precisely how `edges.density` went unread while two real workspaces ran edgeless
for months.

### 19.3 `meristem sync`

Incremental (§14.2), debounced, and cheap when there is nothing to do: one
`git rev-list --count` per root, returning before any embedder is loaded. Only a
drifted root pays for ingest.

| surface | flag |
|---|---|
| report only, exit 1 if behind | `--check` |
| JSON for hooks | `--quiet` |
| ignore the debounce window | `--force` |
| install the git triggers | `--install-hook` |

`--install-hook` writes git hooks (originally just `post-commit`; see §19.6 for
the full set) that run `meristem sync` **detached and
fully redirected** — a commit must never wait on, or be failed by, indexing. It
refuses to overwrite a hook it did not write and prints the line to paste
instead. Debounce (default 90s) exists because a 30-commit push fires
post-commit 30 times and should cost one incremental ingest, not thirty. A lock
file makes a doubled fire two cheap no-ops rather than two ingests racing.

The completion message describes the state the sync actually **reached**, not the
one it aimed at: `ingest` withholds `mark_indexed` when work was budget-deferred,
so a sync can complete and leave a root behind, and reporting "index now current"
there would be the confident-wrong answer this surface exists to replace.

### 19.4 The indexed-sha gate, and the deadlock it caused

`ingest` advances `last_indexed_sha` only when a run was complete — otherwise
budget-skipped files would be orphaned behind an advanced sha (§14.2). That
guard originally keyed on `IngestResult.truncated`, which conflated two
different things, and the conflation deadlocked every non-trivial repo:

- **Budget deferral** — the adapter ran out of time. The next run has a fresh
  budget and genuinely can catch up, so holding the sha is correct.
- **A designed cap** — `symbols` stops at 200 atoms by construction (§14.1).
  This recurs identically on every full scan, so holding the sha meant it was
  *never* recorded, so the next run had no base to diff against, so it did
  another full scan into the same cap.

Measured on a 28-commit repo: 316 atoms ingested, `last_indexed_sha` NULL
forever, incremental ingest never once active, and `doctor` reporting
"registered but never ingested — the store is empty" over a populated store.
`IngestResult.budget_deferred` now gates the sha; a designed cap advances it and
is reported out loud instead, because an unreported coverage gap reads as full
coverage. With the fix, a second ingest of that repo drops from 316 atoms to 5 —
the first time §14.2's steady-state O(diff) claim was true in practice.

Related: `doctor`'s `ingest.completed` keyed on `last_indexed_sha`, which is NULL
for every non-git workspace because that is what `head_sha` returns. It now keys
on `last_indexed_at`, which every completed ingest writes regardless of git.

### 19.5 Surfaces

Drift is stated wherever an answer is given, because it qualifies every answer:

| surface | rendering |
|---|---|
| `meristem doctor` | `repo.freshness` — warn when behind, fail at ≥20 commits, at an unresolvable base, or when a registered root is gone |
| `meristem doctor` | also `hooks.git` — warn when nothing is wired to keep the index level (§19.6) |
| SessionStart digest | `staleness` field (`summary` is null exactly when current), rendered in the `Housekeeping:` block (§6) with sync status |
| UserPromptSubmit | `Housekeeping (changed since last shown)` block when drift appears or clears mid-session (§6, §19.6) |
| Memory Pulse | `⟳ N behind`, shown only when non-zero |
| `meristem status` | per-root `drift` column |
| `route()` — UserPromptSubmit | `drift_commits` + `drift_summary`, non-null only when behind (added 2026-08-06) |

The router was missing from this table for as long as the table existed, which
made "stated wherever an answer is given" untrue at the one surface that puts
facts in front of the model. An injected atom is a claim about code as it stood
at the indexed sha; at 56 commits behind it may describe a function that no
longer exists. `route()` reports drift whether or not the gate admitted
anything — silence plus a stale index is a different diagnosis from silence
alone, because the facts for the new code may simply not be indexed yet.

Unmeasurable drift reports as *nothing to say*, never as zero, and `route()`
never raises on it: this path runs inside UserPromptSubmit, where a crash does
not degrade an answer but blocks the user's turn from happening at all.

**Principle:** a memory that cannot say how old it is will be trusted as though
it were current. Abstention (§14.6) taught the reader to attend when Meristem
speaks; saying "I am 56 commits behind" is what keeps that attention honest.

### 19.6 Triggers: who runs sync (2026-09-30)

§19.3 shipped the mechanism and one trigger. A check on 2026-09-30 found the
trigger had quietly stopped working in the repo Meristem is developed in, and
nothing had said so. Two defects, one cause: the trigger assumed it was the only
thing that could move HEAD.

**Defect 1 — hook ownership.** A managed hook baked in a single workspace path
(`[ -f "<ws>/.meristem/atoms.sqlite" ] || exit 0`), and installing overwrote any
managed hook. A repo's hooks are shared by every workspace whose roots include it
(a worktree checkout, a parent workspace, a sibling project), but a hook file is
one file, so the last workspace to run `--install-hook` evicted the rest. When
that workspace was later renamed, its stale path made the guard exit on every
commit: sync was silently disabled, the index drifted, and no check looked.

**Defect 2 — `post-commit` is not how HEAD moves.** The worktree flow is commit
on a branch, then `git merge --no-ff` into main. A clean merge fires `post-merge`,
not `post-commit`; so does `git pull` and a fast-forward. An amend or rebase
fires `post-rewrite`. The managed `post-merge` hook ran only `import`, never
`sync`. The flow that advances HEAD most in practice fired no sync at all.

**The fix, in four parts.**

1. *Multi-workspace managed hooks.* Each managed hook carries a list of
   `actions|workspace` lines (actions ⊆ {`import`, `sync`}) in a quoted heredoc
   (`<<'MERISTEM_WORKSPACES'`), so the shell never expands or word-splits a path —
   workspaces live under directories with spaces in them. Installing adds this
   workspace's actions to the list, unioned with what is already there, so
   `import --install-hook` never drops a sync step or the reverse. Entries whose
   `.meristem/atoms.sqlite` no longer exists are pruned at install; at run time
   a missing workspace is skipped. A pre-list single-workspace hook is parsed
   (`git_utils.managed_hook_workspaces`) and rewritten in place, not orphaned. A
   hook Meristem did not write is still left alone, with the line to paste.
2. *More events.* `sync --install-hook` installs `post-commit`, `post-rewrite`
   and a sync step in `post-merge`; `import --install-hook` installs `post-merge`
   and `post-checkout` (§16). `post-merge` therefore runs `import` then `sync`
   when both are wired. All keep the original guarantees: detached, fully
   redirected, `unset GIT_*` (git exports `GIT_DIR`/`GIT_INDEX_FILE` into hooks,
   and an inherited index path would make a sync of another root read this repo's
   index), and never failing git.
3. *`doctor` `hooks.git`.* Read-only. Per configured git root it warns if there is
   no managed `post-commit` hook, if that hook does not list this workspace with
   a `sync` action, or if it lists a workspace whose store is gone. The summary
   names the fix, `meristem sync --install-hook`. Roots that are not git repos
   are skipped; `repo.freshness` already reports a missing root. This is the
   check whose absence let defect 1 run unseen.
4. *Session-start sync.* Git hooks only cover commits made where they are
   installed — not a pull done on another machine, not a checkout from before the
   hook existed. So when SessionStart (or, once the notice changes, a later
   UserPromptSubmit) sees the index behind HEAD it spawns `meristem sync --quiet`
   in its own session, all stdio on `/dev/null`: fire-and-forget, so it can
   neither block the hook nor write into the hook's JSON stdout, and it never
   raises. The debounce window and the sync lock (§19.3) make a spawn that
   races a git hook a cheap no-op. The staleness notice is **kept**, annotated
   `background sync started; answers may lag until it finishes` — a sync may be
   debounced or not finish, and the session must still know its answers can
   predate HEAD. Disable with `[freshness] auto_sync_on_session_start = false`;
   the notice then reads `run meristem sync`. An unresolvable base
   (`unknown_base`) or a missing `meristem` binary yields the same plain notice.

**One nudge per stale episode.** The re-surfacing rule is in §6. It is stated
here because it follows from the same measurement: sync lags a commit by a
moment, so a nudge keyed on the commit count would fire after every commit of
an active session and be learned as noise (§19.2's argument against counting
dirty files, again). Candidates are not touched by `ingest` or `sync` — a
captured fact becomes memory only when a human accepts it (§18.2) — and a test
holds that line.

## 20. Trust guard (0.3.0)

Memory that leaves the machine, or that nobody typed as a durable fact, must not
carry credentials or personal data. `guard.py` is a deterministic, stdlib-only
scanner: `scan(text, policy)` returns findings (kind, span), `kinds_in` the kinds
only, `redact` masks. It never returns, logs or stores a matched value.

Kinds. Secrets: `private-key`, `aws-access-key`, `aws-secret-key`, `github-token`,
`slack-token`, `anthropic-key`, `openai-key`, `google-api-key`, `jwt`,
`url-credentials`, `credential-assignment`, `high-entropy`. Personal: `email`,
`phone`, `excluded-term` (the workspace's `[capture] exclude_terms`). Rules are
precision-first: hashes, UUIDs, version strings, credential-free URLs, dotted
identifiers and templates/references (`$VAR`, `{{x}}`) pass.

Enforcement points:

1. **Capture.** A unit that trips the guard is dropped under the rule name
   `guard` (`capture.explain`), so it is never queued.
2. **Export.** `export_jsonl` / `export_jsonl_sharded` withhold an atom whose topic
   or any summary trips the guard, with its summaries, every touching edge and
   those edges' evidence; a lone tripping evidence row is withheld alone. The
   caller receives sorted `(atom_id, kind)` pairs, and `meristem export` prints
   them. Ids and kinds only.
3. **Doctor.** Check `guard.store` warns with the count of live atoms carrying
   findings (ids and kinds in the detail). Such atoms remain in the local store.

Config: `[guard] enabled = true`, `allow_patterns = []` (regexes, `re.search`
against the matched text, whitelisting a known-safe match). Limits: pattern-based,
so an unusual secret shape can pass; it scans memory, not the repository source;
it does not clean the local store.

## 21. Setup — wiring agents (0.3.0)

`meristem setup [--dry-run] [--agents a,b] [--yes]` detects coding agents (config
dir under HOME, or binary on PATH: `claude`, `cursor`, `codex`, `windsurf`,
`gemini`), plans, prints, and applies after confirmation. Logic is in `setup.py`
(planning and applying only; `cli.py` owns output and the git-hook installer).

Rules: an agent is *written* only where its format is documented in this repo
(the JSON `mcpServers` shape); otherwise a snippet is printed and nothing is
written. Today: Claude Code (hooks into `.claude/settings.json`, entry into
`.mcp.json`), Cursor (`.cursor/mcp.json`) and Windsurf (`mcp_config.json`, with
`cwd` set to the workspace) are written; Codex (TOML) and Gemini CLI (JSON) get
snippets only. Merge, never overwrite: only `mcpServers.meristem` is added, and a
differing existing entry is left alone. A file that is not a valid JSON object is
not touched. The first change to an existing file copies it to
`<file>.meristem-bak`, and an existing backup is never replaced. `--dry-run`
computes the identical plan and writes nothing. Re-running reports `unchanged`.
In a workspace it also installs the sync git hooks (§19.6); outside one, project
steps are skipped with a "run `meristem init` first" note. The server entry is
`meristem mcp`, or `uvx --from meristem[mcp] meristem mcp` when only `uvx` is
available.

Related onboarding behaviour: `meristem init` imports the shared export
(`[sync] export_path` or the sharded `export_dir`) into a store that is still
empty, and reports the count; atoms imported this way have no vectors until
`meristem embed` or `meristem sync`.

## Design status

All open questions resolved (2026-05-22). Spec is build-ready. Implementation
progresses task-by-task per the task list (`TaskList` in this session, or
`.meristem/state/tasks.json` in production). Sections 14–17 (scaling, planning,
adoption, prior art) added 2026-05-27 as the post-v1 roadmap. §14 (scaling) is
done through §14.8; §15 (planning mode, `meristem plan`) is implemented
(2026-08-14). §16 (adoption) is now fully IMPLEMENTED across all five
pillars — tree-sitter symbol graphs (2026-08-12), team sync protocol
(2026-08-12, extended 2026-08-18, 2026-08-19), eval harness (2026-08-07,
extended 2026-08-14), observability
(2026-08-11), and agent-agnostic MCP (2026-08-17) — each with its own
"Not yet done" caveats noted inline under §16 rather than repeated here.

§16 being IMPLEMENTED is a code milestone, not an adoption or stability one.
