"""PostToolUse invariant-watcher.

Triggered by Claude Code's PostToolUse hook after Edit/Write/MultiEdit
calls. Looks up atoms whose ``liveness_target`` references the touched
file(s), re-runs their liveness predicates, and surfaces any that broke
since the last check.

The output is JSON (one record per violated atom) so the hook can either
inject it into the next-turn context or surface it through a Stop hook.

This is the zero-cost happy path: with no touched files or no atoms
referencing them, the run is a single SQL query and exits clean.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import liveness, store
from .paths import detect_layout


@dataclass
class WatchReport:
    touched_files: list[str]
    violations: list[dict] = field(default_factory=list)
    passed: int = 0

    def to_json(self, *, pretty: bool = False) -> str:
        return json.dumps(self.__dict__, indent=2 if pretty else None)

    @property
    def has_violations(self) -> bool:
        return bool(self.violations)


def _atoms_for_files(
    conn: sqlite3.Connection, files: list[str]
) -> list[sqlite3.Row]:
    if not files:
        return []
    placeholders = ",".join("?" for _ in files)
    return list(conn.execute(
        f"""SELECT id, type, topic_key, liveness_kind, liveness_target,
                   liveness_pattern, liveness_last_ok, source_ref
              FROM live_atoms
             WHERE liveness_kind IS NOT NULL
               AND liveness_kind != 'none'
               AND (liveness_target IN ({placeholders})
                    OR source_ref IN ({placeholders}))""",
        (*files, *files),
    ))


def watch(files: list[str], *, repo_root: Path | None = None) -> WatchReport:
    """Re-run liveness on atoms attached to `files`. Return report."""
    layout = detect_layout()
    root = repo_root or layout.root
    report = WatchReport(touched_files=files)
    if not layout.db.exists() or not files:
        return report
    with store.connect(layout.db) as conn:
        rows = _atoms_for_files(conn, files)
        for row in rows:
            try:
                result = liveness.check_atom(conn, row["id"], repo_root=root)
            except (KeyError, NotImplementedError, ValueError) as exc:
                report.violations.append({
                    "atom_id": row["id"],
                    "topic_key": row["topic_key"],
                    "type": row["type"],
                    "reason": f"check error: {exc}",
                })
                continue
            if result.ok:
                report.passed += 1
            else:
                report.violations.append({
                    "atom_id": row["id"],
                    "topic_key": row["topic_key"],
                    "type": row["type"],
                    "reason": result.reason,
                    "target": row["liveness_target"],
                })
    return report


__all__ = ["WatchReport", "watch"]
