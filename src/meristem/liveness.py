"""Liveness predicate runner (SPEC §3).

Every atom may carry a liveness predicate: a small machine-checkable assertion
that the world *still* matches the claim. When the predicate fails, the atom
is "stale" — surfaced in the Memory Pulse statusline (`⚠ N stale`).

Four kinds now have runners: `regex` (file-content grep), `sql` (query
against the local atoms store, for self-referential facts, added
2026-08-07), `ast` (tree-sitter query against the parsed file — SPEC §16,
added 2026-08-12, see `treesitter.py`), and `none` (trivially passes).

`ast` is only *conditionally* runnable: it needs both `tree-sitter` core and
the specific file's language grammar importable. Unlike `regex`/`sql`, that
capability varies per environment and per atom, so it is checked dynamically
in `_check_ast` rather than declared statically — see `UNRUNNABLE_KINDS`.

On a passing check, `liveness_last_ok` is bumped to now. Failures don't write;
freshness is inferred from absence (predicate ran today, last_ok < today → stale).
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# The honest outcomes of running a predicate (SPEC §3).
#
#   none          — the atom carries no predicate; nothing was claimed
#   verified      — the predicate ran and matched
#   failed        — the predicate ran and did not match; the atom is stale
#   unverifiable  — the predicate could NOT be run right now
#
# `unverifiable` exists because collapsing it into either neighbour is a lie.
# Treating it as verified — which is what returning True did — makes an atom
# nothing checked indistinguishable in output from one that passed, and that is
# exactly the failure mode Meristem exists to prevent. Treating it as failed
# silently discards knowledge for the sake of a clean-looking report. Callers
# get the third state and decide.
LivenessState = Literal["none", "verified", "failed", "unverifiable", "unconfirmed"]

# Kinds with no runner *at all*, checked statically before dispatch. `ast` used
# to live here (no runner existed); now it has one, but whether it can run
# still depends on which tree-sitter grammar packages are installed, so that
# check happens dynamically inside `_check_ast` instead — see its `ok is None`
# case in `check_atom`. Kept as a frozenset (rather than deleted outright) as
# the extension point for a future kind that ships with no runner at all.
UNRUNNABLE_KINDS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LivenessResult:
    atom_id: str
    kind: str
    ok: bool
    reason: str         # human-readable
    checked_at: int
    state: LivenessState = "verified"

    @property
    def surfaceable(self) -> bool:
        """May this atom be shown? Only a predicate that actually *failed* hides
        it — "couldn't check" is not "known dead"."""
        return self.state != "failed"


def check_atom(
    conn: sqlite3.Connection,
    atom_id: str,
    *,
    repo_root: Path,
) -> LivenessResult:
    """Run the predicate attached to `atom_id`. Updates `liveness_last_ok` on
    pass. Raises `KeyError` if the atom doesn't exist.

    `repo_root` must be the *workspace* root (the directory holding
    `meristem.toml`). Every caller (`cli.py`/`watcher.py`/`mcp_server.py`/
    `retrieval.py` all pass `layout.root` uniformly) passes it that way, and
    it remains the base for most atoms — but it is no longer the *only* base
    tried. `ingesters/base.py`'s `rel_path` does NOT record every target
    workspace-relative, despite this docstring (and `rel_path`'s own, and
    `hooks.py`'s `_relative_within`) having claimed exactly that from
    2026-09-12 to 2026-09-23: `rel_path` tries the workspace root first, but
    falls back to the atom's own *ingest* root when the target doesn't
    resolve under the workspace root — which is silently always the case for
    a sibling root (`roots = [".", "../other_service"]`), since
    `Path.relative_to` never emits `..`. So a sibling-root atom's target is
    actually recorded relative to *its own* root, not the workspace root.
    That mismatch between what this function assumed and what `rel_path`
    actually writes false-staled every predicate ingested from a sibling
    root — 423 atoms in one real onboarded workspace, found 2026-09-23.
    Fixing `rel_path` instead (to always write workspace-relative, `..`s and
    all) was rejected: atom ids hash summary text that embeds the path, so
    changing what gets recorded re-mints ids for every affected atom, breaks
    every edge pointing at them, and desyncs any external read-only
    consumer whose views are built on the old ids.

    The resolver mirrors the writer instead. `_atom_base` below picks the
    base per atom: the workspace root, unless the atom's own
    registered root (`atoms.repo_id` -> `repo_state.root_path`) sits outside
    the workspace root's own directory (a sibling), in which case the atom's
    own root is used — that is the base `rel_path` actually used for it.
    An atom with no `repo_id` (or a `repo_id` with no `repo_state` row) has
    no known ingest root to prefer, so it keeps today's workspace-root-only
    behaviour. Exactly one base is ever tried. A first cut also retried the
    *other* base when the target was missing under the primary one; review
    showed that can report a genuinely deleted sibling-root file as
    "verified" whenever the workspace root happens to hold an unrelated file
    at the same relative path matching the same pattern — a confidently
    wrong answer, the failure this module exists to prevent — and measuring
    every real workspace with and without it changed zero verdicts, so it
    was dropped. A hand-written `../sibling/...` target still resolves: from
    the sibling's own root, `..` walks back out to the same file. The escape
    guard (`_registered_roots`) runs unchanged."""
    row = conn.execute(
        """SELECT liveness_kind, liveness_target, liveness_pattern, repo_id
             FROM atoms WHERE id = ?""",
        (atom_id,),
    ).fetchone()
    if not row:
        raise KeyError(f"atom not found: {atom_id}")

    kind = row["liveness_kind"]
    target = row["liveness_target"]
    pattern = row["liveness_pattern"]
    now = int(time.time())
    valid_roots = _registered_roots(conn, fallback=repo_root)
    base = _atom_base(conn, row["repo_id"], workspace_root=repo_root)

    if kind is None or kind == "none":
        # No predicate — trivially "live". Don't bump last_ok (nothing checked).
        return LivenessResult(atom_id, "none", True, "no predicate", now, state="none")

    if kind in UNRUNNABLE_KINDS:
        # No runner at all for this kind. Report it rather than raising: the
        # caller must be able to surface the atom *and* mark it unchecked.
        # `ok=True` keeps the atom visible; `state` is what tells the truth
        # about it. last_ok is NOT bumped — nothing ran, so it must not start
        # looking freshly verified.
        return LivenessResult(
            atom_id, kind, True,
            f"{kind} liveness has no runner yet — atom shown but unverified",
            now, state="unverifiable",
        )

    if kind == "regex":
        ok, reason = _check_regex(base, target, pattern, valid_roots=valid_roots)
    elif kind == "sql":
        ok, reason = _check_sql(conn, pattern)
    elif kind == "ast":
        ast_ok, reason = _check_ast(base, target, pattern, valid_roots=valid_roots)
        if ast_ok is None:
            # A runner exists, but this atom's language grammar isn't
            # importable right now — a per-atom, per-environment gap, not a
            # blanket "kind has no runner" one. Same honesty rule as above:
            # shown, never bumped, never mistaken for verified.
            return LivenessResult(atom_id, kind, True, reason, now, state="unverifiable")
        ok = ast_ok
    else:
        raise ValueError(f"unknown liveness kind: {kind}")

    if ok:
        with conn:
            conn.execute(
                "UPDATE atoms SET liveness_last_ok = ? WHERE id = ?",
                (now, atom_id),
            )
    return LivenessResult(
        atom_id, kind, ok, reason, now, state="verified" if ok else "failed"
    )


def _registered_roots(conn: sqlite3.Connection, *, fallback: Path) -> list[Path]:
    """Every directory a liveness target is allowed to resolve into: the
    workspace root (`fallback`) plus every root this workspace has actually
    registered in `repo_state`. A legitimate multi-root workspace's
    cross-root targets (e.g. `../billing_service/charge.py`) resolve
    outside the workspace root's own directory by design — this is what
    lets `_resolve_target` allow exactly that, while still rejecting an
    arbitrary escape (`../../../../etc/passwd`) that matches no registered
    root."""
    roots = {fallback.resolve()}
    for row in conn.execute("SELECT DISTINCT root_path FROM repo_state"):
        if row["root_path"]:
            roots.add(Path(row["root_path"]).resolve())
    return list(roots)


def _atom_base(
    conn: sqlite3.Connection, repo_id: str | None, *, workspace_root: Path
) -> Path:
    """The one base an atom's `liveness_target` resolves against, mirroring
    what `ingesters/base.py`'s `rel_path` actually wrote for it (2026-09-23).

    `rel_path` records a path relative to the workspace root when the file
    is inside it, and relative to the atom's own ingest root otherwise —
    which is always the case for a sibling root, since `Path.relative_to`
    can't express `..`. So: no `repo_id`, or a `repo_id` this workspace never
    registered in `repo_state` (nothing to compare) — workspace root, exactly
    the behaviour before this fix. A registered root inside the workspace
    root (the workspace root itself, or a child subdirectory) — workspace
    root, which is what `rel_path` used for it too. A registered root outside
    the workspace root (a sibling) — that root."""
    workspace_root = workspace_root.resolve()
    if not repo_id:
        return workspace_root
    row = conn.execute(
        "SELECT root_path FROM repo_state WHERE repo_id = ?", (repo_id,)
    ).fetchone()
    if not row or not row["root_path"]:
        return workspace_root
    own_root = Path(row["root_path"]).resolve()
    if own_root.is_relative_to(workspace_root):
        return workspace_root
    return own_root


def _resolve_target(
    repo_root: Path, target: str, *, valid_roots: list[Path] | None = None
) -> tuple[Path | None, str | None]:
    """Resolve `target` under `repo_root` with traversal protection. Returns
    (path, None) or (None, reason) — shared by every file-backed predicate
    kind (`regex`, `ast`) so the escape check can't drift between them.

    `valid_roots` (from `_registered_roots`) extends the allowed boundary
    beyond `repo_root` alone to every root this workspace has registered —
    without it, any cross-root target in a legitimate multi-root workspace
    would be rejected as an escape, since it resolves outside `repo_root`'s
    own directory by construction. Defaults to `[repo_root]` for callers
    (tests, anything single-root) that don't pass it."""
    full = (repo_root / target).resolve()
    candidates = valid_roots if valid_roots is not None else [repo_root.resolve()]
    if not any(full.is_relative_to(r) for r in candidates):
        return None, f"target {target!r} escapes repo root"
    if not full.exists():
        return None, f"target file missing: {target}"
    return full, None


def _check_regex(
    repo_root: Path,
    target: str | None,
    pattern: str | None,
    *,
    valid_roots: list[Path] | None = None,
) -> tuple[bool, str]:
    if not target or not pattern:
        return False, "regex liveness missing target or pattern"

    full, err = _resolve_target(repo_root, target, valid_roots=valid_roots)
    if full is None:
        assert err is not None
        return False, err
    try:
        text = full.read_text(errors="replace", encoding="utf-8")
    except OSError as e:
        return False, f"cannot read {target}: {e}"

    try:
        if re.search(pattern, text, re.MULTILINE):
            return True, "regex matched"
        return False, "regex did not match"
    except re.error as e:
        return False, f"invalid regex {pattern!r}: {e}"


def _check_ast(
    repo_root: Path,
    target: str | None,
    pattern: str | None,
    *,
    valid_roots: list[Path] | None = None,
) -> tuple[bool | None, str]:
    """Is the symbol named `pattern` still declared in `target`, per a live
    tree-sitter re-parse? Returns (None, reason) — not (False, reason) — when
    the predicate genuinely could not run (unknown suffix, or that language's
    grammar isn't importable), so the caller reports `unverifiable` rather
    than a false "stale".

    Language support is checked *before* touching the filesystem: it needs
    only the target's suffix, and a target whose language Meristem doesn't
    know how to parse is unverifiable regardless of whether the file itself
    still exists — unlike `regex`, where a missing file is a definite "the
    claim no longer holds", not "can't tell"."""
    if not target or not pattern:
        return False, "ast liveness missing target or pattern"

    from . import treesitter

    suffix = Path(target).suffix
    lang_key = treesitter.language_for_suffix(suffix)
    if lang_key is None:
        return None, f"no tree-sitter grammar mapped for suffix {suffix!r}"
    if not treesitter.is_available(lang_key):
        return None, f"tree-sitter grammar for {lang_key!r} is not installed"

    full, err = _resolve_target(repo_root, target, valid_roots=valid_roots)
    if full is None:
        assert err is not None
        return False, err
    try:
        text = full.read_text(errors="replace", encoding="utf-8")
    except OSError as e:
        return False, f"cannot read {target}: {e}"

    declared = treesitter.symbol_declared(lang_key, text, pattern)
    if declared is None:
        # Shouldn't happen given the availability check above — kept honest
        # rather than assumed in case a grammar unloads between the two.
        return None, f"tree-sitter grammar for {lang_key!r} is not installed"
    if declared:
        return True, "ast match"
    return False, f"symbol {pattern!r} no longer declared in {target}"


# A `sql` predicate is self-referential — it checks the atoms store itself
# ("does this edge/atom still exist"), not a file, so there is no `target` to
# resolve and no repo to escape. The one real risk is a predicate that isn't
# read-only: this runs unattended from `revalidate_sweep`, so a stray UPDATE or
# DELETE embedded in a "liveness check" would silently mutate the store on a
# background pass. Rejecting anything but a bare SELECT is the whole guard.
_SQL_SELECT_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)


