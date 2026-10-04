"""Index freshness — how far behind the repo the substrate has fallen (SPEC §19).

Liveness (§14.5) can only ever *shrink* a store: it drops atoms whose predicate
stopped matching. Nothing in Meristem could add what a repo grew since the last
manual `meristem ingest`, so a store drifts silently and answers with equal
confidence at 0 commits behind and at 56 — which is exactly what a live
workspace was measured doing on 2026-08-05.

This module supplies the number that makes drift sayable, and the small amount
of state that makes acting on it safe to trigger automatically:

  * `workspace_drift()` — per-root commits-behind, dirty-file count, sha state.
  * `SyncState` / `debounce_remaining()` — don't re-ingest on every commit of a
    30-commit push.
  * `sync_lock()` — one ingest at a time, so a backgrounded post-commit hook
    firing twice can't fight itself for the write lock.

Everything here is read-only against the store; `meristem sync` owns the writes.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import git_utils
from .paths import Layout

SYNC_STATE_FILE = "sync.json"
SYNC_LOCK_FILE = "sync.lock"

# Don't re-ingest more than once per window. A `git push` of 30 commits fires
# post-commit 30 times; a rebase or a scripted series fires it faster still.
DEFAULT_DEBOUNCE_SECONDS = 90

# A lock older than this is assumed to belong to a process that died mid-ingest.
# Generous, because a first ingest of a large repo legitimately takes minutes.
LOCK_STALE_SECONDS = 15 * 60


@dataclass(frozen=True)
class RepoDrift:
    """How stale one registered root's index is.

    `commits_behind is None` means *could not tell*, never *zero*. Two causes:
    the root is not a git repo (no drift concept applies — `is_git` is False),
    or the stored sha no longer resolves (`is_git` is True and the index is
    unreachable by any incremental path — a full re-ingest is the only fix).
    """

    repo_id: str
    root: Path
    exists: bool
    is_git: bool
    last_indexed_sha: str | None
    last_indexed_at: int | None
    head_sha: str | None
    commits_behind: int | None
    dirty: int

    @property
    def never_indexed(self) -> bool:
        """No ingest has ever completed against this root.

        Keyed on the timestamp, not the sha: `mark_indexed` records whatever
        `head_sha` returned, which is NULL for a non-git root — so a non-git
        workspace with a perfectly successful ingest has no sha and never will.
        `last_indexed_at` is written by every completed ingest regardless.
        """
        return self.last_indexed_at is None

    @property
    def unknown_base(self) -> bool:
        """Indexed against a sha git can no longer resolve."""
        return (
            self.is_git
            and self.last_indexed_sha is not None
            and self.commits_behind is None
        )

    @property
    def indexed_without_head(self) -> bool:
        """Indexed when the repo had no commits yet — and it has some now.

        `mark_indexed` stores whatever `head_sha()` returned, which is NULL for
        a git repo with zero commits. That is a real, reachable state: any
        workspace that ran `meristem init --all` in a freshly `git init`-ed
        directory before its first commit lands here.

        Nothing else catches it. `commits_between` cannot measure from a NULL
        base, so `commits_behind` stays None; `unknown_base` does not apply
        (it requires a sha git cannot resolve, and here there is no sha at
        all); `never_indexed` is False because the ingest really did complete.
        So `behind` reads 0 and the root reports itself "current with HEAD"
        forever, while `meristem sync` skips it on every run no matter how many
        commits land — the index is measuring against a baseline of nothing and
        calling it a clean bill.

        That is exactly the silent staleness this module exists to end, so it
        counts as drift until an ingest records a real HEAD. Found 2026-09-11
        on a real workspace that had been `git init`-ed and indexed in the
        same session, then committed four times and still read as current.
        """
        return (
            self.is_git
            and self.last_indexed_at is not None
            and self.last_indexed_sha is None
            and self.head_sha is not None
        )

    @property
    def behind(self) -> int:
        """Commits behind, counting unknown as 0 — for arithmetic only.

        Callers deciding severity must consult `unknown_base` / `never_indexed`
        separately; folding them in here would let an unanswerable question
        render as a clean zero.
        """
        return self.commits_behind or 0

    @property
    def drifted(self) -> bool:
        """Is there committed work this index has not seen?

        False outside a git repo, and that is not a dodge: there is no HEAD to
        be behind, so "behind" has no referent. Reporting drift there would fail
        every non-git workspace permanently, on a question that was never asked.

        Deliberately not influenced by `dirty` either. A dirty working tree is
        the normal state of active development, so treating it as drift would
        make this fire on nearly every check — and a signal that fires
        constantly is one people stop reading, which is how `edges.density` went
        unheard while two real workspaces ran edgeless for months. Dirty files
        are reported alongside the number, and `meristem sync` picks them up when it
        runs, but they never on their own claim the index is behind.
        """
        if not (self.exists and self.is_git):
            return False
        return (
            self.never_indexed
            or self.unknown_base
            or self.indexed_without_head
            or self.behind > 0
        )

    def describe(self) -> str:
        """One clause, first person, from the index's point of view."""
        if not self.exists:
            return "root no longer exists on disk"
        if not self.is_git:
            return "not a git repo — drift cannot be measured"
        if self.never_indexed:
            return "never indexed"
        if self.unknown_base:
            # `unknown_base` is only true when last_indexed_sha is not None;
            # that lives in the property, which mypy cannot see through.
            sha = self.last_indexed_sha or "?"
            return f"indexed at {sha[:7]}, which git cannot resolve"
        if self.indexed_without_head:
            return "indexed before the repo's first commit — needs a full re-ingest"
        if self.behind == 0:
            return "current with HEAD"
        return f"{self.behind} commit(s) behind HEAD"


