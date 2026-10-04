"""Personalized PageRank retrieval over the typed atom graph (SPEC §4).

The retrieval contract is HippoRAG-style: a query produces a *seed set*
(top-K embedding hits plus structural hits like atoms LOCATED_IN the
current file). We then run Personalized PageRank with α=0.15, 20
iterations, using a *kind-weighted* transition matrix so high-signal
edges (MIRRORS, IMPLEMENTS) carry more probability mass than weak ones
(REFERENCES, CO_CHANGED). Results are ranked by graph-flow, surfacing
BLOCKS and MIRRORS partners that pure k-NN misses.

Each result carries a provenance trail (depth + via_kind + via_edge)
so the caller can render the "Surfaced because 2 hops from
auth_service.py via MIRRORS" citation line from SPEC §4.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import edges as edges_mod
from . import embeddings, liveness

# Kind-weighted transition mass (SPEC §4). Higher = more probability flows
# along this edge kind during PPR.
EDGE_WEIGHTS: dict[str, float] = {
    "MIRRORS":     1.0,
    "IMPLEMENTS":  0.9,
    "SUPERSEDES":  0.7,
    "CONTRADICTS": 0.8,
    "BLOCKS":      0.9,
    "DERIVED_FROM": 0.6,
    "ROLLS_UP":    0.6,   # SPEC §14.1 — module↔child hierarchy mass
    "REFERENCES":  0.5,
    "LOCATED_IN":  0.6,
    "DEPENDS_ON":  0.5,
    "OWNS":        0.4,
    "CO_CHANGED":  0.4,
}

ALPHA = 0.15  # teleportation probability — SPEC §4
ITERATIONS = 20
DRILL_MODULES = 3  # SPEC §14.1 — how many hot modules to drill into
MAX_HOPS = 3       # SPEC §14.4 — cap BFS depth so a hub can't pull in the graph
EPSILON = 1e-6     # SPEC §14.4 — PPR early-exit when the max rank delta < ε
DECAY_HALF_LIFE_DAYS = 90.0  # SPEC §14.5 — score halves every half-life of staleness
VERIFY_FRESHNESS_SECONDS = 3600  # SPEC §14.5 — trust a liveness check newer than this


@dataclass
class Retrieved:
    atom_id: str
    score: float
    depth: int
    via_kind: str | None
    via_edge_id: int | None
    seed: bool  # was this atom in the seed set?
    # How well Meristem can vouch for this atom right now (SPEC §3):
    # "verified" (predicate ran and matched), "unverifiable" (predicate could
    # not be run), "none" (no predicate), or "trusted" (a recent pass was
    # cached rather than re-run). Callers MUST be able to distinguish these —
    # an atom nothing could check must not read like one that passed.
    liveness_state: str = "none"
    # Cosine between the query and THIS atom's own vector — the only number in
    # this dataclass that measures relevance. `score` is a PPR mass share: it
    # ranks atoms against each other within one query but is not comparable
    # across queries, so it cannot answer "is any of this worth injecting?".
    # -1.0 means unknown (no query vector, or no vector stored for this atom
    # under the querying model) and MUST NOT be read as "irrelevant".
    relevance: float = -1.0

    @property
    def relevance_known(self) -> bool:
        return self.relevance >= 0.0

    def trail(self) -> str:
        if self.seed:
            return "seed"
        if self.via_kind:
            return f"{self.depth}-hop via {self.via_kind}"
        return f"{self.depth}-hop"

    @property
    def unverified(self) -> bool:
        """True when this atom's claim could not be checked against the code."""
        return self.liveness_state == "unverifiable"