def _check_sql(conn: sqlite3.Connection, pattern: str | None) -> tuple[bool, str]:
    if not pattern:
        return False, "sql liveness missing pattern"
    if not _SQL_SELECT_RE.match(pattern):
        return False, "sql liveness must be a read-only SELECT"
    try:
        row = conn.execute(pattern).fetchone()
    except sqlite3.Error as e:
        return False, f"invalid sql: {e}"
    if row is not None:
        return True, "sql returned a row"
    return False, "sql returned no rows"


@dataclass(frozen=True)
class SweepResult:
    checked: int                 # candidates considered this pass
    passed: int                  # predicate ran and matched
    failed: list[str]            # predicate ran and did not match
    # Predicate could not be run at all (currently only `ast`, and only when
    # that atom's language grammar isn't installed). Kept apart from `failed`
    # so a sweep doesn't report phantom staleness — and so the count of facts
    # Meristem cannot currently check is visible rather than buried.
    unverifiable: list[str] = field(default_factory=list)


def revalidate_sweep(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    limit: int = 50,
    threshold_seconds: int = 86_400,
) -> SweepResult:
    """Background incremental revalidation (SPEC §14.5).

    Re-checks only the `limit` *stalest* atoms (oldest `liveness_last_ok` first)
    whose predicate hasn't passed inside the threshold window — never an eager
    global scan over the whole store. `check_atom` bumps `liveness_last_ok` on
    pass, so repeated sweeps walk forward through the staleness frontier. An
    un-runnable predicate (`ast` with no grammar installed for that atom's
    language) is reported as `unverifiable` rather than counted against
    `failed` — it is a coverage gap, not evidence of staleness, and conflating
    the two would make the sweep report rot that isn't there."""
    candidates = stale_atoms(
        conn, threshold_seconds=threshold_seconds, limit=limit
    )
    passed = 0
    failed: list[str] = []
    unverifiable: list[str] = []
    for row in candidates:
        try:
            result = check_atom(conn, row["id"], repo_root=repo_root)
        except (KeyError, ValueError):
            # A missing atom or an invalid predicate kind is a data defect, not
            # a stale fact — but it genuinely cannot be verified either.
            unverifiable.append(row["id"])
            continue
        if result.state == "unverifiable":
            unverifiable.append(row["id"])
        elif result.ok:
            passed += 1
        else:
            failed.append(row["id"])
    return SweepResult(
        checked=len(candidates), passed=passed, failed=failed,
        unverifiable=unverifiable,
    )


