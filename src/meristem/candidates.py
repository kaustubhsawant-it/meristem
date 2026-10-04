"""The review queue — proposed facts that are not yet memory (SPEC §18).

A candidate is deliberately *not* retrievable. `retrieve()` never sees this
table. That separation is what lets capture be permissive: extraction can guess,
because a wrong guess sits in a queue costing one keystroke to reject, rather
than entering the substrate as a fact that gets injected as truth six weeks
later. The credibility rule from §14.5 — never return something we cannot stand
behind — is why the queue exists rather than an auto-assert with low confidence.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from . import atoms as atoms_mod

if TYPE_CHECKING:
    from .capture import CandidateFact

# How long an accepted conversational fact is trusted before it must be
# reconfirmed. Domain rules drift silently — a team changes ownership, a policy
# is revised — and nothing in the repo records it, so there is no predicate to
# run. Ninety days matches the retrieval decay half-life, so a fact fades from
# ranking and falls due for reconfirmation on roughly the same clock.
DEFAULT_CONFIRM_DAYS = 90


# Token-set Jaccard at or above this means "the
# same sentence with small variations". Measured on review labels, 12 of 95
# rejected candidates were re-proposals of an already-rejected template with
# 0 of 26 accepted lost, which exact-fingerprint dedup cannot see.
NEAR_DUP_JACCARD = 0.6


def tokenize(text: str) -> frozenset[str]:
    """Lower-cased alphanumeric token set — the unit of near-duplicate comparison."""
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def near_duplicate(tokens: frozenset[str], pool: Iterable[frozenset[str]]) -> bool:
    """True when `tokens` is within NEAR_DUP_JACCARD of any token set in `pool`."""
    return any(jaccard(tokens, other) >= NEAR_DUP_JACCARD for other in pool)


@dataclass
class Candidate:
    fingerprint: str
    text: str
    proposed_type: str
    score: int
    signals: list[str]
    source: str
    source_ref: str | None
    session_id: str | None
    created_at: int
    status: str
    atom_id: str | None = None


def _row_to_candidate(r: sqlite3.Row) -> Candidate:
    try:
        signals = json.loads(r["signals"] or "[]")
    except json.JSONDecodeError:
        signals = []
    return Candidate(
        fingerprint=r["fingerprint"],
        text=r["text"],
        proposed_type=r["proposed_type"],
        score=r["score"],
        signals=signals,
        source=r["source"],
        source_ref=r["source_ref"],
        session_id=r["session_id"],
        created_at=r["created_at"],
        status=r["status"],
        atom_id=r["atom_id"],
    )


def queue(
    conn: sqlite3.Connection,
    *,
    text: str,
    proposed_type: str = "convention",
    score: int = 0,
    signals: list[str] | None = None,
    source: str = "cli",
    source_ref: str | None = None,
    session_id: str | None = None,
    fingerprint: str | None = None,
) -> bool:
    """Queue one candidate. Returns True if newly queued, False if already known.

    `INSERT OR IGNORE` on the fingerprint is what makes rejection permanent: a
    sentence the user has already rejected stays rejected no matter how many
    times it is said again, so review does not become a treadmill.
    """
    if fingerprint is None:
        from .capture import CandidateFact

        fingerprint = CandidateFact(text=text, score=score).fingerprint
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO candidates
                 (fingerprint, text, proposed_type, score, signals, source,
                  source_ref, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fingerprint,
                text,
                proposed_type,
                score,
                json.dumps(signals or []),
                source,
                source_ref,
                session_id,
            ),
        )
    return cur.rowcount > 0


def queue_many(
    conn: sqlite3.Connection, facts: list[CandidateFact], *, source: str = "transcript"
) -> list[CandidateFact]:
    """Queue extracted facts, returning only those that were new.

    Beyond the exact-fingerprint dedup in `queue`, a fact whose token set is a
    near-duplicate (Jaccard >= NEAR_DUP_JACCARD) of a *rejected* candidate, of a
    pending one, or of an earlier fact in this batch is skipped — the first is
    kept. Accepted text is never a blocker: it is already memory, and a
    variation may be a legitimate refinement. The pool is pre-tokenised once;
    a store holds hundreds of candidates, not millions, so the pairwise scan is
    cheap (O(batch x pool)) and needs no index.
    """
    pool = [
        tokenize(r["text"])
        for r in conn.execute(
            "SELECT text FROM candidates WHERE status IN ('rejected', 'pending')"
        )
    ]
    fresh: list[CandidateFact] = []
    for f in facts:
        toks = tokenize(f.text)
        if near_duplicate(toks, pool):
            continue
        if queue(
            conn,
            text=f.text,
            proposed_type=f.proposed_type,
            score=f.score,
            signals=f.signals,
            source=source,
            source_ref=f.source_ref,
            session_id=f.session_id,
            fingerprint=f.fingerprint,
        ):
            fresh.append(f)
            pool.append(toks)
    return fresh


def pending(conn: sqlite3.Connection, *, limit: int = 50) -> list[Candidate]:
    rows = conn.execute(
        """SELECT * FROM candidates WHERE status = 'pending'
           ORDER BY score DESC, created_at ASC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [_row_to_candidate(r) for r in rows]