# ---------------------------------------------------------------------------
# The relevance gate (SPEC §14.6) — one implementation, every surface
# ---------------------------------------------------------------------------
#
# The gate used to live in `router.route`, which meant it only covered the
# UserPromptSubmit hook. `meristem query` and MCP `query_facts` call `retrieve`
# directly, so they served ungated, unfiltered results for as long as the gate
# has existed — including the module-tier scaffolding atoms that win the top
# slot on most queries by being the highest-degree nodes in the graph.
#
# Three surfaces disagreeing about what "relevant" means is how a memory layer
# becomes untrustworthy: the same query answered two ways, depending on which
# door you came in. The predicate and the arithmetic now live here; the surfaces
# differ only in what they DO with the verdict, which is a policy question they
# are each entitled to answer differently:
#
#   route()      — silently drops. It injects into a prompt nobody asked to see.
#   query_facts  — drops, and reports the count. Its caller is an agent that
#                  will treat whatever comes back as authoritative.
#   meristem query   — shows everything, marks what failed. A human typed the query
#                  and is entitled to see what was considered and rejected.

SCAFFOLDING_PREFIX = "module:"

# symbol:<path>:<name> — see the ingester in `ingesters/symbols.py`. Matched by
# position, not a full path parse, so this stays correct even if `path`
# contains characters that would confuse a smarter split.
_SYMBOL_PREFIX = "symbol:"

# Common test-file conventions across the languages Meristem ingests. A path
# match is intentionally broader than a filename-only match — "tests/" or
# "__tests__/" anywhere in the path catches nested suites a prefix/suffix
# check would miss.
_TEST_PATH_MARKERS = ("tests/", "test/", "__tests__/", "spec/")
_TEST_FILE_PREFIXES = ("test_", "Test")
_TEST_FILE_SUFFIXES = ("_test.py", "_test.go", ".test.ts", ".test.tsx",
                        ".test.js", ".spec.ts", ".spec.js")


def _is_test_symbol(topic_key: str) -> bool:
    """True for a `symbol:` atom sourced from a test file.

    A glossary atom for a test function only asserts "a function with this
    name exists" — no more informative than the module-tier atoms this
    predicate already drops, but immune to that check because it isn't a
    module atom. Test names are also written close to natural language
    (`test_rejected_edge_status_is_sticky_across_merge`), so they embed
    deceptively well against a substantive question and can clear the
    relevance floor while answering nothing (first observed live: a "how
    does the merge policy handle X" query returned three bare test names
    and zero atoms describing the policy itself)."""
    rest = topic_key[len(_SYMBOL_PREFIX):]
    path, _, _ = rest.rpartition(":")
    if not path:
        return False
    lower = path.lower()
    if any(marker in lower for marker in _TEST_PATH_MARKERS):
        return True
    name = path.rsplit("/", 1)[-1]
    if name.startswith(_TEST_FILE_PREFIXES):
        return True
    return name.endswith(_TEST_FILE_SUFFIXES)


def is_scaffolding(topic_key: str | None) -> bool:
    """True for atoms that exist to make retrieval work, not to inform a reader.

    Two independent sources of the same symptom — an atom that wins a query
    on graph position or embedding similarity without carrying an answer:

    Module-tier atoms ("Source module `migrations/` — a top-level subsystem")
    are how tier-aware drill-down finds a hot subsystem and pulls its children
    into the candidate set (SPEC §14.1). By the time results are handed to a
    consumer that expansion has already happened, so the module atom itself is
    spent — and being the highest-degree node in the graph, it otherwise wins
    the top slot on most queries.

    Symbol-tier atoms for test functions/classes carry no information beyond
    "this identifier exists in a test file" — see `_is_test_symbol`.
    """
    key = str(topic_key or "")
    if key.startswith(SCAFFOLDING_PREFIX):
        return True
    if key.startswith(_SYMBOL_PREFIX):
        return _is_test_symbol(key)
    return False


