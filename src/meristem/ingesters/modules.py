"""Modules ingester — top-level package directories → module-tier atoms.

Implements the ingestion half of SPEC §14.1 (hierarchical atoms). For each
top-level source directory under the workspace root we emit one ``module``-tier
atom (``topic_key = module:<reldir>``) and connect any already-ingested child
atoms (those whose ``source_ref`` lives under that directory) to it with a
``ROLLS_UP`` edge. Retrieval can then start coarse at module atoms and let PPR
mass flow down the ROLLS_UP edges into the subsystem that actually matters.

Runs LAST in the registry so file/symbol atoms from the other adapters already
exist and can be rolled up. Stdlib-only, deterministic.
"""

from __future__ import annotations

from pathlib import Path

from ..atoms import assert_fact
from ..edges import upsert_edge
from .base import IngestContext, IngestResult

# Directories that are never a "module" even if they contain source.
# ".dlms" is the legacy (pre-rename) state dir — excluded too in case one is
# still sitting around from before a workspace was re-init'd as Meristem.
_NEVER_MODULE = {".git", ".meristem", ".dlms", ".github", ".vscode", "__pycache__"}

# Extensions we count as source when deciding whether a dir is a module.
_SOURCE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".dart", ".go", ".rs", ".rb",
    ".java", ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".sql",
}

# Top-level dirs per ingest run. Usually tiny (a handful of subsystems), but a
# workspace root pointed at a package-manager monorepo (`packages/` with
# hundreds of top-level packages) has no other bound — mirrors owners.py's
# MAX_DIRS, which already caps the same directory-derivation shape.
MAX_MODULES = 100


def run(ctx: IngestContext) -> IngestResult:
    res = IngestResult(name="modules")
    excl = set(ctx.exclude) | _NEVER_MODULE

    if ctx.changed_files is None:
        # Full scan: every top-level dir containing source.
        module_dirs = sorted(
            d for d in ctx.root.iterdir()
            if d.is_dir()
            and d.name not in excl
            and not d.name.startswith(".")
            and _has_source(d, excl)
        )
    else:
        # Incremental (SPEC §14.2): only top-level dirs that own a changed
        # source file — derived from the changed set, no full-tree rglob.
        tops: set[str] = set()
        for rel in ctx.changed_files:
            parts = Path(rel).parts
            if len(parts) < 2:
                continue  # a root-level file is not under a module dir
            top = parts[0]
            if top in excl or top.startswith(".") or top in tops:
                continue
            if Path(rel).suffix.lower() in _SOURCE_SUFFIXES:
                tops.add(top)
        module_dirs = sorted(
            d for d in (ctx.root / t for t in tops) if d.is_dir()
        )
    if not module_dirs:
        res.notes.append("no top-level source modules found")
        return res
    if len(module_dirs) > MAX_MODULES:
        res.notes.append(f"capped at {MAX_MODULES} of {len(module_dirs)} modules")
        res.truncated = True
        module_dirs = module_dirs[:MAX_MODULES]

    for d in module_dirs:
        # Workspace-relative, so it prefix-matches the `source_ref` values the
        # other adapters record. Under a multi-root workspace the bare basename
        # would collide between roots (two `core/` dirs → one module atom).
        rel = ctx.rel_path(d)
        atom = assert_fact(
            ctx.conn,
            type="glossary",
            topic_key=f"module:{rel}",
            tier="module",
            summary_10w=f"module {rel}",
            summary_50w=f"Source module `{rel}/` — a top-level subsystem of the repo.",
            source_kind="manual",
            source_ref=rel,
            repo_id=ctx.repo_id,
            workspace_id=ctx.workspace_id,
        )
        res.atoms_inserted += 1
        res.notes.append(
            f"{rel}/ → {_roll_up_children(ctx, module_id=atom.id, reldir=rel)} children"
        )
    return res


def _has_source(d: Path, excl: set[str]) -> bool:
    """True if `d` contains at least one source file (cheap, early-exit walk)."""
    for p in d.rglob("*"):
        if any(part in excl for part in p.parts):
            continue
        if p.is_file() and p.suffix.lower() in _SOURCE_SUFFIXES:
            return True
    return False


def _roll_up_children(ctx: IngestContext, *, module_id: str, reldir: str) -> int:
    """Link existing live child atoms under `reldir/` to the module via ROLLS_UP.

    Children are non-module atoms whose `source_ref` is inside the directory.
    The edge is directed child→parent; skip the module atom's own row.
    """
    rows = ctx.conn.execute(
        """SELECT id FROM live_atoms
            WHERE tier != 'module'
              AND id != ?
              AND source_ref IS NOT NULL
              AND (source_ref = ? OR source_ref LIKE ?)""",
        (module_id, reldir, f"{reldir}/%"),
    ).fetchall()
    linked = 0
    for r in rows:
        upsert_edge(
            ctx.conn,
            src_id=r["id"],
            dst_id=module_id,
            kind="ROLLS_UP",
            source="shared_ref",
            weight=0.6,
            evidence=[("module_dir", reldir)],
        )
        linked += 1
    return linked