def noise(conn: sqlite3.Connection) -> list[tuple[Candidate, str]]:
    """Pending candidates the current filter would no longer propose, with the rule.

    Re-runs `capture.explain` (hard drops and score penalties) and the
    near-duplicate-of-rejected rule over the pending queue. Read-only: disposing
    of them is `meristem review --reject-noise`, an explicit human action.
    """
    from .capture import explain

    rejected = [
        tokenize(r["text"])
        for r in conn.execute("SELECT text FROM candidates WHERE status = 'rejected'")
    ]
    out: list[tuple[Candidate, str]] = []
    for c in pending(conn, limit=-1):
        rule = explain(c.text)
        if rule is None and near_duplicate(tokenize(c.text), rejected):
            rule = "near-duplicate"
        if rule is not None:
            out.append((c, rule))
    return out


def pending_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM candidates WHERE status = 'pending'"
    ).fetchone()
    return int(row["c"]) if row else 0


def get(conn: sqlite3.Connection, fingerprint: str) -> Candidate | None:
    row = conn.execute(
        "SELECT * FROM candidates WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    return _row_to_candidate(row) if row else None


def resolve(conn: sqlite3.Connection, prefix: str) -> Candidate | None:
    """Look a candidate up by fingerprint prefix — review is done by hand."""
    row = conn.execute(
        "SELECT * FROM candidates WHERE fingerprint LIKE ? || '%' LIMIT 2", (prefix,)
    ).fetchall()
    return _row_to_candidate(row[0]) if len(row) == 1 else None


def reject(conn: sqlite3.Connection, fingerprint: str) -> bool:
    with conn:
        cur = conn.execute(
            """UPDATE candidates SET status = 'rejected', reviewed_at = ?
               WHERE fingerprint = ? AND status = 'pending'""",
            (int(time.time()), fingerprint),
        )
    return cur.rowcount > 0


def accept(
    conn: sqlite3.Connection,
    fingerprint: str,
    *,
    type_: str | None = None,
    topic_key: str | None = None,
    confirm_days: int = DEFAULT_CONFIRM_DAYS,
) -> atoms_mod.Atom | None:
    """Promote a candidate to a real atom.

    The atom carries `confirm_by` rather than a liveness predicate, because
    there is nothing in the repo to check it against — that is the defining
    property of the knowledge this whole path exists to capture. Confidence
    starts at 1.0: a human just confirmed it, which is stronger evidence than
    anything the ingesters produce.
    """
    c = get(conn, fingerprint)
    if c is None or c.status != "pending":
        return None

    atom_type = type_ or c.proposed_type
    if atom_type not in atoms_mod.ATOM_TYPES:
        # The schema CHECK already refuses this, so nothing invalid was ever
        # stored — but it surfaced as a raw sqlite3.IntegrityError traceback
        # with the constraint SQL in it, which tells a user nothing about what
        # they should have typed. Found by the Phase B mypy pass (str reaching
        # an AtomType parameter), not by any test.
        raise ValueError(
            f"unknown atom type {atom_type!r}. Valid types: "
            + ", ".join(atoms_mod.ATOM_TYPES)
        )
    key = topic_key or f"captured:{c.fingerprint}"
    atom = atoms_mod.assert_fact(
        conn,
        type=cast(atoms_mod.AtomType, atom_type),  # validated against ATOM_TYPES above
        topic_key=key,
        summary_50w=c.text,
        source_kind="manual",
        source_ref=c.source_ref,
        tier="module",
    )
    with conn:
        conn.execute(
            "UPDATE atoms SET confirm_by = ? WHERE id = ?",
            (int(time.time()) + confirm_days * 86_400, atom.id),
        )
        conn.execute(
            """UPDATE candidates SET status = 'accepted', reviewed_at = ?, atom_id = ?
               WHERE fingerprint = ?""",
            (int(time.time()), atom.id, fingerprint),
        )
    return atom


# ---------------------------------------------------------------------------
# Agent review suggestions (0.3.0 stream C)
#
# An agent can read the queue and say "I would accept/reject this, because ...".
# That is advice shown next to the item in `meristem review`; it never changes
# a candidate's status — disposal stays a human keystroke. Stored in a sidecar
# JSON rather than a schema column: it is advisory, per-machine operational
# state (gitignore it like sync.json), and needs no migration.
# ---------------------------------------------------------------------------

SUGGESTIONS_FILE = "review_suggestions.json"
SUGGESTION_VERDICTS = ("accept", "reject")
_MAX_REASON = 500


def _suggestions_path(layout_or_dir: Any) -> Path:
    state_dir = getattr(layout_or_dir, "state_dir", layout_or_dir)
    return Path(state_dir) / SUGGESTIONS_FILE


def read_suggestions(layout_or_dir: Any) -> dict[str, dict[str, Any]]:
    """All recorded suggestions keyed by full fingerprint ({} if absent/corrupt)."""
    try:
        data = json.loads(_suggestions_path(layout_or_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def record_suggestion(
    layout_or_dir: Any, fingerprint: str, verdict: str, reason: str
) -> dict[str, Any]:
    """Record (replace) the agent's suggestion for one candidate. Atomic write."""
    if verdict not in SUGGESTION_VERDICTS:
        raise ValueError(f"verdict must be one of {SUGGESTION_VERDICTS}, got {verdict!r}")
    path = _suggestions_path(layout_or_dir)
    entry = {
        "verdict": verdict,
        "reason": " ".join(reason.split())[:_MAX_REASON],
        "at": int(time.time()),
    }
    data = read_suggestions(layout_or_dir)
    data[fingerprint] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return entry


def suggestion_for(layout_or_dir: Any, fingerprint: str) -> dict[str, Any] | None:
    s = read_suggestions(layout_or_dir).get(fingerprint)
    return s if isinstance(s, dict) and "verdict" in s else None


def render_suggestion(layout_or_dir: Any, fingerprint: str) -> str:
    """One display line for `meristem review`, or "" when there is no suggestion."""
    s = suggestion_for(layout_or_dir, fingerprint)
    if s is None:
        return ""
    reason = s.get("reason") or ""
    return f"agent suggests {s['verdict']}" + (f": {reason}" if reason else "")


def unconfirmed_atoms(conn: sqlite3.Connection, *, now: int | None = None) -> list[str]:
    """Live atoms whose reconfirmation horizon has passed."""
    now = now if now is not None else int(time.time())
    rows = conn.execute(
        """SELECT id FROM live_atoms
            WHERE confirm_by IS NOT NULL AND confirm_by < ?""",
        (now,),
    ).fetchall()
    return [r["id"] for r in rows]


__all__ = [
    "DEFAULT_CONFIRM_DAYS",
    "NEAR_DUP_JACCARD",
    "SUGGESTIONS_FILE",
    "SUGGESTION_VERDICTS",
    "Candidate",
    "accept",
    "get",
    "jaccard",
    "near_duplicate",
    "noise",
    "pending",
    "pending_count",
    "queue",
    "queue_many",
    "read_suggestions",
    "record_suggestion",
    "reject",
    "render_suggestion",
    "resolve",
    "suggestion_for",
    "tokenize",
    "unconfirmed_atoms",
]