@dataclass(frozen=True)
class Gated:
    """The verdict of the relevance gate over one query's hits."""

    kept: list[Retrieved] = field(default_factory=list)
    # Hits that cleared scaffolding but fell below the floor. Reported, never
    # merely dropped: silence must be auditable or it is indistinguishable from
    # a broken hook (SPEC §14.6).
    suppressed: list[Retrieved] = field(default_factory=list)
    # Hits dropped as retrieval machinery. Counted separately from `suppressed`
    # because they are not a relevance judgement — a module atom is filtered no
    # matter how well it matches.
    scaffolding: list[Retrieved] = field(default_factory=list)
    # Best relevance among non-scaffolding hits, BEFORE the floor was applied.
    # -1.0 means no hit carried a comparable vector. This is the number that
    # records how close a silenced query came.
    top_relevance: float = -1.0
    min_relevance: float = 0.0

    @property
    def suppressed_count(self) -> int:
        return len(self.suppressed)

    @property
    def scaffolding_count(self) -> int:
        return len(self.scaffolding)

    @property
    def silenced(self) -> bool:
        """True when atoms were retrieved and every one of them was dropped."""
        return not self.kept and bool(self.suppressed)


def gate(
    hits: Iterable[Retrieved],
    *,
    min_relevance: float,
    topic_of: Callable[[str], str | None] | None = None,
    drop_scaffolding: bool = True,
) -> Gated:
    """Partition `hits` into kept / suppressed / scaffolding.

    `topic_of` maps an atom_id to its topic_key; without it the scaffolding
    filter cannot run and is skipped rather than guessed at.

    An atom with `relevance == -1.0` (unknown — no comparable vector under the
    querying model) fails the gate. Unknown-as-irrelevant is deliberate: it is
    the exact state that let a cross-model workspace serve confident noise for
    two months. Callers that genuinely want everything pass a floor below -1.0.
    """
    kept: list[Retrieved] = []
    suppressed: list[Retrieved] = []
    scaffolding: list[Retrieved] = []
    top = -1.0
    for h in hits:
        if drop_scaffolding and topic_of is not None and is_scaffolding(topic_of(h.atom_id)):
            scaffolding.append(h)
            continue
        # Measured over non-scaffolding hits only: a module atom's cosine is
        # not an answer to the question "did this workspace know anything?".
        top = max(top, h.relevance)
        (kept if h.relevance >= min_relevance else suppressed).append(h)
    return Gated(
        kept=kept,
        suppressed=suppressed,
        scaffolding=scaffolding,
        top_relevance=top,
        min_relevance=min_relevance,
    )


def _seed_set(
    conn: sqlite3.Connection,
    *,
    file_context: list[str] | None,
    k: int,
    qvec: list[float] | None,
    model: str | None,
) -> dict[str, float]:
    """Build the personalization vector — atom_id → probability mass.

    Note that the returned mass is normalised, which destroys the absolute
    similarity: a query matching nothing produces the same *shape* of seed
    vector as one matching perfectly. Callers that need to know how good the
    match actually was must keep the raw cosines — see `Retrieved.relevance`.
    """
    seeds: dict[str, float] = {}

    # 1) Embedding hits — top-K by cosine, weighted by similarity. Restricted
    # to vectors from THIS embedder: a cosine between two different models'
    # vectors is arithmetically fine and semantically meaningless, and both
    # embedders emit 384 dims so nothing else catches the mismatch.
    if qvec is not None:
        for atom_id, sim in embeddings.top_k(conn, qvec, k=k, model=model):
            if sim > 0:
                seeds[atom_id] = max(seeds.get(atom_id, 0.0), sim)

    # 2) Structural hits — atoms LOCATED_IN any provided file path. We use
    # the atom's `source_ref` as a coarse proxy since LOCATED_IN edges are
    # populated by the post-ingest edge-discovery pass (not yet wired).
    if file_context:
        placeholders = ",".join("?" for _ in file_context)
        rows = conn.execute(
            f"SELECT id FROM live_atoms WHERE source_ref IN ({placeholders})",
            file_context,
        ).fetchall()
        for r in rows:
            seeds[r["id"]] = max(seeds.get(r["id"], 0.0), 1.0)

    if not seeds:
        return seeds
    total = sum(seeds.values()) or 1.0
    # Sorted by atom id: this dict's own order reaches the provenance BFS
    # queue (which breaks equal-depth ties) and the leak redistribution in
    # `_run_ppr`, and it is built from an unordered SQL read plus a top-k whose
    # own ties are arbitrary. Pinning it here keeps both deterministic.
    return {k_: v / total for k_, v in sorted(seeds.items())}