def count_stale(
    conn: sqlite3.Connection,
    *,
    threshold_seconds: int = 86_400,
) -> int:
    """How many atoms are currently stale, with no row materialization.

    A `SELECT COUNT(*)` mirror of `stale_atoms`'s own WHERE clause. Exists
    for a caller that needs to know when the staleness frontier is clear
    (`cli.watch --all`'s multi-batch loop) without fetching every stale
    atom's row just to take `len()` of it."""
    cutoff = int(time.time()) - threshold_seconds
    return conn.execute(
        "SELECT COUNT(*) FROM live_atoms"
        " WHERE liveness_kind IS NOT NULL AND liveness_kind != 'none'"
        "   AND (liveness_last_ok IS NULL OR liveness_last_ok < ?)",
        (cutoff,),
    ).fetchone()[0]


def stale_atoms(
    conn: sqlite3.Connection,
    *,
    threshold_seconds: int = 86_400,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Atoms whose liveness predicate exists but hasn't passed in the threshold
    window (default 24h). Powers the Pulse `⚠ N stale` indicator. When `limit`
    is set the bound is pushed into SQL (stalest first), so a background sweep
    never materializes the whole staleness set — no eager global scan."""
    cutoff = int(time.time()) - threshold_seconds
    sql = """SELECT id, type, topic_key, liveness_kind, liveness_last_ok
               FROM live_atoms
              WHERE liveness_kind IS NOT NULL
                AND liveness_kind != 'none'
                AND (liveness_last_ok IS NULL OR liveness_last_ok < ?)
              ORDER BY COALESCE(liveness_last_ok, 0) ASC"""
    params: tuple = (cutoff,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (cutoff, limit)
    return list(conn.execute(sql, params))