def _countable_dirty(paths: set[str] | None) -> int:
    """Uncommitted paths, excluding Meristem's own state directory.

    `.meristem/atoms.sqlite` and its WAL change on every ingest by
    construction, so counting them would pin the dirty count permanently
    above zero — a number that can never be clean is a number nobody reads.
    """
    if not paths:
        return 0
    return sum(
        1 for p in paths
        if not p.startswith((".meristem/", ".dlms/")) and p not in (".meristem", ".dlms")
    )


def drift_for(
    root: Path,
    *,
    repo_id: str,
    last_indexed_sha: str | None,
    last_indexed_at: int | None,
) -> RepoDrift:
    exists = root.exists()
    is_git = git_utils.is_repo(root) if exists else False
    head = git_utils.head_sha(root) if is_git else None
    behind = git_utils.commits_between(root, last_indexed_sha) if is_git else None
    dirty = git_utils.dirty_paths(root) if is_git else None
    return RepoDrift(
        repo_id=repo_id,
        root=root,
        exists=exists,
        is_git=is_git,
        last_indexed_sha=last_indexed_sha,
        last_indexed_at=last_indexed_at,
        head_sha=head,
        commits_behind=behind,
        dirty=_countable_dirty(dirty),
    )


def workspace_drift(conn: sqlite3.Connection) -> list[RepoDrift]:
    """Drift for every root registered in `repo_state`, in stable order.

    One `git rev-list --count` + one `git status` per root. Both are indexed
    operations against the object store, so this stays cheap enough for the
    SessionStart digest and the per-turn statusline.
    """
    rows = conn.execute(
        "SELECT repo_id, root_path, last_indexed_sha, last_indexed_at"
        "  FROM repo_state ORDER BY repo_id"
    ).fetchall()
    return [
        drift_for(
            Path(r["root_path"]),
            repo_id=r["repo_id"],
            last_indexed_sha=r["last_indexed_sha"],
            last_indexed_at=r["last_indexed_at"],
        )
        for r in rows
    ]


def total_behind(drifts: list[RepoDrift]) -> int:
    return sum(d.behind for d in drifts)