def _load_edges(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str, int, float]]]:
    """Adjacency list: src → [(dst, kind, edge_id, weight*edge_kind_weight)].

    Symmetric edges add both directions, and so do the directed kinds in
    `edges.REVERSE_TRAVERSABLE_KINDS` (BLOCKS) — stored one way, walkable both,
    so a decision is reachable from the code it constrains. Edge ``weight`` is
    multiplied by the kind weight so a low-confidence MIRRORS edge still beats
    a high-confidence CO_CHANGED edge.
    """
    out: dict[str, list[tuple[str, str, int, float]]] = {}
    rows = conn.execute(
        """SELECT id, src_id, dst_id, kind, directed, weight
             FROM edges
            WHERE valid_to IS NULL AND status = 'live'
            ORDER BY id"""
    ).fetchall()
    for r in rows:
        kind_w = EDGE_WEIGHTS.get(r["kind"], 0.3)
        w = r["weight"] * kind_w
        out.setdefault(r["src_id"], []).append((r["dst_id"], r["kind"], r["id"], w))
        if not r["directed"] or r["kind"] in edges_mod.REVERSE_TRAVERSABLE_KINDS:
            out.setdefault(r["dst_id"], []).append((r["src_id"], r["kind"], r["id"], w))
    # Row-normalize so each row sums to 1.
    for src, neighbors in out.items():
        total = sum(w for _, _, _, w in neighbors) or 1.0
        out[src] = [(d, k, eid, w / total) for d, k, eid, w in neighbors]
    return out


def _forward_reachable(
    seeds: dict[str, float],
    adj: dict[str, list[tuple[str, str, int, float]]],
    *,
    max_hops: int = MAX_HOPS,
) -> set[str]:
    """Universe = seeds ∪ everything within `max_hops` live-edge hops of them.

    The hop cap (SPEC §14.4) keeps the candidate universe bounded regardless of
    repo size — a high-degree hub can't drag the whole graph into the PPR.
    """
    universe: set[str] = set(seeds)
    frontier = list(seeds)
    hops = 0
    while frontier and hops < max_hops:
        hops += 1
        nxt: list[str] = []
        for node in frontier:
            for dst, *_ in adj.get(node, ()):
                if dst not in universe:
                    universe.add(dst)
                    nxt.append(dst)
        frontier = nxt
    return universe


def _run_ppr(
    universe: set[str],
    seeds: dict[str, float],
    adj: dict[str, list[tuple[str, str, int, float]]],
    *,
    alpha: float,
    iterations: int,
    epsilon: float = EPSILON,
) -> dict[str, float]:
    """Personalized PageRank power iteration over `universe`. Dangling mass
    redistributes to the personalization (seed) vector. Stops early once the
    largest per-node rank delta falls below `epsilon` (SPEC §14.4) — typically
    well before `iterations`, since PPR converges geometrically."""
    # Iterate in sorted id order, never set order. `universe` is a set, whose
    # iteration order is hash-randomized per process, and float addition is not
    # associative: summing the same mass in a different order produces scores
    # differing in the last bits. Those near-ties then rank arbitrarily, which
    # is how one identical query returned different atoms across runs (seen as
    # the ablation harness's gate_off recall flipping between 5/6 and 6/6).
    # Pinning the order here makes the scores themselves reproducible.
    nodes = sorted(universe)
    seed_items = sorted(seeds.items())
    rank: dict[str, float] = {a: seeds.get(a, 0.0) for a in nodes}
    for _ in range(iterations):
        nxt: dict[str, float] = {a: alpha * seeds.get(a, 0.0) for a in nodes}
        leak = 0.0
        for node in nodes:
            neighbors = adj.get(node, ())
            mass = rank[node] * (1 - alpha)
            if not neighbors:
                leak += mass
                continue
            for dst, _kind, _eid, w in neighbors:
                if dst in nxt:
                    nxt[dst] += mass * w
        if leak:
            for s, p in seed_items:
                nxt[s] = nxt.get(s, 0.0) + leak * p
        delta = max((abs(nxt[a] - rank[a]) for a in nodes), default=0.0)
        rank = nxt
        if delta < epsilon:
            break
    return rank


