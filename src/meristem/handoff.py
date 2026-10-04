"""PreCompact handoff generator — anti-hallucination resumption (SPEC §9).

Triggers:
  - PreCompact hook (≥80% context)
  - Skill clean exit (`tick_complete`)
  - User invokes `/meristem:handoff`
  - Halt-safety (≥85% capacity)
  - Stop hook with dirty files and no handoff this session

Storage:
  `.meristem/handoffs/<UTC-ts>-<session_id>.md` — committed (tribal knowledge)
  `.meristem/handoffs/LATEST.md`                 — symlink, gitignored

Schema: YAML frontmatter (SPEC §9) + narrative section, capped at 400
tokens. The narrative lints out anything regenerable from `git log -p`
or an atom ID — keeping context-window cost down on resumption.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import store
from .paths import Layout, detect_layout

NARRATIVE_TOKEN_CAP = 400

# Auto-detection window when no prior handoff exists to bound "since" — a
# fresh workspace's first handoff must not dump its entire historical atom
# store as "drafted this session".
_FALLBACK_WINDOW_SECONDS = 24 * 60 * 60

# Caps on the auto-detected lists themselves — a long session can touch far
# more atoms than are worth listing; the handoff is a resumption aid, not an
# audit log (retrieval_log/atoms already are that, queryable by timestamp).
_MAX_ATOMS_CONSULTED = 50
_MAX_ATOMS_DRAFTED = 30


@dataclass
class HandoffInput:
    """Caller-supplied fields. Everything else is auto-detected."""
    current_tick_id: str | None = None
    current_tick_title: str | None = None
    current_tick_status: str = "in_progress"
    current_tick_phase: str = "Act"
    context_pct_at_end: float | None = None
    ended_reason: str = "user_handoff"
    atoms_consulted: list[str] = field(default_factory=list)
    atoms_drafted: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_action: str = ""
    failed_assumptions: list[str] = field(default_factory=list)
    do_not_redo: list[str] = field(default_factory=list)
    edge_cases_pending: list[str] = field(default_factory=list)
    narrative: str = ""
    session_id: str | None = None


def _git_state(root: Path) -> dict[str, Any]:
    def g(*args) -> str | None:
        try:
            r = subprocess.run(
                ["git", *args], cwd=root, check=True, capture_output=True, text=True
            )
            return r.stdout
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    branch_raw = g("rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_raw.strip() if branch_raw is not None else None
    sha_raw = g("rev-parse", "HEAD")
    sha = sha_raw.strip() if sha_raw is not None else None
    status = g("status", "--porcelain") or ""
    # `status`'s leading column is a fixed-width "XY " status prefix and must
    # not be `.strip()`ed like branch/sha: porcelain's first entry is often
    # " M path" (leading space = clean index), and a whole-string `.strip()`
    # eats exactly that leading space, shifting the `line[3:]` slice below by
    # one and truncating that one file's name — e.g. "SPEC.md" -> "PEC.md".
    dirty = [line[3:].strip() for line in status.splitlines() if line.strip()][:30]
    return {"branch": branch, "last_commit_sha": sha, "dirty_files": dirty}


_ENDED_AT_RE = re.compile(r'^ended_at:\s*"?([0-9T:Z\-]+)"?\s*$', re.MULTILINE)


def _previous_handoff_since(layout: Layout) -> int:
    """Epoch seconds to auto-detect atoms *since* -- the prior handoff's
    `ended_at`, or a bounded fallback window when there is no prior handoff
    (a fresh workspace's first handoff must not dump the whole store)."""
    now = dt.datetime.now(dt.UTC)
    fallback = int((now - dt.timedelta(seconds=_FALLBACK_WINDOW_SECONDS)).timestamp())
    if not layout.handoffs.is_dir():
        return fallback
    existing = sorted(
        p for p in layout.handoffs.glob("*.md") if p.name != "LATEST.md"
    )
    if not existing:
        return fallback
    try:
        text = existing[-1].read_text(encoding="utf-8")
        m = _ENDED_AT_RE.search(text)
        if not m:
            return fallback
        ts = dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.UTC)
        return int(ts.timestamp())
    except (OSError, ValueError):
        return fallback


