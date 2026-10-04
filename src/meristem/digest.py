"""SessionStart digest — bootstraps Claude Code with substrate context.

Per SPEC §6 + §7, the digest delivers ≤4K tokens at session start:
  - branch + dirty files (orientation)
  - last 3 commits (recent history)
  - top-5 atoms by weight (invariants + CLOSED decisions)
  - halt contract (the literal instruction Claude is bound by)

Emitted as JSON on stdout — Claude Code's SessionStart hook wraps it
into the bootstrap context. The shell wrapper handles file-not-found
gracefully (so `meristem init` was never run → empty injection).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import atoms as atoms_mod
from . import candidates, store
from . import doctor as doctor_mod
from .paths import detect_layout

# Halt contract is delivered verbatim — the literal instructions Claude is
# bound by during the session. Kept short to stay within the digest budget.
HALT_CONTRACT = (
    "At ≥85% context capacity, agentic skills enter safe-halt: write a handoff "
    "with `meristem handoff`, then stop. Read-only operations may continue but no "
    "new atom drafts. The halt is non-negotiable unless --force-continue is "
    "explicitly passed."
)


# How many non-ok health findings ride along in the digest. The digest has a
# ~4K token budget (SPEC §7) and health is a nudge, not the payload.
MAX_HEALTH_FINDINGS = 4


@dataclass
class Digest:
    workspace: str
    branch: str | None
    dirty_files: list[str]
    recent_commits: list[dict[str, str]]
    top_atoms: list[dict[str, Any]]
    halt_contract: str
    schema_version: int
    atom_count: int
    # Non-ok findings from `meristem doctor`. Present so the agent learns at token
    # zero that the substrate is empty / edgeless / unindexed, instead of
    # discovering it by receiving silently empty retrievals all session.
    health: list[dict[str, str]] = field(default_factory=list)
    # Facts captured from conversation and waiting on review (SPEC §18). The
    # digest carries this because a review queue nobody is told about is a
    # review queue nobody empties — the same failure that left `meristem doctor`
    # unread for two months. Capture is worthless without the disposal step.
    pending_review: int = 0
    # How far behind the repo this index is (SPEC §19). A dedicated field rather
    # than only a `health` line, because it qualifies every answer the substrate
    # gives for the rest of the session: at 56 commits behind, an atom describing
    # a module can be describing a module that no longer exists. `summary` is
    # None exactly when every root is current.
    staleness: dict[str, Any] = field(
        default_factory=lambda: {"behind": 0, "dirty": 0, "summary": None, "roots": []}
    )

    def to_json(self, *, pretty: bool = False) -> str:
        return json.dumps(self.__dict__, indent=2 if pretty else None)


def _git_dirty(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root, check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [line[3:].strip() for line in out.stdout.splitlines() if line.strip()][:20]


def _git_branch(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root, check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _git_recent(root: Path, n: int = 3) -> list[dict[str, str]]:
    try:
        out = subprocess.run(
            ["git", "log", f"-n{n}", "--pretty=format:%h%x09%s%x09%an"],
            cwd=root, check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append({"sha": parts[0], "subject": parts[1], "author": parts[2]})
    return rows


def build_digest(top_k: int = 5) -> Digest:
    """Assemble the SessionStart digest."""
    layout = detect_layout()
    branch = _git_branch(layout.root)
    dirty = _git_dirty(layout.root)
    recent = _git_recent(layout.root)

    top_atoms: list[dict[str, Any]] = []
    schema_v = 0
    atom_count = 0
    pending = 0
    staleness = _empty_staleness()
    if layout.db.exists():
        with store.connect(layout.db) as conn:
            schema_v = store.schema_version(conn)
            atom_count = store.atom_count(conn)
            try:
                pending = candidates.pending_count(conn)
            except sqlite3.Error:
                pending = 0  # pre-v5 store; the table arrives on next migrate
            staleness = _staleness(conn)
            # Headline atoms: invariants + decisions that actually CONSTRAIN
            # future work, pinned first. Filtering to `constraint` is the point:
            # the git ingester mints one CLOSED decision per commit, so this
            # query used to return commit subject lines — 76 of them against 0
            # real constraints in one workspace — burning the digest's headline
            # slots on history that forbids nothing.
            for t in ("invariant", "decision"):
                rows = atoms_mod.query_atoms(
                    conn,
                    type=t,
                    decision_status=("CLOSED" if t == "decision" else None),
                    decision_class=("constraint" if t == "decision" else None),
                    order_by="confidence DESC",
                    limit=top_k,
                )
                for a in rows:
                    view = atoms_mod.get_atom(conn, a.id)
                    if view:
                        top_atoms.append({
                            "id": a.id,
                            "type": a.type,
                            "topic_key": a.topic_key,
                            "summary": view.summaries.get(50, ""),
                            "pinned": a.pinned,
                        })
                if len(top_atoms) >= top_k:
                    break
        top_atoms = sorted(top_atoms, key=lambda r: not r["pinned"])[:top_k]

    return Digest(
        workspace=str(layout.root),
        branch=branch,
        dirty_files=dirty,
        recent_commits=recent,
        top_atoms=top_atoms,
        halt_contract=HALT_CONTRACT,
        schema_version=schema_v,
        atom_count=atom_count,
        health=_health(layout),
        pending_review=pending,
        staleness=staleness,
    )


def _empty_staleness() -> dict[str, Any]:
    return {"behind": 0, "dirty": 0, "summary": None, "roots": []}


def _staleness(conn: sqlite3.Connection) -> dict[str, Any]:
    """Commits-behind per root, for the SessionStart injection (SPEC §19).

    Advisory like `_health`: a git subprocess that hangs or a repo_state table
    from a partly-migrated store must degrade to "nothing reported", never to a
    session that fails to bootstrap.
    """
    from . import freshness

    try:
        drifts = freshness.workspace_drift(conn)
    except Exception:  # noqa: BLE001 — the digest must always render
        return _empty_staleness()
    return {
        "behind": freshness.total_behind(drifts),
        "dirty": sum(d.dirty for d in drifts),
        "summary": freshness.summarize(drifts),
        "roots": [
            {"root": str(d.root), "behind": d.commits_behind, "dirty": d.dirty}
            for d in drifts
            if d.drifted or d.dirty
        ],
    }


def _health(layout) -> list[dict[str, str]]:
    """Non-ok doctor findings, worst first, for the SessionStart injection.

    `meristem doctor` has detected an edgeless store since the day it shipped, but
    nothing ever ran it: no hook, no skill, no statusline, no other command.
    A health check nobody reads cannot prevent anything, so the digest — the
    one surface guaranteed to be consumed every session — carries it.

    Advisory by construction: health must never be the reason a session fails
    to bootstrap, so any error here degrades to "no health reported".
    """
    try:
        report = doctor_mod.run(layout, quick=True)
    except Exception:  # noqa: BLE001 — the digest must always render
        return []
    rank = {"fail": 0, "warn": 1}
    ranked = sorted(
        (f for f in report.findings if f.severity != "ok"),
        key=lambda f: (rank.get(f.severity, 2), f.name),
    )
    return [
        {"name": f.name, "severity": f.severity, "summary": f.summary}
        for f in ranked[:MAX_HEALTH_FINDINGS]
    ]


__all__ = ["Digest", "HALT_CONTRACT", "build_digest"]