def _hot_modules(
    conn: sqlite3.Connection, universe: set[str], rank: dict[str, float], k: int
) -> list[str]:
    """The top-`k` module-tier atoms in `universe` by PPR score (score > 0).

    These are the subsystems the query's mass concentrated on — the only ones
    worth drilling into (SPEC §14.1). Empty when the store has no module atoms,
    which makes drill-down a no-op (full backward compatibility).
    """
    if not universe:
        return []
    ph = ",".join("?" for _ in universe)
    module_ids = {
        r["id"]
        for r in conn.execute(
            f"SELECT id FROM atoms WHERE tier = 'module' AND id IN ({ph})",
            list(universe),
        )
    }
    ranked = sorted(
        ((aid, rank.get(aid, 0.0)) for aid in module_ids if rank.get(aid, 0.0) > 0),
        # Secondary key (atom id) breaks score ties deterministically — set
        # iteration order is hash-randomized, so score-only sort would be flaky.
        key=lambda t: (t[1], t[0]),
        reverse=True,
    )
    return [aid for aid, _ in ranked[:k]]


def _module_children(
    conn: sqlite3.Connection, module_ids: list[str]
) -> list[tuple[str, str, int]]:
    """(module_id, child_id, edge_id) for live ROLLS_UP edges into `module_ids`.

    ROLLS_UP is directed child→module, so children are the *sources* of edges
    whose destination is a hot module — the reverse lookup forward BFS misses.
    """
    if not module_ids:
        return []
    ph = ",".join("?" for _ in module_ids)
    rows = conn.execute(
        f"""SELECT id, src_id, dst_id FROM edges
             WHERE dst_id IN ({ph}) AND kind = 'ROLLS_UP'
               AND valid_to IS NULL AND status = 'live'
             ORDER BY id""",
        module_ids,
    ).fetchall()
    return [(r["dst_id"], r["src_id"], r["id"]) for r in rows]


def _augment_downward(
    adj: dict[str, list[tuple[str, str, int, float]]],
    triples: list[tuple[str, str, int]],
) -> dict[str, list[tuple[str, str, int, float]]]:
    """Return a copy of `adj` with downward module→child ROLLS_UP edges added.

    Base ROLLS_UP is directed child→module (mass flows up). To let a *hot*
    module's mass reach its children during drill-down (SPEC §14.1), we add the
    reverse edge for those modules only and re-normalize the affected rows so
    the row stays a probability distribution.
    """
    rolls_w = EDGE_WEIGHTS["ROLLS_UP"]
    by_module: dict[str, list[tuple[str, int]]] = {}
    for module, child, eid in triples:
        by_module.setdefault(module, []).append((child, eid))

    out = dict(adj)
    for module, kids in by_module.items():
        combined = list(adj.get(module, ())) + [
            (child, "ROLLS_UP", eid, rolls_w) for child, eid in kids
        ]
        total = sum(w for *_, w in combined) or 1.0
        out[module] = [(d, k, e, w / total) for d, k, e, w in combined]
    return out


def _liveness_meta(
    conn: sqlite3.Connection, ids: list[str]
) -> dict[str, tuple[str | None, int | None]]:
    """(liveness_kind, liveness_last_ok) for each id — one query, no I/O."""
    if not ids:
        return {}
    ph = ",".join("?" for _ in ids)
    return {
        r["id"]: (r["liveness_kind"], r["liveness_last_ok"])
        for r in conn.execute(
            f"SELECT id, liveness_kind, liveness_last_ok FROM atoms WHERE id IN ({ph})",
            ids,
        )
    }


