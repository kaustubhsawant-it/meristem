"""Git ingester — recent commits → CLOSED decision atoms.

"Commits are atoms" (SPEC §1, principle #1). Each commit since
``last_indexed_sha`` (or, on a fresh repo, the last DEFAULT_LOOKBACK commits)
becomes one ``decision`` atom with ``decision_status='CLOSED'`` — the commit
*is* the close-out of a decision. The atom's topic_key is unique per SHA
(``commit:<short_sha>``) so commits never supersede one another.

Provenance: ``source_kind='commit'``, ``source_ref=<sha>``. The 50-word
summary is the subject + first author line; the 250-word summary appends
the wrapped commit body. ``valid_from`` is the commit's author timestamp.

No liveness check — a commit is immutable; if it disappears the repo
itself was rewritten, which is out of scope.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..atoms import assert_fact
from .base import IngestContext, IngestResult

DEFAULT_LOOKBACK = 50
# The first-run branch is already bounded by DEFAULT_LOOKBACK via `-n`, but the
# incremental branch's `last_indexed_sha..HEAD` range has no such limit — a
# workspace that goes a long time between ingests (or imports history) can
# hand back thousands of commits in one range. Capped to the newest N here;
# `last_indexed_sha` still advances to HEAD like every other designed cap
# (IngestResult.budget_deferred's docstring) rather than holding it back,
# since the range does not shrink between runs — holding it would re-derive
# the identical oversized range next time and never make progress.
MAX_INCREMENTAL_COMMITS = 500

# Field separator + record terminator are NULs so commit bodies containing
# anything (including the format markers) parse cleanly.
_FMT = "%H%x00%h%x00%an%x00%at%x00%s%x00%b"
_REC_SEP = "\x1e"  # ASCII Record Separator


@dataclass
class _Commit:
    sha: str
    short: str
    author: str
    ts: int
    subject: str
    body: str


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )
        return out.stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _parse_commits(raw: str) -> list[_Commit]:
    out: list[_Commit] = []
    for chunk in raw.split(_REC_SEP):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        parts = chunk.split("\x00")
        if len(parts) < 6:
            continue
        sha, short, author, ts, subject, body = parts[:6]
        try:
            ts_i = int(ts)
        except ValueError:
            ts_i = 0
        out.append(_Commit(sha, short, author, ts_i, subject.strip(), body.strip()))
    return out


def run(ctx: IngestContext) -> IngestResult:
    res = IngestResult(name="git")
    # Verify the root is actually a git checkout — otherwise skip silently.
    if _git(["rev-parse", "--is-inside-work-tree"], ctx.root) is None:
        res.notes.append("not a git repository")
        return res

    if ctx.last_indexed_sha:
        rev_range = f"{ctx.last_indexed_sha}..HEAD"
        args = ["log", rev_range, f"--pretty=format:{_FMT}{_REC_SEP}"]
    else:
        args = ["log", "-n", str(DEFAULT_LOOKBACK), f"--pretty=format:{_FMT}{_REC_SEP}"]
    raw = _git(args, ctx.root)
    if raw is None:
        # last_indexed_sha may not exist (rebase / force-push) — fall back.
        if ctx.last_indexed_sha:
            args = ["log", "-n", str(DEFAULT_LOOKBACK), f"--pretty=format:{_FMT}{_REC_SEP}"]
            raw = _git(args, ctx.root)
            if raw is None:
                res.notes.append("git log failed")
                return res
            res.notes.append("last_indexed_sha unreachable; fell back to lookback")
        else:
            res.notes.append("git log failed")
            return res

    commits = _parse_commits(raw)
    if not commits:
        res.notes.append("no new commits")
        return res
    if len(commits) > MAX_INCREMENTAL_COMMITS:
        total = len(commits)
        commits = commits[:MAX_INCREMENTAL_COMMITS]  # `git log` lists newest-first
        res.notes.append(
            f"capped at {MAX_INCREMENTAL_COMMITS} of {total} commits in this "
            f"catch-up range; {total - MAX_INCREMENTAL_COMMITS} older commit(s) "
            f"not ingested"
        )
        res.truncated = True

    for c in commits:
        body_one_line = " ".join(c.body.split()) if c.body else ""
        summary_10 = c.subject[:80]
        summary_50 = f"{c.subject} — {c.author} ({c.short})"
        summary_250 = (
            f"{c.subject}\n\nAuthor: {c.author}  SHA: {c.short}\n\n{body_one_line}"
        ).strip()
        assert_fact(
            ctx.conn,
            type="decision",
            decision_status="CLOSED",
            # A commit reports what happened; it forbids nothing. Marking it
            # explicitly keeps it out of constraint retrieval (SPEC §4).
            decision_class="change",
            topic_key=f"commit:{c.short}",
            summary_10w=summary_10,
            summary_50w=summary_50,
            summary_250w=summary_250,
            source_kind="commit",
            source_ref=c.sha,
            valid_from=c.ts or None,
            repo_id=ctx.repo_id,
            workspace_id=ctx.workspace_id,
        )
        res.atoms_inserted += 1
    return res