def summarize(drifts: list[RepoDrift]) -> str | None:
    """The headline sentence, or ``None`` when every root is current.

    Phrased so an agent reading it at token zero knows the answer it is about to
    get may predate the code in front of it.
    """
    stale = [d for d in drifts if d.drifted]
    if not stale:
        return None
    parts: list[str] = []
    behind_roots = [d for d in stale if d.behind > 0]
    if behind_roots:
        total = total_behind(behind_roots)
        roots = "root" if len(behind_roots) == 1 else "roots"
        parts.append(f"index is {total} commit(s) behind HEAD across {len(behind_roots)} {roots}")
    if unknown := [d for d in stale if d.unknown_base]:
        parts.append(f"{len(unknown)} root(s) indexed against a sha git cannot resolve")
    if never := [d for d in stale if d.never_indexed]:
        parts.append(f"{len(never)} root(s) never indexed")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# sync state — debounce + lock (sidecar files, no schema version needed)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SyncState:
    last_sync_at: int = 0
    last_head: dict[str, str] | None = None

    def to_dict(self) -> dict:
        return {"last_sync_at": self.last_sync_at, "last_head": self.last_head or {}}


def _state_path(layout: Layout) -> Path:
    return layout.state_dir / SYNC_STATE_FILE


def read_sync_state(layout: Layout) -> SyncState:
    p = _state_path(layout)
    if not p.exists():
        return SyncState()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SyncState()  # a corrupt sidecar must never block a sync
    return SyncState(
        last_sync_at=int(raw.get("last_sync_at", 0) or 0),
        last_head=raw.get("last_head") or {},
    )


def write_sync_state(layout: Layout, drifts: list[RepoDrift], *, now: int | None = None) -> None:
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    state = SyncState(
        last_sync_at=int(now if now is not None else time.time()),
        last_head={d.repo_id: d.head_sha for d in drifts if d.head_sha},
    )
    _state_path(layout).write_text(json.dumps(state.to_dict()), encoding="utf-8")


def debounce_remaining(
    layout: Layout, *, window: int = DEFAULT_DEBOUNCE_SECONDS, now: float | None = None
) -> int:
    """Seconds left before another sync is allowed. 0 means go."""
    state = read_sync_state(layout)
    if not state.last_sync_at:
        return 0
    elapsed = (now if now is not None else time.time()) - state.last_sync_at
    return max(0, int(window - elapsed))


class SyncLock:
    """Best-effort single-writer guard for `meristem sync`.

    `O_EXCL` creation, pid + timestamp inside, stale locks reclaimed after
    `LOCK_STALE_SECONDS`. Advisory only: SQLite remains the real arbiter of
    concurrent writes. This exists so a fire-and-forget post-commit hook that
    fires twice does two cheap no-ops instead of two ingests racing for the
    write lock — a lock we fail to take is a reason to skip, never to error.
    """

    def __init__(self, layout: Layout) -> None:
        self.path = layout.state_dir / SYNC_LOCK_FILE
        self.acquired = False

    def _stale(self) -> bool:
        try:
            return (time.time() - self.path.stat().st_mtime) > LOCK_STALE_SECONDS
        except OSError:
            return False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):  # second pass runs only after reclaiming a stale lock
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if not self._stale():
                    return False
                try:
                    self.path.unlink()
                except OSError:
                    return False
                continue
            except OSError:
                return False
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"pid": os.getpid(), "at": int(time.time())}))
            self.acquired = True
            return True
        return False

    def release(self) -> None:
        if not self.acquired:
            return
        with contextlib.suppress(OSError):
            self.path.unlink()
        self.acquired = False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


__all__ = [
    "DEFAULT_DEBOUNCE_SECONDS",
    "RepoDrift",
    "SyncLock",
    "SyncState",
    "debounce_remaining",
    "drift_for",
    "read_sync_state",
    "summarize",
    "total_behind",
    "workspace_drift",
    "write_sync_state",
]