def _confirm_horizons(
    conn: sqlite3.Connection, ids: list[str]
) -> dict[str, int | None]:
    """`confirm_by` for each id — one query, no I/O, no predicate execution."""
    if not ids:
        return {}
    ph = ",".join("?" for _ in ids)
    return {
        r["id"]: r["confirm_by"]
        for r in conn.execute(
            f"SELECT id, confirm_by FROM atoms WHERE id IN ({ph})", ids
        )
    }


def _liveness_state_now(
    conn: sqlite3.Connection,
    atom_id: str,
    meta: tuple[str | None, int | None] | None,
    *,
    repo_root: Path,
    now: int,
    freshness: int,
) -> str:
    """Lazy read-time liveness (SPEC §14.5) — returns the atom's current state.

    No predicate → "none". A check newer than `freshness` is "trusted" without
    re-running. Otherwise the predicate runs and we report what happened.

    An un-runnable predicate is *kept* — "can't verify" is not "verified dead" —
    but it comes back as "unverifiable" rather than silently passing. That
    distinction is the point: this function used to swallow NotImplementedError
    and return True, which made an atom nothing had checked look exactly like
    one that passed, in the very system whose promise is that memory it cannot
    stand behind will say so.
    """
    kind, last_ok = meta if meta else (None, None)
    if kind is None or kind == "none":
        return "none"
    if kind in liveness.UNRUNNABLE_KINDS:
        return "unverifiable"
    if last_ok is not None and (now - last_ok) < freshness:
        return "trusted"
    try:
        return liveness.check_atom(conn, atom_id, repo_root=repo_root).state
    except (ValueError, KeyError):
        # Invalid predicate kind or a vanished row: cannot be checked, but must
        # not masquerade as verified.
        return "unverifiable"


def _apply_decay(
    conn: sqlite3.Connection,
    rank: dict[str, float],
    *,
    half_life_days: float,
    now: int,
) -> dict[str, float]:
    """Down-rank stale / low-confidence atoms (SPEC §14.5).

    Each score is multiplied by ``confidence × 0.5^(age / half_life)``, where
    age is measured from the most recent of:

      liveness_last_ok — when the claim was last *reconfirmed against the code*
      valid_from       — failing that, when the fact became true
      asserted_at      — last resort

    `valid_from` is the important one. The chain used to run through
    `updated_at`, which is just "when Meristem wrote the row" — so a first ingest of
    a repo minted one decision atom per commit, all stamped with the ingest
    time, and a two-year-old commit scored exactly the same as a constraint
    asserted a second ago. Decay was measuring how long ago Meristem *heard* a
    fact, not how old the fact is, which is precisely backwards for the atoms
    that most needed to sink: the git ingester sets `valid_from` to the commit's
    author timestamp, so ancient commits now decay from their real date.

    Facts carrying a passing liveness predicate stay fresh by being verified;
    facts without one sink as they go unconfirmed. Time-weighted, never zeroed
    by age alone — only confidence=0 removes an atom.
    """
    if not rank or half_life_days <= 0:
        return rank
    ids = list(rank)
    ph = ",".join("?" for _ in ids)
    meta = {
        r["id"]: (r["confidence"], r["ts"])
        for r in conn.execute(
            f"""SELECT id, confidence,
                       COALESCE(liveness_last_ok, valid_from, asserted_at) AS ts
                  FROM atoms WHERE id IN ({ph})""",
            ids,
        )
    }
    hl_secs = half_life_days * 86_400
    adjusted: dict[str, float] = {}
    for aid, score in rank.items():
        conf, ts = meta.get(aid, (1.0, now))
        age = max(0, now - (ts if ts is not None else now))
        adjusted[aid] = score * conf * (0.5 ** (age / hl_secs))
    return adjusted


