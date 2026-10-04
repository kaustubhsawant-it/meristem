"""Shared types for ingester adapters."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class IngestContext:
    """Per-run input handed to each ingester.

    `conn` is the open atom store. `root` is the workspace root to scan.
    `repo_id` and `workspace_id` flow through to atom rows for scoping.
    `last_indexed_sha` lets stateful adapters (git) read incrementally.
    `exclude` is the directory-name blocklist from meristem.toml.
    `max_file_kb` skips oversized files (per SPEC §scan).
    """

    conn: sqlite3.Connection
    root: Path
    repo_id: str
    workspace_id: str = "default"
    last_indexed_sha: str | None = None
    exclude: list[str] = field(default_factory=list)
    max_file_kb: int = 512
    # Incremental ingest (SPEC §14.2): repo-relative paths changed since the
    # last index. None = full scan (first run); a set = only these files.
    changed_files: set[str] | None = None
    # The workspace root (the directory holding meristem.toml). Paths *recorded on
    # atoms* are relative to this, not to `root` — see `rel_path`. None falls
    # back to `root`, which is correct for a single-root workspace.
    workspace_root: Path | None = None

    def is_changed(self, rel_path: str) -> bool:
        """True if `rel_path` should be (re)ingested this run.

        Full-scan runs (changed_files is None) accept everything; incremental
        runs accept only paths in the changed set. Note these are *ingest*-root
        relative (they come from `git diff` inside `root`), unlike `rel_path`.
        """
        return self.changed_files is None or rel_path in self.changed_files

    def rel_path(self, path: Path) -> str:
        """The path to record on an atom — relative to the WORKSPACE root
        when possible, else relative to this adapter's own ingest root.

        Tries the workspace root first: for a single-root workspace, or a
        registered root that is the workspace root itself or one of its own
        child subdirectories, `path.relative_to(base)` succeeds and that's
        what gets recorded — atom ids hash summary text that embeds this
        path, and two roots sharing an internal layout (`api/core` and
        `worker/core`) would otherwise mint identical topic_keys/summaries
        and collide into one id.

        For a *sibling* root (`roots = [".", "../other_service"]`), though,
        `path.relative_to(workspace_root)` always raises — `Path.relative_to`
        can never produce a leading `..` — so this silently falls through to
        `path.relative_to(self.root)` instead, recording the target relative
        to the sibling's own root, not the workspace root. That is NOT a bug
        in this method to "fix" by forcing a `../`-prefixed workspace-relative
        path here: atom ids hash summary text that embeds this string, so
        changing what a sibling-root atom records would re-mint its id,
        orphaning every edge pointing at the old one. `liveness.check_atom`
        (`_atom_bases`, added 2026-09-23) is what actually reconciles this:
        it resolves a sibling-root atom's target against that atom's own
        registered root first, matching what this method actually wrote for
        it, with the workspace root tried as a fallback.

        Single-root workspaces are unaffected either way: the workspace root
        *is* the ingest root, so this returns exactly what it always did.
        """
        base = self.workspace_root or self.root
        resolved = path.resolve()
        for candidate in (base, self.root):
            try:
                return str(resolved.relative_to(candidate.resolve()))
            except ValueError:
                continue
        # A root configured outside the workspace entirely — record the absolute
        # path rather than a misleading relative one that resolves elsewhere.
        return str(resolved)


@dataclass
class IngestResult:
    """Per-adapter outcome surfaced in the CLI table."""

    name: str
    atoms_inserted: int = 0
    atoms_skipped: int = 0
    notes: list[str] = field(default_factory=list)
    skipped: str | None = None
    error: str | None = None
    # True when the adapter did NOT process everything it should have — budget
    # ran out, or a designed per-adapter cap was reached. Reporting only.
    truncated: bool = False
    # True only when the work was deferred by something the NEXT run will have
    # more of: the time budget. This, not `truncated`, gates the indexed-sha
    # advance (§14.2).
    #
    # The two were one flag until 2026-08-06, and conflating them deadlocked
    # every non-trivial repo. A designed cap (symbols stops at 200 atoms) is hit
    # on every full scan by construction, so holding the sha back meant the sha
    # was NEVER recorded — which meant the next run had no base to diff against,
    # so it was another full scan, which hit the same cap. Measured on a 28-commit
    # repo: 316 atoms ingested, `last_indexed_sha` NULL forever, incremental
    # ingest permanently inactive, and `doctor` reporting "registered but never
    # ingested — the store is empty" over a populated store.
    budget_deferred: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.skipped is None

    def status_label(self) -> str:
        if self.error:
            return f"err: {self.error}"
        if self.skipped:
            return f"skip: {self.skipped}"
        return "ok"


def token_pattern(name: str) -> str:
    r"""Whole-token regex for `name`, safe for non-word-edged names.

    ``\b`` only asserts a boundary *between* a word and a non-word character,
    so a leading ``\b`` can never match a name that itself starts with a
    non-word character — npm scopes (``@supabase/supabase-js``) and JS
    symbols like ``$emit`` being the common cases, since the character
    before either in source is usually punctuation, not a word character.
    That made the predicate permanently unsatisfiable and the atom read as
    stale drift forever. Use a lookaround on any non-word edge instead.
    """
    if not name:
        return re.escape(name)
    esc = re.escape(name)
    left = r"\b" if (name[0].isalnum() or name[0] == "_") else r"(?<![\w@])"
    right = r"\b" if (name[-1].isalnum() or name[-1] == "_") else r"(?![\w-])"
    return f"{left}{esc}{right}"


def iter_files(
    root: Path,
    *,
    suffixes: tuple[str, ...] | None = None,
    names: tuple[str, ...] | None = None,
    exclude: list[str],
    max_kb: int,
    changed: set[str] | None = None,
) -> list[Path]:
    """Walk `root` collecting files by suffix or basename, honoring exclude dirs.

    `exclude` matches any path component literally (e.g., 'node_modules').
    Files larger than `max_kb` kilobytes are skipped to keep ingest cheap.
    When `changed` is given (incremental ingest, SPEC §14.2) we iterate exactly
    those repo-relative paths instead of walking the whole tree — keeping cost
    O(changed) rather than O(repo).
    """
    excl = set(exclude)
    cap = max_kb * 1024
    suffix_set = {s.lower() for s in suffixes} if suffixes else None
    name_set = set(names) if names else None
    candidates: Iterable[Path] = (
        (root / rel for rel in sorted(changed))
        if changed is not None
        else root.rglob("*")
    )
    out: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        if any(part in excl for part in path.parts):
            continue
        matches_suffix = suffix_set is not None and path.suffix.lower() in suffix_set
        matches_name = name_set is not None and path.name in name_set
        if suffix_set is None and name_set is None:
            pass  # accept everything
        elif not (matches_suffix or matches_name):
            continue
        try:
            if path.stat().st_size > cap:
                continue
        except OSError:
            continue
        out.append(path)
    return out