def _auto_detect_atoms(
    layout: Layout, since_ts: int
) -> tuple[list[str], list[dict[str, Any]]]:
    """Real signal for atoms_consulted/atoms_drafted, pulled from data that
    already exists rather than left for a caller who has no way to supply it.

    Before this, every CLI-driven handoff left both fields permanently `[]`
    -- `meristem handoff` never exposed flags for them and nothing populated
    them by default, which is how handoffs degenerated into empty
    checkpoints. consulted comes from `retrieval_log.atoms_returned` (what
    the router actually served since the last checkpoint); drafted comes from
    `atoms.created_at` (what this workspace actually wrote). A caller that
    supplies either list explicitly is respected as-is -- this only fills the
    gap when nothing was supplied.
    """
    if not layout.db.exists():
        return [], []
    consulted: list[str] = []
    drafted: list[dict[str, Any]] = []
    try:
        conn = store.connect(layout.db)
    except Exception:
        return [], []
    try:
        seen: set[str] = set()
        for row in conn.execute(
            "SELECT atoms_returned FROM retrieval_log WHERE ts >= ? ORDER BY ts DESC",
            (since_ts,),
        ):
            try:
                ids = json.loads(row["atoms_returned"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            for aid in ids:
                if aid not in seen:
                    seen.add(aid)
                    consulted.append(aid)
                if len(consulted) >= _MAX_ATOMS_CONSULTED:
                    break
            if len(consulted) >= _MAX_ATOMS_CONSULTED:
                break
        for row in conn.execute(
            """SELECT id, type, topic_key FROM atoms
                WHERE created_at >= ? AND archived = 0
                ORDER BY created_at DESC LIMIT ?""",
            (since_ts, _MAX_ATOMS_DRAFTED),
        ):
            drafted.append(
                {"id": row["id"], "type": row["type"], "topic_key": row["topic_key"]}
            )
    finally:
        conn.close()
    return consulted, drafted


def _approx_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _truncate_narrative(text: str, cap: int = NARRATIVE_TOKEN_CAP) -> str:
    """Crude truncation: cut at last sentence boundary before the cap."""
    if _approx_tokens(text) <= cap:
        return text
    target_chars = cap * 4
    head = text[:target_chars]
    cut = max(head.rfind(". "), head.rfind("\n\n"))
    return head[: cut + 1] if cut > 0 else head


def _format_yaml_value(value: Any, indent: int = 0) -> str:
    """Tiny YAML emitter — only what the schema needs (no anchors, no flow)."""
    pad = "  " * indent
    if isinstance(value, str):
        if value and ("\n" in value or value.startswith(" ") or value.endswith(" ")):
            return "|\n" + "\n".join(f"{pad}  {line}" for line in value.splitlines())
        if value == "" or re.search(r"[:#&*\[\]{},]", value):
            return json.dumps(value)
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        out = []
        for item in value:
            if isinstance(item, dict):
                lines = []
                first = True
                for k, v in item.items():
                    prefix = f"{pad}- " if first else f"{pad}  "
                    lines.append(f"{prefix}{k}: {_format_yaml_value(v, indent + 1)}")
                    first = False
                out.append("\n".join(lines))
            else:
                out.append(f"{pad}- {_format_yaml_value(item, indent + 1)}")
        return "\n" + "\n".join(out)
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            lines.append(f"{pad}{k}: {_format_yaml_value(v, indent + 1)}")
        return "\n" + "\n".join(lines)
    return json.dumps(value)


def render(handoff: HandoffInput, *, started_at: dt.datetime | None = None) -> str:
    """Render frontmatter + narrative. Does real I/O (git state, and now a
    best-effort atoms-store read) despite the name -- see `write()` for the
    only caller that matters; this stays a separate function so tests can
    render without touching disk beyond what `detect_layout` needs.
    """
    layout = detect_layout()
    git = _git_state(layout.root)
    now = dt.datetime.now(dt.UTC)
    started = started_at or now
    session_id = handoff.session_id or f"sess-{uuid.uuid4().hex[:8]}"

    # A caller that already knows what it consulted/drafted (a skill with
    # richer session context than the CLI has) is respected as-is -- supplying
    # either field at all opts out of auto-detection for both, so a curated
    # list is never silently topped up with inferred data the caller didn't
    # ask for. Otherwise -- the common case, since `meristem handoff` exposes
    # no flags for these -- fill from what actually happened since the last
    # checkpoint.
    atoms_consulted = handoff.atoms_consulted
    atoms_drafted = handoff.atoms_drafted
    if not atoms_consulted and not atoms_drafted:
        since_ts = _previous_handoff_since(layout)
        atoms_consulted, atoms_drafted = _auto_detect_atoms(layout, since_ts)

    fields: dict[str, Any] = {
        "session_id": session_id,
        "started_at": started.strftime("%Y-%m-%dT%H:%MZ"),
        "ended_at":   now.strftime("%Y-%m-%dT%H:%MZ"),
        "ended_reason": handoff.ended_reason,
        "context_pct_at_end": handoff.context_pct_at_end,
        "branch": git["branch"],
        "last_commit_sha": git["last_commit_sha"][:7] if git["last_commit_sha"] else None,
        "dirty_files": git["dirty_files"],
        "current_tick": {
            "id": handoff.current_tick_id or session_id,
            "title": handoff.current_tick_title or "(untitled)",
            "status": handoff.current_tick_status,
            "phase": handoff.current_tick_phase,
        },
        "atoms_consulted": atoms_consulted,
        "atoms_drafted": atoms_drafted,
        "open_questions": handoff.open_questions,
        "next_action": handoff.next_action or "(no next action specified)",
        "failed_assumptions": handoff.failed_assumptions,
        "do_not_redo": handoff.do_not_redo,
        "edge_cases_pending": handoff.edge_cases_pending,
    }
    yaml_body = "\n".join(
        f"{k}: {_format_yaml_value(v)}" for k, v in fields.items()
    )
    narrative = _truncate_narrative(handoff.narrative or "(no narrative supplied)")
    return f"---\n{yaml_body}\n---\n\n## Narrative\n\n{narrative}\n"


def write(handoff: HandoffInput, *, started_at: dt.datetime | None = None) -> Path:
    """Render + write the handoff. Returns the file path. Refreshes LATEST.md."""
    layout = detect_layout()
    layout.handoffs.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.UTC)
    session_id = handoff.session_id or f"sess-{uuid.uuid4().hex[:8]}"
    handoff.session_id = session_id
    filename = f"{now.strftime('%Y-%m-%dT%H%MZ')}-{session_id}.md"
    path = layout.handoffs / filename
    path.write_text(render(handoff, started_at=started_at), encoding="utf-8")

    _record_session(layout, handoff, path, now=now, started_at=started_at)

    latest = layout.handoffs / "LATEST.md"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    try:
        os.symlink(filename, latest)
    except OSError:
        # Symlinks unavailable (Windows without privilege) — fall back to copy.
        latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _record_session(
    layout: Layout,
    handoff: HandoffInput,
    path: Path,
    *,
    now: dt.datetime,
    started_at: dt.datetime | None,
) -> None:
    """Mirror this handoff into `session_state`.

    Best-effort by design. A handoff is the anti-hallucination safety net —
    the thing written precisely when a session is failing — so an unopenable
    or unmigrated store must never be the reason one fails to get written.
    The markdown file is the durable record; this row is the queryable index
    over it.
    """
    import sqlite3

    from . import store

    if not layout.db.exists():
        return
    try:
        conn = store.connect(layout.db)
    except (sqlite3.Error, OSError):
        return
    try:
        git = _git_state(layout.root)
        store.record_session(
            conn,
            session_id=handoff.session_id or "",
            started_at=int((started_at or now).timestamp()),
            ended_at=int(now.timestamp()),
            ended_reason=handoff.ended_reason,
            context_pct_end=handoff.context_pct_at_end,
            handoff_path=str(path),
            branch=git["branch"],
            last_commit_sha=git["last_commit_sha"],
        )
        conn.commit()
    except (sqlite3.Error, OSError):
        # Environmental only — an unmigrated or locked store must not stop a
        # handoff being written. Programming errors deliberately propagate:
        # the first draft of this caught bare `Exception` and swallowed a
        # NameError (`_git_context` for `_git_state`) that left session_state
        # empty while every handoff reported success. A silent no-op that
        # cannot be detected is the failure mode this project has now been
        # bitten by three times.
        pass
    finally:
        conn.close()


__all__ = ["HandoffInput", "NARRATIVE_TOKEN_CAP", "render", "write"]