def retrieve(
    conn: sqlite3.Connection,
    *,
    query: str,
    file_context: list[str] | None = None,
    embedder: embeddings.Embedder | None = None,
    seed_k: int = 10,
    top_n: int = 20,
    alpha: float = ALPHA,
    iterations: int = ITERATIONS,
    drill_down: bool = True,
    drill_modules: int = DRILL_MODULES,
    max_hops: int = MAX_HOPS,
    decay: bool = True,
    decay_half_life_days: float = DECAY_HALF_LIFE_DAYS,
    repo_root: Path | None = None,
    verify_live: bool = True,
    verify_freshness_seconds: int = VERIFY_FRESHNESS_SECONDS,
) -> list[Retrieved]:
    """Run PPR seeded on query + file context. Return top_n atoms.

    Retrieval is tiered (SPEC §14.1): a coarse PPR pass ranks the seeds'
    reachable component (including parent modules); then, when `drill_down`
    is set, the top `drill_modules` modules by mass are expanded into their
    children and the rank is recomputed over the larger universe. With no
    module atoms in the store this is a no-op and behaves as a single pass.

    If no seeds emerge (empty store, no embeddings yet), returns the
    most-recently-asserted live atoms as a degenerate fallback.

    Lazy liveness (SPEC §14.5): when `verify_live` and a `repo_root` is given,
    each atom is verified *as it is emitted* — predicates whose last check is
    stale are re-run, and an atom whose predicate now fails is dropped (we
    never return a fact we can't verify live). This is bounded: verification
    stops once `top_n` live atoms are collected, never scanning the whole store.
    With no `repo_root` (e.g. unit tests, ranking-only callers) verification is
    skipped and the result is identical to the pre-§14.5 behaviour.
    """
    embedder = embedder or embeddings.default_embedder()
    model = getattr(embedder, "name", None)
    qvec = embedder.embed(query) if embedder is not None else None
    seeds = _seed_set(
        conn, file_context=file_context, k=seed_k, qvec=qvec, model=model
    )
    if not seeds:
        rows = conn.execute(
            "SELECT id FROM live_atoms ORDER BY asserted_at DESC LIMIT ?",
            (top_n,),
        ).fetchall()
        # Relevance stays unknown (-1.0) here by construction: this path fires
        # when nothing matched at all, so "most recently asserted" is a
        # recency guess, not a match. A gate must be free to drop the lot.
        return [
            Retrieved(atom_id=r["id"], score=0.0, depth=0, via_kind=None,
                      via_edge_id=None, seed=False)
            for r in rows
        ]

    adj = _load_edges(conn)

    # Pass 1 (coarse): PPR over the seeds' forward-reachable component. Because
    # ROLLS_UP is directed child→module, this reaches parent modules but not a
    # module's *other* children — keeping the coarse universe bounded.
    universe = _forward_reachable(seeds, adj, max_hops=max_hops)
    rank = _run_ppr(universe, seeds, adj, alpha=alpha, iterations=iterations)

    # Pass 2 (drill-down, SPEC §14.1): for the modules the query's mass
    # concentrated on, pull their children into the universe AND flow mass
    # downward into them (base ROLLS_UP only points up), then re-rank.
    if drill_down:
        hot = _hot_modules(conn, universe, rank, drill_modules)
        if hot:
            triples = _module_children(conn, hot)
            child_ids = {child for _, child, _ in triples}
            if child_ids - universe:
                universe |= child_ids
                adj = _augment_downward(adj, triples)
                rank = _run_ppr(universe, seeds, adj, alpha=alpha, iterations=iterations)

    # Provenance: for each atom, pick the best (highest-flow) incoming edge.
    best_in: dict[str, tuple[str, int, int]] = {}  # atom_id -> (kind, edge_id, depth)
    # BFS from seeds to determine depth + best edge.
    visited: dict[str, tuple[int, str | None, int | None]] = {
        s: (0, None, None) for s in seeds
    }
    queue: list[str] = list(seeds)
    while queue:
        node = queue.pop(0)
        depth, _, _ = visited[node]
        if depth >= max_hops:  # provenance is only needed for in-universe atoms
            continue
        for dst, kind, eid, _ in adj.get(node, ()):
            if dst not in visited:
                visited[dst] = (depth + 1, kind, eid)
                queue.append(dst)
                best_in[dst] = (kind, eid, depth + 1)

    # Confidence/staleness decay (SPEC §14.5) — applied to final scores before
    # the top-n cut so unconfirmed or low-confidence atoms sink in ranking.
    if decay:
        rank = _apply_decay(
            conn, rank, half_life_days=decay_half_life_days, now=int(time.time())
        )

    # Secondary key (atom id) breaks score ties deterministically — the same
    # tie-break `_hot_modules` already applies to its own ranking. Without it,
    # equal-scoring atoms come out in dict-insertion order, which traces back
    # to hash-randomized set iteration, so repeated identical queries could
    # disagree on which of two tied atoms made the top_n cut.
    out = sorted(rank.items(), key=lambda t: (t[1], t[0]), reverse=True)
    # Lazy read-time liveness (SPEC §14.5). Only active when a repo_root is
    # supplied; metadata is fetched once and predicates re-run lazily as atoms
    # are emitted. We iterate the full ranked list but stop at top_n live
    # results, so a dead atom is replaced by the next-best live one without an
    # eager global scan.
    verify = verify_live and repo_root is not None
    live_meta = _liveness_meta(conn, [aid for aid, _ in out]) if verify else {}
    now_ts = int(time.time())
    results: list[Retrieved] = []
    for atom_id, score in out:
        if score <= 0:
            continue
        if len(results) >= top_n:
            break
        state = "none"
        if verify and repo_root is not None:  # `verify` already implies the second
            state = _liveness_state_now(
                conn, atom_id, live_meta.get(atom_id),
                repo_root=repo_root, now=now_ts, freshness=verify_freshness_seconds,
            )
            # Only a predicate that actually ran and failed removes the atom.
            if state == "failed":
                continue
        if atom_id in seeds:
            results.append(
                Retrieved(atom_id=atom_id, score=score, depth=0, via_kind=None,
                          via_edge_id=None, seed=True, liveness_state=state)
            )
        else:
            kind, eid, depth = best_in.get(atom_id, (None, None, 0))  # type: ignore[assignment]
            results.append(
                Retrieved(atom_id=atom_id, score=score, depth=depth,
                          via_kind=kind, via_edge_id=eid, seed=False,
                          liveness_state=state)
            )

    # Reconfirmation horizon (SPEC §18). Unlike liveness this needs no repo and
    # no predicate run — it is a timestamp comparison — so it applies on every
    # path, including callers that pass no `repo_root`. It only ever overrides
    # "none": an atom with a predicate that actually passed has been checked
    # against reality, which is stronger evidence than a human's last nod.
    if results:
        horizons = _confirm_horizons(conn, [r.atom_id for r in results])
        for r in results:
            if r.liveness_state != "none":
                continue
            by = horizons.get(r.atom_id)
            if by is not None and by < now_ts:
                r.liveness_state = "unconfirmed"

    # Relevance is measured only over the atoms actually being returned — at
    # most `top_n` dot products against vectors already in the store, so this
    # adds no model call and no meaningful latency to the hot path.
    if qvec is not None and results:
        sims = embeddings.similarity_for(
            conn, qvec, [r.atom_id for r in results], model=model
        )
        for r in results:
            r.relevance = sims.get(r.atom_id, -1.0)
    return results


__all__ = [
    "EDGE_WEIGHTS",
    "SCAFFOLDING_PREFIX",
    "Gated",
    "Retrieved",
    "gate",
    "is_scaffolding",
    "retrieve",
]
