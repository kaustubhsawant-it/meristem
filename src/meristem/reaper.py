"""Close atoms whose source was deleted from the repo (SPEC §14.5).

Why this exists
---------------
Ingestion is additive. An atom asserted from `svc/legacy_billing.py` stays
`valid_to IS NULL` after that file is deleted, because no adapter ever revisits
a path that is no longer there. Five sessions of handoffs listed "deletion
reaper" under `edge_cases_pending` and none of them built it.

Measured on a real workspace: delete a file, commit, re-ingest — the atom for
its symbol is still live.

Lazy liveness (§14.5) is a partial safety net and it is worth being precise
about how partial. An atom with a `regex` predicate pointed at a deleted file
fails verification at read time and is dropped, so it does not reach a reader.
But:

* it stays in the store as live, inflating every census `doctor` reports;
* it stays in the PPR universe. `_seed_set` runs off `embeddings.top_k`, which
  does not verify liveness, so a dead atom can still *seed* retrieval and pull
  its neighbourhood into the candidate set — shaping what does get returned;
* every read pays to re-run its predicate against a missing file, forever;
* atoms with **no** predicate are not covered at all. `module:` and `owner:`
  atoms carry none, so a deleted subsystem keeps a live module atom — and
  module atoms are the highest-degree nodes in the graph, which is exactly the
  kind that keeps seeding drill-down into a subsystem that no longer exists.

Reaping closes the atom (`valid_to`), it does not delete the row. The history
model in `atoms.py` is append-only and this respects it: the fact was true, and
now it has an end date.

Positive evidence only
----------------------
This closes knowledge, so it acts on deletions git *asserts*, never on "the
path isn't on disk". `source_ref` is not uniformly a path — `commit:` atoms
carry a 40-char sha, and no column distinguishes them — so an
absence-of-file test would reap every commit atom in the store. Anything the
reaper cannot positively tie to a deleted path is left alone.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import git_utils

# `source_ref` for symbol-tier atoms carries a line number: `svc/foo.py:120`.
# Anchored at the end so a Windows-style `C:\...` or a `doc:README.md` topic
# prefix cannot be mistaken for one.
_LINE_SUFFIX = re.compile(r":\d+$")


def source_path(source_ref: str | None) -> str | None:
    """The repo-relative path part of a `source_ref`, or None if it isn't one.

    Only strips a trailing `:<line>`. Deciding *whether* a ref is a path is not
    done here and must not be — that judgement belongs to the caller comparing
    against a set of paths git reported as deleted.
    """
    if not source_ref:
        return None
    return _LINE_SUFFIX.sub("", source_ref) or None


@dataclass(frozen=True)
class Reaped:
    """What one reap pass closed, and what it could not judge."""

    atom_ids: list[str] = field(default_factory=list)
    # Directory-backed atoms closed because no tracked file remains under them.
    dir_atom_ids: list[str] = field(default_factory=list)
    deleted_paths: set[str] = field(default_factory=set)
    # True when git could not report deletions. Distinct from "nothing was
    # deleted": one means no atoms should be closed, the other means none needed
    # closing, and a caller reporting them the same way would claim a clean
    # sweep it never performed.
    undetermined: bool = False

    @property
    def count(self) -> int:
        return len(self.atom_ids) + len(self.dir_atom_ids)


def _dir_backed_candidates(
    conn: sqlite3.Connection, repo_id: str | None
) -> list[tuple[str, str]]:
    """Live `module:`/`owner:` atoms as (atom_id, path).

    These carry no liveness predicate, so nothing else in the system can ever
    invalidate them.
    """
    sql = (
        "SELECT id, source_ref FROM live_atoms"
        " WHERE source_ref IS NOT NULL"
        "   AND (topic_key LIKE 'module:%' OR topic_key LIKE 'owner:%')"
    )
    params: list[object] = []
    if repo_id is not None:
        sql += " AND repo_id = ?"
        params.append(repo_id)
    out = []
    for r in conn.execute(sql, params):
        if (p := source_path(r["source_ref"])):
            out.append((r["id"], p))
    return out


def reap(
    conn: sqlite3.Connection,
    root: Path,
    *,
    since_sha: str | None,
    repo_id: str | None = None,
    dry_run: bool = False,
    now: int | None = None,
) -> Reaped:
    """Close atoms whose source path git reports as deleted since `since_sha`.

    `since_sha` is the root's `last_indexed_sha`. With no sha there is no
    interval to diff and nothing is reaped — a first ingest has no deletions by
    definition.
    """
    now = now if now is not None else int(time.time())
    deleted = git_utils.deleted_files_since(root, since_sha)
    if deleted is None:
        # Could not tell. Close nothing and say so — an unanswerable question
        # must not render as a clean pass, the same rule doctor applies to a
        # check that could not run.
        return Reaped(undetermined=True)

    file_ids: list[str] = []
    if deleted:
        sql = "SELECT id, source_ref FROM live_atoms WHERE source_ref IS NOT NULL"
        params: list[object] = []
        if repo_id is not None:
            sql += " AND repo_id = ?"
            params.append(repo_id)
        for r in conn.execute(sql, params):
            if (p := source_path(r["source_ref"])) and p in deleted:
                file_ids.append(r["id"])

    # Directory-backed atoms: git reports deleted files, never deleted
    # directories, so these need their own positive test — "no tracked file
    # remains under this prefix". Only considered when something was actually
    # deleted, so an unrelated ingest never walks every module atom.
    dir_ids: list[str] = []
    if deleted:
        touched_dirs = {str(Path(p).parent) for p in deleted}
        for atom_id, path in _dir_backed_candidates(conn, repo_id):
            if path not in touched_dirs and not any(
                d == path or d.startswith(f"{path}/") for d in touched_dirs
            ):
                continue
            remaining = git_utils.tracked_under(root, path)
            if remaining == 0:
                dir_ids.append(atom_id)

    if not dry_run and (file_ids or dir_ids):
        with conn:
            conn.executemany(
                "UPDATE atoms SET valid_to = COALESCE(valid_to, ?), updated_at = ?"
                " WHERE id = ? AND valid_to IS NULL",
                [(now, now, aid) for aid in (*file_ids, *dir_ids)],
            )
    return Reaped(
        atom_ids=file_ids,
        dir_atom_ids=dir_ids,
        deleted_paths=deleted,
    )


__all__ = ["Reaped", "reap", "source_path"]
