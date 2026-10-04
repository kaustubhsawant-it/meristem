"""Owner ingester — git history → `owner` atoms + OWNS edges (SPEC §4 rule 6).

`owner` was one of the four atom types nothing produced, which quietly disabled
two things: SPEC §15's `/meristem:plan` is specified to retrieve "invariants, closed
decisions, owners", and `/meristem:trace` advertises "who writes Y". Both answered
from an empty set.

Ownership here is empirical rather than declared — who actually touches a
subsystem, from `git log`, not a CODEOWNERS file someone forgot to update. One
atom per (module, dominant author) with a share above a floor, so a directory
with genuinely shared ownership yields several owners rather than a misleading
single name.

Staleness is handled honestly through `valid_from`: it is set to that author's
*most recent* commit in the directory, so decay (§14.5) sinks the claim as the
ownership ages. Someone who last touched a subsystem two years ago stops
ranking as its owner without anyone having to retract the fact.

There is deliberately no liveness predicate. A regex over file content cannot
verify a claim about people, and inventing one would produce exactly the
unverifiable-but-looks-verified atom the liveness work exists to prevent.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path

from ..atoms import assert_fact
from ..edges import upsert_edge
from .base import IngestContext, IngestResult

# An author must own at least this share of a directory's commits to be named.
MIN_SHARE = 0.25
# Never name more than this many owners for one directory.
MAX_OWNERS_PER_DIR = 3
# Commits scanned per directory.
LOOKBACK = 300
# Directories examined per run.
MAX_DIRS = 40
# Child atoms linked per owner via OWNS.
MAX_OWNED_ATOMS = 25

# ".dlms" is the legacy (pre-rename) state dir — excluded too in case one is
# still sitting around from before a workspace was re-init'd as Meristem.
_NEVER_MODULE = {
    ".git", ".meristem", ".dlms", ".github", ".vscode", "__pycache__", "node_modules",
}


def _git_authors(root: Path, reldir: str) -> list[tuple[str, int]]:
    """(author, unix_ts) for recent commits touching `reldir`, newest first."""
    try:
        out = subprocess.run(
            ["git", "log", "-n", str(LOOKBACK), "--no-merges",
             "--pretty=format:%an%x00%at", "--", reldir],
            cwd=root, check=True, capture_output=True, text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []
    rows: list[tuple[str, int]] = []
    for line in out.splitlines():
        name, _, ts = line.partition("\x00")
        if not name.strip():
            continue
        try:
            rows.append((name.strip(), int(ts)))
        except ValueError:
            continue
    return rows


def _dominant(rows: list[tuple[str, int]]) -> tuple[list[tuple[str, int, float, int]], int]:
    """(authors, total_commits) — authors clearing MIN_SHARE, richest first.

    Each author entry is (name, commit_count, share, latest_ts).
    """
    counts: dict[str, int] = defaultdict(int)
    latest: dict[str, int] = {}
    for name, ts in rows:
        counts[name] += 1
        latest[name] = max(latest.get(name, 0), ts)
    total = sum(counts.values()) or 1
    ranked = sorted(
        (
            (name, n, n / total, latest[name])
            for name, n in counts.items()
            if n / total >= MIN_SHARE
        ),
        # Commit count first; name breaks ties so repeated runs are identical.
        key=lambda t: (-t[1], t[0]),
    )
    return ranked[:MAX_OWNERS_PER_DIR], total


def run(ctx: IngestContext) -> IngestResult:
    res = IngestResult(name="owners")
    if not (ctx.root / ".git").exists() and not _is_repo(ctx.root):
        res.notes.append("not a git repository")
        return res

    excl = set(ctx.exclude) | _NEVER_MODULE
    if ctx.changed_files is None:
        # Full scan: every top-level directory.
        dirs = sorted(
            d for d in ctx.root.iterdir()
            if d.is_dir() and d.name not in excl and not d.name.startswith(".")
        )
    else:
        # Incremental (SPEC §14.2): only top-level dirs that own a changed
        # file — derived from the changed set, no full-tree `git log` scan
        # (mirrors modules.py's changed-dir derivation).
        tops: set[str] = set()
        for rel in ctx.changed_files:
            parts = Path(rel).parts
            if len(parts) < 2:
                continue  # a root-level file has no owning directory
            top = parts[0]
            if top in excl or top.startswith("."):
                continue
            tops.add(top)
        dirs = sorted(d for d in (ctx.root / t for t in tops) if d.is_dir())
    if not dirs:
        res.notes.append("no top-level directories to attribute")
        return res
    if len(dirs) > MAX_DIRS:
        res.notes.append(f"attributing the first {MAX_DIRS} of {len(dirs)} directories")
        res.truncated = True
        dirs = dirs[:MAX_DIRS]

    for d in dirs:
        rel = ctx.rel_path(d)
        owners, total = _dominant(_git_authors(ctx.root, d.name))
        for author, count, share, latest_ts in owners:
            atom = assert_fact(
                ctx.conn,
                type="owner",
                topic_key=f"owner:{rel}:{author}",
                summary_10w=f"{author} owns {rel}/",
                summary_50w=(
                    f"{author} is a principal author of `{rel}/` — {count} of the "
                    f"last {total} commits there ({share * 100:.0f}%)."
                ),
                summary_250w=(
                    f"{author} authored {count} commits touching `{rel}/` "
                    f"({share * 100:.0f}% of recent activity in that directory). "
                    f"Ownership is inferred from git history, not declared."
                ),
                source_kind="commit",
                source_ref=rel,
                # Decay measures the fact's age (§14.5), so dating this to the
                # author's last commit here lets stale ownership sink on its own.
                valid_from=latest_ts or None,
                confidence=min(1.0, 0.5 + share / 2),
                repo_id=ctx.repo_id,
                workspace_id=ctx.workspace_id,
            )
            res.atoms_inserted += 1
            res.notes.append(
                f"{rel}/ → {author} ({share * 100:.0f}%), "
                f"{_link_owned(ctx, owner_id=atom.id, reldir=rel)} atoms"
            )
    return res


def _is_repo(root: Path) -> bool:
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root, check=True, capture_output=True, text=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


def _link_owned(ctx: IngestContext, *, owner_id: str, reldir: str) -> int:
    """OWNS edges from the owner atom to the atoms under that directory.

    Directed person→area, per SPEC §4. This makes "who owns the code I'm about
    to change?" a graph traversal rather than a separate lookup, so PPR can
    surface the owner alongside the code itself.
    """
    rows = ctx.conn.execute(
        """SELECT id FROM live_atoms
            WHERE id != ? AND source_ref IS NOT NULL
              AND (source_ref = ? OR source_ref LIKE ?)
            LIMIT ?""",
        (owner_id, reldir, f"{reldir}/%", MAX_OWNED_ATOMS),
    ).fetchall()
    linked = 0
    for r in rows:
        try:
            upsert_edge(
                ctx.conn,
                src_id=owner_id,
                dst_id=r["id"],
                kind="OWNS",
                source="git_blame",
                weight=0.7,
                evidence=[("file_path", reldir)],
            )
            linked += 1
        except (KeyError, ValueError):
            continue
    return linked
