# 3. The edge graph

## Edges connect atoms

Atoms (data model section) are inert alone. The `edges` table (`schema.sql`)
turns them into a graph: `(src_id, dst_id, kind, directed, weight,
confidence, valid_from, valid_to, source, status)`, `kind` closed by a
`CHECK` to twelve values, `source` closed to seven provenance tags
(`shared_ref`, `embed_sim`, `commit_couple`, `llm_extract`, `git_blame`,
`manual`, `schema_diff`). Edges are bi-temporal (`valid_to IS NULL` = live)
and carry `status ∈ {live, suggested, rejected}`. `edges.upsert_edge` is
idempotent — a repeat `(src, dst, kind)` refreshes weight/confidence by
max-merge rather than duplicating the row.

## Edge kinds

- **MIRRORS** — same symbol name across more than one repo root.
  `discovery._rule_mirrors`, symmetric.
- **REFERENCES** — atom's summary text mentions a shared file path.
  `discovery._rule_references`, weight 0.6.
- **CO_CHANGED** — two files edited in the same commit ≥3 times, mined over
  the last 500 commits. `discovery._rule_co_changed`, symmetric.
- **OWNS** — person/role → file, from `git blame`. `ingesters/owners.py`,
  directed, weight 0.7.
- **BLOCKS** — a decision forbids changes under a path prefix.
  `edges.link_blocks`, via `meristem decide --blocks` and the MCP write
  path, directed, manual, weight 0.9.
- **ROLLS_UP** — file/symbol atom → its module parent. `ingesters/modules.py`,
  directed child→module, weight 0.6.
- **CONTRADICTS** — two live atoms disagree. Raised by `assert_fact`
  (`atoms.py`) on a diverging supersede, and by `sync_protocol.py` when a
  merge reconciles clashing `topic_key`s. Symmetric, `source="llm_extract"`.

Five more kinds exist in the schema and in `retrieval.EDGE_WEIGHTS` with no
producer yet. **IMPLEMENTS** (code symbol fulfills an invariant/decision)
is the target of the LLM-extraction rule SPEC §4 marks "not built."
**DERIVED_FROM** (summary 50w ← 250w, or atom ← source doc) and
**DEPENDS_ON** (runtime/build dependency) simply have no ingester wired to
emit them yet. **LOCATED_IN** (atom → file path) is unwired too —
`_seed_set` uses `source_ref` as a stand-in. **SUPERSEDES** lives on the
atom as `superseded_by` and is never materialized as an edge.

## Directed vs. symmetric

`edges.SYMMETRIC_KINDS = {CONTRADICTS, CO_CHANGED, MIRRORS}`; every other
kind is directed. For a symmetric kind, `_canonical_endpoints` sorts
`(src, dst)` before insert so `(a,b)` and `(b,a)` collapse to one row
regardless of discovery order.

MIRRORS earned its place the hard way: `_rule_mirrors` emits pairs via
`combinations()` over id-sorted atoms, so which side lands in `src` vs.
`dst` is an artifact of ordering, not a real direction — but until a
2026-08-11 fix, `SYMMETRIC_KINDS` listed only `CONTRADICTS`/`CO_CHANGED`,
so MIRRORS was stored `directed=1`. `_load_edges` only adds the reverse hop
for undirected edges, so a query seeded from the *dst* side had no edge to
walk at all. Both directions are now regression-tested.

## Weights

Each edge carries its own `weight ∈ [0,1]`, set by its producer: flat 0.6
for REFERENCES, 0.7 for OWNS, 0.9 for BLOCKS, 1.0 (the `upsert_edge`
default) for CONTRADICTS. MIRRORS ramps from 0.8 (group size 2) down to a
floor of 0.4 as the mirror group grows toward the 8-repo cap
(`_mirror_weight`, graded rather than flat). CO_CHANGED is the one weight
computed empirically: `weight = min(1.0, count / 10.0)`, `count` being how
many commits touched both files together.

Separately, `retrieval.EDGE_WEIGHTS` sets a *kind* weight used only during
PPR transition: MIRRORS 1.0, IMPLEMENTS/BLOCKS 0.9, CONTRADICTS 0.8,
SUPERSEDES 0.7, DERIVED_FROM/ROLLS_UP/LOCATED_IN 0.6, REFERENCES/DEPENDS_ON
0.5, OWNS/CO_CHANGED 0.4. `_load_edges` multiplies stored weight by this,
then row-normalizes each node's outgoing edges to sum to 1.

## Retrieval: Personalized PageRank

`retrieval.retrieve` seeds a personalization vector from top-K embedding
hits (cosine similarity, restricted to vectors from the query's own
embedding model) plus structural hits — atoms whose `source_ref` matches a
file in `file_context` — normalized to sum to 1. `_forward_reachable` walks
live edges outward from those seeds up to `MAX_HOPS` (3) to build the
candidate universe before PPR runs — bounding cost the way tiers bound atom
counts, so a high-degree hub can't pull the whole graph in. PPR runs at
`α=0.15` for up to 20 iterations, early-exiting once the largest rank delta
drops below `1e-6`; dangling mass leaks back to the seed vector. A
drill-down pass can then expand the top 3 module atoms by mass into their
`ROLLS_UP` children and re-rank.

This is "HippoRAG-style" because it follows that paper's shape: retrieval
isn't nearest-neighbor lookup, it's PageRank over a typed graph seeded by
embeddings, so an atom several hops away by a strong edge (a BLOCKS
decision, a MIRRORS partner) can outrank a closer but structurally isolated
embedding match. SPEC §17 credits HippoRAG as prior art.

## The abstention gate

PPR score orders atoms within one query but carries no information about
whether any of them answer it. Measured directly
(`tools/replay_retrieval.py`, 198 prompts over a 607-atom workspace): top-1
PPR score averaged 0.1124 for a nonsense query against 0.1003 for a genuine
one — nonsense scored *higher*, and no floor between 0 and 0.02 separated a
single query. Query↔atom cosine separated them cleanly instead: 0.58–0.67
for nonsense vs. 0.83–0.84 for genuine.

The fix is `Retrieved.relevance`: cosine between the query vector and each
returned atom's own stored vector, computed only over the ≤`top_n` atoms
being emitted. `retrieval.gate` partitions hits into `kept` / `suppressed` /
`scaffolding` against a `min_relevance` floor — 0.70, calibrated for the
`bge-small-en-v1.5` embedder, re-derived per embedder — and separately
drops scaffolding: module-tier atoms (spent once drill-down has used them)
and test-file symbol atoms (which embed deceptively well without answering
anything). `relevance = -1.0` (no comparable vector under the querying
model) fails the gate by construction: unknown is irrelevant, because
unknown-as-relevant is what let a cross-model workspace serve confident
noise for two months.

One `gate()` backs all three retrieval surfaces: `route()` drops silently,
`query_facts` drops but reports the suppressed/scaffolding/top-relevance
counts, `meristem query` shows everything with rejects marked. A query that
misses the floor is "silenced" — logged, with `top_relevance` recording how
close it came, so silence stays auditable rather than indistinguishable
from a broken hook.
