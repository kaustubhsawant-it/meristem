"""Meristem ingester adapters.

Each adapter scans a workspace root and emits atoms into the store. The set
shipped here is deterministic + dependency-free (stdlib only) so a fresh
`meristem ingest` works without ML weights or extra tools downloaded.

Adapters
--------
readme    — README / CLAUDE / AGENTS docs at root → glossary atoms
manifest  — pyproject / package.json / pubspec / Cargo / requirements / go.mod
            → dependency + runtime + build_recipe atoms
schema    — *.sql files, CREATE TABLE → schema_fact atoms
invariants— declared schema constraints (NOT NULL/UNIQUE/CHECK/FK) → invariant
            atoms, each with a runnable regex predicate
git       — recent commits since last_indexed_sha → decision atoms (CLOSED)
symbols   — top-level fn/class regex per language → glossary atoms (capped)
modules   — top-level source dirs → module-tier atoms + ROLLS_UP edges (§14.1)
owners    — git history per directory → owner atoms + OWNS edges (§4 rule 6)

Each adapter is invoked via :func:`run`, which respects the enabled-list from
`meristem.toml` and a wall-clock budget.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from . import (
    git_commits,
    invariants,
    manifest,
    modules,
    owners,
    readme,
    schema_sql,
    symbols,
)
from .base import IngestContext, IngestResult

# Registry — string key matches `meristem.toml [ingesters].enabled`.
# Order matters at the tail: `modules` and `owners` both link to atoms the
# earlier adapters produce, so they run once those exist (SPEC §14.1, §4 r6).
_REGISTRY: dict[str, Callable[[IngestContext], IngestResult]] = {
    "readme": readme.run,
    "manifest": manifest.run,
    "schema": schema_sql.run,
    "invariants": invariants.run,
    "git": git_commits.run,
    "symbols": symbols.run,
    "modules": modules.run,
    "owners": owners.run,
}


def names() -> list[str]:
    """All registered ingester names, in canonical order."""
    return list(_REGISTRY.keys())


def run(
    ctx: IngestContext,
    enabled: list[str] | None = None,
    budget_seconds: float | None = None,
) -> dict[str, IngestResult]:
    """Run enabled ingesters against `ctx`, honoring budget.

    Returns a map ``{name: IngestResult}``. An unknown name in `enabled` is
    surfaced as a ``skipped`` result rather than crashing the run.
    """
    if enabled is None:
        enabled = list(_REGISTRY.keys())

    started = time.monotonic()
    results: dict[str, IngestResult] = {}
    for name in enabled:
        if budget_seconds is not None and (time.monotonic() - started) >= budget_seconds:
            results[name] = IngestResult(
                name=name,
                skipped="budget exhausted",
                truncated=True,
                # The next run gets a fresh budget, so this adapter genuinely
                # can catch up — hold the indexed sha until it does.
                budget_deferred=True,
            )
            continue
        fn = _REGISTRY.get(name)
        if fn is None:
            results[name] = IngestResult(name=name, skipped=f"unknown ingester: {name}")
            continue
        try:
            results[name] = fn(ctx)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash sibling adapters
            results[name] = IngestResult(name=name, error=f"{type(exc).__name__}: {exc}")
    return results


# Re-export the helpers most callers want.
__all__ = ["IngestContext", "IngestResult", "names", "run"]
