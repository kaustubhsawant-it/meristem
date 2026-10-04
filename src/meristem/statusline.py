"""Memory Pulse statusline (SPEC §13).

Format:
    Meristem │ 🧠 142 · ⏵Orient · 🟢→3 · ⚠ 1 stale · ✦ tick 3

  🧠 N      — live atom count
  ⏵<phase> — current OODAR phase (auto-detected, see below)
  <color>→N — atoms injected this turn (last route() call)
  ⚠ N stale — atoms with failed liveness today
  ✦ tick N — OODAR ticks completed today (commits since midnight)

The Claude Code statusline calls `meristem statusline` per assistant turn —
read-only, sub-50ms. Reads `.meristem/atoms.sqlite` and a small sidecar
`pulse.json` written by other commands to track ephemeral state (last
class injected, OODAR phase hint).
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import freshness, git_utils, liveness, store
from .paths import detect_layout

PULSE_FILE = "pulse.json"

PHASE_GLYPHS = {
    "Observe": "⏵Observe",
    "Orient":  "⏵Orient",
    "Decide":  "⏵Decide",
    "Act":     "⏵Act",
    "Verify":  "⏵Verify",
    "Remember": "⏵Remember",
}

CLASS_COLOR = {
    "navigational":  "🔵",
    "semantic":      "🟢",
    "contradictory": "🔴",
    "implementation": "🟣",
}


@dataclass
class Pulse:
    atoms: int
    phase: str
    last_class: str | None
    last_class_count: int
    stale: int
    ticks_today: int
    # Commits the index is behind HEAD (SPEC §19). Shown only when non-zero:
    # the pulse is glanced at, not read, so a glyph that is always present
    # stops being seen. `⚠ stale` earns its place by being rare; `⟳ behind`
    # has to earn its the same way.
    behind: int = 0
    # Pending review-queue candidates (0.3.0). Shown only when non-zero, like
    # `behind`: the number is the nudge to run `meristem review`.
    to_review: int = 0

    def render(self) -> str:
        parts = [f"🧠 {self.atoms}", PHASE_GLYPHS.get(self.phase, f"⏵{self.phase}")]
        if self.last_class:
            color = CLASS_COLOR.get(self.last_class, "·")
            parts.append(f"{color}→{self.last_class_count}")
        if self.stale:
            parts.append(f"⚠ {self.stale} stale")
        if self.to_review:
            parts.append(f"{self.to_review} to review")
        if self.behind:
            parts.append(f"⟳ {self.behind} behind")
        parts.append(f"✦ tick {self.ticks_today}")
        return "Meristem │ " + " · ".join(parts)


def _pulse_state(layout) -> dict:
    p = layout.state_dir / PULSE_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_pulse(*, phase: str | None = None, last_class: str | None = None,
                last_class_count: int = 0) -> None:
    """Update the sidecar pulse.json. Called from `route`, `digest`, etc."""
    layout = detect_layout()
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    state = _pulse_state(layout)
    if phase is not None:
        state["phase"] = phase
    if last_class is not None:
        state["last_class"] = last_class
        state["last_class_count"] = last_class_count
    (layout.state_dir / PULSE_FILE).write_text(json.dumps(state), encoding="utf-8")


def _ticks_today(root: Path) -> int:
    midnight = dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        out = subprocess.run(
            ["git", "log", f"--since={midnight.isoformat()}", "--oneline"],
            cwd=root, check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return 0
    return sum(1 for line in out.stdout.splitlines() if line.strip())


def _pending_count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE status = 'pending'"
        ).fetchone()[0])
    except sqlite3.Error:  # pre-v5 store without the table
        return 0


def _behind_fast(conn: sqlite3.Connection) -> int:
    """Commits behind HEAD summed over registered roots: one rev-list per root.

    Unlike `freshness.workspace_drift` this skips the is-repo / HEAD / dirty
    probes — the pulse only displays the commit count.
    """
    total = 0
    for root_path, sha in conn.execute(
        "SELECT root_path, last_indexed_sha FROM repo_state"
    ).fetchall():
        p = Path(root_path)
        if sha and p.exists():
            total += git_utils.commits_between(p, sha) or 0
    return total


def compact_line() -> str:
    """The `--compact` statusline: a sub-100ms path with no heavy imports.

    Opens the store read-only with plain sqlite3 (no migrations, no
    embeddings/retrieval/numpy), reads two counts, and shells out to git only
    for the freshness rev-list and today's tick count. Always renders: any
    failure degrades to a smaller line rather than an error in the user's
    status bar.
    """
    layout = detect_layout()
    state = _pulse_state(layout)
    atoms = stale = behind = to_review = 0
    if layout.db.exists():
        try:
            conn = sqlite3.connect(f"file:{layout.db}?mode=ro", uri=True)
        except sqlite3.Error:
            conn = None
        if conn is not None:
            try:
                atoms = int(conn.execute(
                    "SELECT COUNT(*) FROM live_atoms"
                ).fetchone()[0])
                to_review = _pending_count(conn)
                try:
                    cutoff = int(dt.datetime.now(dt.UTC).timestamp()) - 86_400
                    stale = int(conn.execute(
                        "SELECT COUNT(*) FROM live_atoms"
                        " WHERE liveness_kind IS NOT NULL AND liveness_kind != 'none'"
                        "   AND (liveness_last_ok IS NULL OR liveness_last_ok < ?)",
                        (cutoff,),
                    ).fetchone()[0])
                    behind = _behind_fast(conn)
                except Exception:  # noqa: BLE001 — the pulse must always render
                    pass
            except sqlite3.Error:
                pass
            finally:
                conn.close()
    return Pulse(
        atoms=atoms,
        phase=state.get("phase", "Observe"),
        last_class=state.get("last_class"),
        last_class_count=int(state.get("last_class_count", 0)),
        stale=stale,
        ticks_today=_ticks_today(layout.root),
        behind=behind,
        to_review=to_review,
    ).render()


def main(argv: list[str] | None = None) -> int:
    """`python -m meristem.statusline [--compact]` — skips the CLI's import cost."""
    print(compact_line())
    return 0


def build_pulse() -> Pulse:
    layout = detect_layout()
    state = _pulse_state(layout)
    atoms = 0
    stale = 0
    behind = 0
    to_review = 0
    if layout.db.exists():
        with store.connect(layout.db) as conn:
            atoms = store.atom_count(conn)
            to_review = _pending_count(conn)
            stale = len(liveness.stale_atoms(conn))
            try:
                # One `git rev-list --count` per root — the same order of cost as
                # `_ticks_today`, which this line already pays every turn.
                behind = freshness.total_behind(freshness.workspace_drift(conn))
            except Exception:  # noqa: BLE001 — the pulse must always render
                behind = 0
    return Pulse(
        atoms=atoms,
        phase=state.get("phase", "Observe"),
        last_class=state.get("last_class"),
        last_class_count=int(state.get("last_class_count", 0)),
        stale=stale,
        ticks_today=_ticks_today(layout.root),
        behind=behind,
        to_review=to_review,
    )


__all__ = [
    "CLASS_COLOR", "PHASE_GLYPHS", "Pulse", "build_pulse", "compact_line", "main",
    "write_pulse",
]

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
