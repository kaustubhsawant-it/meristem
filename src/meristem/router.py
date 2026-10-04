"""UserPromptSubmit hook — classify prompt, retrieve atoms, cap injection.

Heuristic v1 classifier (SPEC §13 color codes):

  navigational  — "where is X", "show me Y", "what file"
  semantic      — "why does", "is it safe", "how does X interact"
  contradictory — "but we said", "didn't we decide", "isn't X already"
  implementation — "add", "fix", "refactor", "remove", "rename"

The class drives retrieval shape:
  navigational  → ≤ 3 atoms, summaries at 10w resolution
  semantic      → ≤ 5 atoms, summaries at 50w
  contradictory → ≤ 7 atoms, prefer atoms in `history()` that share topic
  implementation → ≤ 5 atoms + 2-hop MIRRORS neighborhood (SPEC §11 seed)

Output is a JSON block written to stdout in the SessionStart-style format
Claude Code's UserPromptSubmit hook ingests. Token cap is enforced —
SPEC §7 requires ≤3K tokens per turn.

Two qualifiers travel with the atoms, because an unqualified fact is the thing
Meristem exists to avoid:

* the relevance gate (SPEC §14.6) — nothing below `min_relevance` is injected,
  up to and including injecting nothing at all. The gate itself now lives in
  `retrieval.gate` so `meristem query` and MCP `query_facts` apply the same rules;
* index drift (SPEC §19) — `drift_commits` / `drift_summary` say how far behind
  HEAD the index that produced these atoms is, populated only when non-zero.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import atoms as atoms_mod
from . import config, freshness, retrieval, store
from .paths import detect_layout

TURN_TOKEN_CAP = 3000

_NAV_RE = re.compile(
    r"\b(where (is|do|does)|show me|which file|find|locate|grep|"
    r"path to|file for)\b", re.IGNORECASE,
)
_SEM_RE = re.compile(
    r"\b(why (does|is|do|are)|how does|is it safe|what happens|"
    r"is .* allowed|explain)\b", re.IGNORECASE,
)
_CONTRA_RE = re.compile(
    r"\b(but (we|i) (said|decided)|didn'?t we|isn'?t .* already|"
    r"thought (we|that)|haven'?t we)\b", re.IGNORECASE,
)
_IMPL_RE = re.compile(
    r"\b(add|fix|refactor|remove|rename|implement|build|wire|"
    r"replace|migrate|update|patch|tweak)\b", re.IGNORECASE,
)

CLASSES = ("navigational", "semantic", "contradictory", "implementation")


@dataclass
class Routed:
    classified_as: str
    confidence: float
    atoms: list[dict[str, Any]]
    injected_tokens: int
    # How many retrieved atoms the relevance gate dropped. Reported so a caller
    # (and `meristem doctor`) can tell "the store had nothing to say" apart from
    # "the store said nothing because it was never asked".
    suppressed: int = 0
    # Best query↔atom cosine seen for this prompt, before gating. -1.0 when no
    # atom carried a comparable vector. This is the number that decides whether
    # Meristem speaks; `confidence` below is the *classifier's* confidence in the
    # prompt's category and has never had anything to do with relevance.
    top_relevance: float = -1.0
    # Module-tier atoms filtered as retrieval machinery (SPEC §14.1). Separate
    # from `suppressed` because it is not a relevance judgement — a module atom
    # is filtered however well it matches.
    scaffolding_hidden: int = 0
    # How far behind HEAD the index that produced these atoms is (SPEC §19).
    # `drift_summary` is None when there is nothing to say — every root current,
    # or drift could not be measured. Absence is not a claim of currency; the
    # payload simply does not raise the subject, exactly as the Memory Pulse
    # shows `⟳ N behind` only when N is non-zero.
    drift_commits: int = 0
    drift_summary: str | None = None

    def to_json(self) -> str:
        return json.dumps(self.__dict__)


def classify(prompt: str) -> tuple[str, float]:
    """Return (class, confidence). Defaults to 'semantic' at 0.3."""
    # Priority: contradictory > implementation > navigational > semantic.
    # Contradictory takes precedence because if Claude is about to argue
    # with a prior decision, surfacing the closed atom is highest-value.
    if _CONTRA_RE.search(prompt):
        return "contradictory", 0.9
    if _IMPL_RE.search(prompt):
        return "implementation", 0.7
    if _NAV_RE.search(prompt):
        return "navigational", 0.8
    if _SEM_RE.search(prompt):
        return "semantic", 0.6
    return "semantic", 0.3


def _atom_payload(view: atoms_mod.AtomView, resolution: int) -> dict[str, Any]:
    return {
        "id": view.atom.id,
        "type": view.atom.type,
        "topic_key": view.atom.topic_key,
        "decision_status": view.atom.decision_status,
        "decision_class": view.atom.decision_class,
        "summary": view.summaries.get(resolution) or view.summaries.get(50, ""),
        "source_ref": view.atom.source_ref,
        "source_kind": view.atom.source_kind,
    }


# The scaffolding predicate and the floor arithmetic moved to `retrieval.gate`
# so `meristem query` and MCP `query_facts` — which call `retrieval.retrieve`
# directly and therefore bypassed this module entirely — apply the same rules.


def render_atom_line(a: dict[str, Any]) -> str:
    """The exact markdown line a prompt-hook recall block renders for one atom.

    Single source of truth for both the injected text (`hooks._handle_prompt`)
    and its token accounting (`route`, below) — they were computed separately
    before and silently diverged: the logged `injected_tokens` counted only
    `summary`, undercounting the real per-turn cost by the kind label and any
    source-ref/liveness suffix actually sent to the model.

    Commit-sourced atoms (`source_kind == "commit"`) already embed their short
    SHA in `summary` (`git_commits.py`'s `summary_50`); appending the full
    40-char `source_ref` SHA too is pure duplication, so it's suppressed here.
    Non-commit atoms keep the suffix — their `source_ref` (a file path) isn't
    shown anywhere else.
    """
    if a.get("type") == "decision":
        status = a.get("decision_status")
        kind = f"DECIDED{'/' + status if status else ''}"
    else:
        kind = str(a.get("type") or "").upper()
    show_source = bool(a.get("source_ref")) and a.get("source_kind") != "commit"
    src = f" _({a['source_ref']})_" if show_source else ""
    weak = " _(unverified)_" if a.get("liveness_state") == "unverifiable" else ""
    return f"- [{kind}] {a.get('summary', '')}{src}{weak}"


def _approx_tokens(s: str) -> int:
    return max(1, (len(s) + 3) // 4)


def _drift(conn: sqlite3.Connection) -> tuple[int, str | None]:
    """How stale the index behind this answer is (SPEC §19).

    §19.5 states drift on four surfaces — doctor, the SessionStart digest, the
    Memory Pulse and `meristem status` — and missed the one that actually puts facts
    in front of the model. An atom injected here is asserted about code as it
    stood at the indexed sha; if that is 56 commits back, the claim may describe
    a function that no longer exists. Saying so is what §19 called keeping the
    reader's attention honest, and this is where the reader is.

    Cost is one `git rev-list --count` plus one `git status` per registered root
    — the same work the per-turn Memory Pulse already does, so the hot path
    absorbs it.

    Never raises. This runs inside a UserPromptSubmit hook: a crash here does
    not degrade an answer, it blocks the user's prompt. An unmeasurable drift
    reports as "nothing to say" rather than as zero — `doctor`'s `repo.freshness`
    check is the surface that escalates an index nobody can date.
    """
    try:
        drifts = freshness.workspace_drift(conn)
        return freshness.total_behind(drifts), freshness.summarize(drifts)
    except Exception:  # noqa: BLE001 — see docstring; a hook must not crash
        return 0, None


def route(
    prompt: str,
    file_context: list[str] | None = None,
    *,
    min_relevance: float | None = None,
) -> Routed:
    """Classify the prompt, retrieve atoms, gate on relevance, cap injection.

    The gate is the point. Retrieval ranks by PPR mass, which orders atoms
    within one query but says nothing about whether any of them match it — so
    before the gate existed, every prompt got a confident answer, including
    prompts the store had nothing to say about. A memory that always answers
    teaches its reader to stop reading it.
    """
    cls, conf = classify(prompt)
    layout = detect_layout()
    floor = (
        min_relevance
        if min_relevance is not None
        else config.load(layout.config).retrieval.min_relevance
    )

    n, resolution = {
        "navigational":   (3, 10),
        "semantic":       (5, 50),
        "contradictory":  (7, 50),
        "implementation": (5, 50),
    }[cls]

    if not layout.db.exists():
        return Routed(classified_as=cls, confidence=conf, atoms=[], injected_tokens=0)

    atoms_out: list[dict[str, Any]] = []
    tokens = 0
    with store.connect(layout.db) as conn:
        hits = retrieval.retrieve(
            conn, query=prompt, file_context=file_context, top_n=n,
            repo_root=layout.root,
        )
        # Resolve views up front so the gate can see topic_key. A hit whose
        # atom has vanished is dropped here rather than counted as suppressed —
        # it is a missing row, not a relevance judgement.
        views = {}
        for h in hits:
            if (view := atoms_mod.get_atom(conn, h.atom_id)) is not None:
                views[h.atom_id] = view
        verdict = retrieval.gate(
            (h for h in hits if h.atom_id in views),
            min_relevance=floor,
            topic_of=lambda aid: views[aid].atom.topic_key,
        )
        suppressed = verdict.suppressed_count
        top_rel = verdict.top_relevance

        for h in verdict.kept:
            payload = _atom_payload(views[h.atom_id], resolution)
            payload["trail"] = h.trail()
            payload["relevance"] = round(h.relevance, 4)
            payload["score"] = round(h.score, 6)
            payload["liveness_state"] = h.liveness_state
            row_tokens = _approx_tokens(render_atom_line(payload))
            if tokens + row_tokens > TURN_TOKEN_CAP:
                break
            tokens += row_tokens
            atoms_out.append(payload)

        drift_commits, drift_summary = _drift(conn)

        # Audit log so we can train a real classifier later (SPEC §9) — and so
        # the log doubles as a regression corpus: `tools/replay_retrieval.py`
        # reads it back, which is only useful if the scores are recorded too.
        conn.execute(
            """INSERT INTO retrieval_log
                 (query, classified_as, atoms_returned, injected_tokens,
                  relevances, top_relevance, n_suppressed, min_relevance)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                prompt[:500],
                cls,
                json.dumps([a["id"] for a in atoms_out]),
                tokens,
                json.dumps([a["relevance"] for a in atoms_out]),
                top_rel,
                suppressed,
                floor,
            ),
        )
        conn.commit()

    return Routed(
        classified_as=cls,
        confidence=conf,
        atoms=atoms_out,
        injected_tokens=tokens,
        suppressed=suppressed,
        top_relevance=round(top_rel, 4),
        scaffolding_hidden=verdict.scaffolding_count,
        drift_commits=drift_commits,
        drift_summary=drift_summary,
    )


__all__ = ["CLASSES", "Routed", "TURN_TOKEN_CAP", "classify", "route"]
