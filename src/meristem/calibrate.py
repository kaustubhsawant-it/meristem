"""Derive a per-workspace relevance floor from data (SPEC §14.6).

Why this exists
---------------
`[retrieval] min_relevance` defaults to 0.70. That number is not a constant of
nature — it was hand-derived from ONE embedder (`bge-small-en-v1.5`) against ONE
populated 607-atom workspace, by observing that deliberate nonsense topped out
at 0.666 there while genuine queries reached 0.83. Cosine scales differ per
model and per corpus, so the same 0.70 is simultaneously:

* too high on a near-empty workspace — a genuinely good match scored 0.678 and
  was suppressed, so a brand-new store stays mute until it has content;
* too low under a different embedder, where nonsense may clear 0.70 comfortably
  and the gate silently stops gating.

`tools/replay_retrieval.py` could always re-derive the number by hand. Nothing
called it, so nobody did. This module is that derivation as a function.

The method
----------
Two distributions, measured through the real `retrieve()` path:

* **genuine** — the workspace's own logged prompts (`retrieval_log` is a free
  evaluation corpus of things a human actually asked here);
* **nonsense** — a fixed, deliberately meaningless probe set. Fixed, not
  random, so two runs on an unchanged store agree.

Take the top-1 relevance per query in each. The floor belongs between the
ceiling of nonsense and the bottom of genuine. We place it at
`nonsense_p95 + 0.25 * (genuine_p25 - nonsense_p95)` — nearer the nonsense
ceiling, because the asymmetry matters: a floor set too low leaks noise into
every prompt, but a floor set too high makes Meristem mute, and a mute substrate is
the failure mode that gets the whole system uninstalled. That blend also
reproduces the hand-calibrated 0.70 from §14.6's own numbers (0.666 + 0.25 ×
(0.828 − 0.666) ≈ 0.707), which is the only validation datapoint that exists.

When the two distributions overlap, this refuses to emit a number. An embedder
that cannot tell nonsense from a real question has no usable floor, and
inventing one would dress that up as a tuning problem.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from . import embeddings, retrieval

# Deliberately meaningless, and deliberately fixed: a random probe set would
# make the recommended floor jitter between runs on an unchanged store, which
# is indistinguishable from the store having changed. Shaped like real queries
# (3–6 tokens, lowercase, no punctuation) so any length effect on cosine falls
# on both distributions equally.
NONSENSE_PROBES: tuple[str, ...] = (
    "purple elephant tax return quantum",
    "zqxjv wobblegrump flimflarn",
    "marmalade tectonic bicycle sonnet",
    "glimmering walrus fiscal umbrella",
    "seventeen velvet asteroids humming",
    "pretzel cathedral migratory lozenge",
    "bewildered saxophone tundra pickle",
    "opaque tangerine parliament drift",
    "nocturnal abacus lemon scaffold",
    "crystalline badger monsoon ledger",
    "wistful pomegranate turbine chorus",
    "obsidian kazoo meridian sprout",
)

# Below this many distinct logged prompts there is no genuine distribution to
# speak of, only anecdotes. Refusing here is the point: a floor derived from
# four queries would carry the authority of a measurement and the reliability
# of a guess.
MIN_GENUINE_QUERIES = 20
# A store with almost no vectors produces relevance numbers that describe the
# sparsity, not the embedder.
MIN_EMBEDDED_ATOMS = 20
# Where to place the floor between the nonsense ceiling and the genuine floor.
# See module docstring for why this is not 0.5.
BLEND = 0.25
# Top-n per probe. We only read the top-1 relevance, but retrieval quality at
# n=1 is noisier than the shape of the head, and this matches the router's
# smallest class budget.
PROBE_TOP_N = 3

Status = Literal["ok", "insufficient_data", "not_separable"]


@dataclass(frozen=True)
class Calibration:
    status: Status
    # Recommended floor. None unless status == "ok" — an unusable calibration
    # returns no number at all rather than a number with a caveat attached,
    # because the caveat is what gets dropped when this is read by a script.
    recommended: float | None = None
    current: float | None = None
    nonsense_ceiling: float | None = None   # p95 of nonsense top-1
    genuine_floor: float | None = None      # p25 of genuine top-1
    n_genuine: int = 0
    n_nonsense: int = 0
    n_embedded: int = 0
    reason: str = ""
    genuine_tops: list[float] = field(default_factory=list)
    nonsense_tops: list[float] = field(default_factory=list)

    @property
    def separation(self) -> float | None:
        """Gap between the two distributions. Negative means they overlap."""
        if self.nonsense_ceiling is None or self.genuine_floor is None:
            return None
        return self.genuine_floor - self.nonsense_ceiling

    @property
    def drift(self) -> float | None:
        """How far the configured floor sits from the recommended one."""
        if self.recommended is None or self.current is None:
            return None
        return self.current - self.recommended


def _pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile. `values` need not be sorted."""
    s = sorted(values)
    if not s:
        raise ValueError("empty")
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def load_genuine_queries(conn: sqlite3.Connection, limit: int) -> list[str]:
    """Distinct logged prompts, most recent first.

    DISTINCT matters: a hook firing on a repeated prompt would otherwise let one
    query dominate the distribution and set the floor for the whole workspace.
    """
    rows = conn.execute(
        """SELECT query, MAX(ts) AS last_seen FROM retrieval_log
           WHERE query IS NOT NULL AND length(trim(query)) > 2
           GROUP BY query
           ORDER BY last_seen DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [r["query"] for r in rows]


def _top_relevance(
    conn: sqlite3.Connection,
    query: str,
    embedder: embeddings.Embedder | None,
) -> float | None:
    """Best known relevance among non-scaffolding hits, or None if unmeasurable.

    Liveness verification is deliberately off: this measures the embedding
    space, and re-running predicates over every probe would make calibration
    cost a repo scan. A dead atom's cosine still describes the scale.
    """
    hits = retrieval.retrieve(
        conn, query=query, top_n=PROBE_TOP_N, embedder=embedder, verify_live=False,
    )
    best: float | None = None
    for h in hits:
        if not h.relevance_known:
            continue
        row = conn.execute(
            "SELECT topic_key FROM atoms WHERE id = ?", (h.atom_id,)
        ).fetchone()
        if row is not None and retrieval.is_scaffolding(row["topic_key"]):
            continue
        best = h.relevance if best is None else max(best, h.relevance)
    return best


def derive(
    conn: sqlite3.Connection,
    *,
    current: float | None = None,
    embedder: embeddings.Embedder | None = None,
    limit: int = 200,
) -> Calibration:
    """Measure both distributions and recommend a floor.

    Read-only: replays queries through retrieval and writes nothing — no
    `retrieval_log` rows, no atoms.
    """
    embedder = embedder or embeddings.default_embedder()
    # Counted under the QUERYING model, not in total. A store full of vectors
    # written by a different embedder is precisely the state where every
    # relevance reads as unknown, and a total count would hide that behind a
    # reassuring number.
    model = getattr(embedder, "name", None)
    n_embedded = conn.execute(
        "SELECT COUNT(*) c FROM atom_embeddings WHERE ? IS NULL OR model = ?",
        (model, model),
    ).fetchone()["c"]

    genuine_queries = load_genuine_queries(conn, limit)
    if n_embedded < MIN_EMBEDDED_ATOMS:
        return Calibration(
            status="insufficient_data",
            current=current,
            n_genuine=len(genuine_queries),
            n_embedded=n_embedded,
            reason=(
                f"only {n_embedded} embedded atom(s); need {MIN_EMBEDDED_ATOMS}. "
                f"Run `meristem ingest` then `meristem embed` — relevance measured over a "
                f"near-empty store describes the emptiness, not the embedder."
            ),
        )
    if len(genuine_queries) < MIN_GENUINE_QUERIES:
        return Calibration(
            status="insufficient_data",
            current=current,
            n_genuine=len(genuine_queries),
            n_embedded=n_embedded,
            reason=(
                f"only {len(genuine_queries)} distinct logged prompt(s); need "
                f"{MIN_GENUINE_QUERIES}. The floor is derived against what people "
                f"actually ask here, so this workspace has to be used first. Until "
                f"then the default floor stands."
            ),
        )

    genuine_tops = [
        v for q in genuine_queries
        if (v := _top_relevance(conn, q, embedder)) is not None
    ]
    nonsense_tops = [
        v for q in NONSENSE_PROBES
        if (v := _top_relevance(conn, q, embedder)) is not None
    ]
    if len(genuine_tops) < MIN_GENUINE_QUERIES or not nonsense_tops:
        return Calibration(
            status="insufficient_data",
            current=current,
            n_genuine=len(genuine_tops),
            n_nonsense=len(nonsense_tops),
            n_embedded=n_embedded,
            reason=(
                "too few queries produced a measurable relevance — atoms are "
                "stored without vectors under the querying model. Check "
                "`meristem doctor` for embedding drift."
            ),
        )

    ceiling = _pct(nonsense_tops, 0.95)
    floor_of_genuine = _pct(genuine_tops, 0.25)
    common = {
        "current": current,
        "nonsense_ceiling": ceiling,
        "genuine_floor": floor_of_genuine,
        "n_genuine": len(genuine_tops),
        "n_nonsense": len(nonsense_tops),
        "n_embedded": n_embedded,
        "genuine_tops": genuine_tops,
        "nonsense_tops": nonsense_tops,
    }
    if floor_of_genuine <= ceiling:
        return Calibration(
            status="not_separable",
            reason=(
                f"nonsense scores as well as real queries here "
                f"(nonsense p95 {ceiling:.4f} ≥ genuine p25 {floor_of_genuine:.4f}), "
                f"so no floor separates them and any number would be arbitrary. "
                f"This is an embedder problem, not a tuning problem: the gate "
                f"cannot work under a model that does not discriminate."
            ),
            **common,
        )
    recommended = round(ceiling + BLEND * (floor_of_genuine - ceiling), 4)
    return Calibration(
        status="ok",
        recommended=recommended,
        reason=(
            f"nonsense tops out at {ceiling:.4f}, genuine queries start at "
            f"{floor_of_genuine:.4f}; floor placed {BLEND:.0%} into that gap."
        ),
        **common,
    )


__all__ = [
    "BLEND",
    "MIN_EMBEDDED_ATOMS",
    "MIN_GENUINE_QUERIES",
    "NONSENSE_PROBES",
    "Calibration",
    "derive",
    "load_genuine_queries",
]
