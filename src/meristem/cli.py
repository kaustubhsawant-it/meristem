"""Meristem CLI entry point.

Subcommands shipped in Task #4:
  meristem init     — write meristem.toml, create .meristem/ + atoms.sqlite (schema applied)
  meristem ingest   — record HEAD SHA per workspace root (ingesters are stubs for now)
  meristem status   — show config path, db path, atom count, last-indexed SHA per repo

The ingesters listed in meristem.toml (`readme`, `manifest`, `schema`, `git`,
`symbols`) are implemented in later tasks. This scaffold establishes the
control surface so subsequent tasks can plug adapters into the `jobs` queue.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import (
    __version__,
    calibrate,
    candidates,
    config,
    discovery,
    doctor,
    embeddings,
    freshness,
    git_utils,
    ingesters,
    reaper,
    retrieval,
    store,
    sync_protocol,
)
from . import atoms as atoms_mod
from . import edges as edges_mod
from . import guard as guard_mod
from . import hooks as hooks_mod
from . import setup as setup_mod
from .ingesters.base import IngestContext
from .paths import Layout, detect_layout, layout_for, legacy_state_present

app = typer.Typer(
    name="meristem",
    no_args_is_help=True,
    add_completion=False,
    help="Meristem — a self-invalidating knowledge substrate for coding agents.",
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"meristem {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version",
        callback=_version_callback, is_eager=True,
        help="Print the Meristem CLI version and exit.",
    ),
) -> None:
    """Meristem — a self-invalidating knowledge substrate for coding agents.

    `--version` here is the conventional root-level flag every other CLI a
    user reaches for supports; the `version` subcommand below still works
    too and is what stays scriptable if `--version` ever needs to carry
    other eager root options in the future.
    """


def _repo_id(root: Path) -> str:
    """Stable repo_id derived from absolute path. 12 hex chars is plenty."""
    return hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:12]


def _resolve_root_path(layout: Layout, raw: str) -> Path:
    p = Path(raw)
    return (p if p.is_absolute() else layout.root / p).resolve()


def _require_store(layout: Layout) -> None:
    """Common guard for every command that reads an existing store.

    Distinguishes "never initialized" from "initialized under the old DLMS
    name" — the latter would otherwise read as a silently-empty fresh store
    (`meristem init` runs clean, finds nothing, and every query comes back
    empty with no indication a populated `.dlms/` sits right next to it).
    """
    if layout.db.exists():
        return
    if legacy_state_present(layout):
        console.print(
            "[red]✗[/red] found an old [bold].dlms/[/bold] directory here — "
            "this project was renamed from DLMS to Meristem. Run "
            "[bold]meristem init[/bold] to build a fresh [bold].meristem/[/bold] "
            "store; the old directory is left in place, delete it yourself "
            "once you've confirmed the new one works."
        )
    else:
        console.print("[red]✗[/red] no atoms.sqlite — run [bold]meristem init[/bold] first")
    raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing meristem.toml."),
    all: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="After init, also run ingest + embed (one-shot setup).",
    ),
) -> None:
    """Initialize Meristem in the current workspace.

    Writes `meristem.toml` (if missing), creates `.meristem/` with `atoms.sqlite`
    initialized from `schema.sql`, registers each configured root in
    `repo_state`, and registers the git merge driver for the shared
    `atoms.jsonl` export (SPEC §16 — safe and required to re-run on every
    clone, since the driver command is local git config).

    Use `--all` (or `-a`) to chain `ingest` + `embed` immediately after init —
    one command, fully set up.
    """
    layout = detect_layout()
    name = layout.root.name

    if legacy_state_present(layout):
        console.print(
            "[yellow]·[/yellow] found an old [bold].dlms/[/bold] directory here "
            "(this project was renamed from DLMS to Meristem) — building a fresh "
            "[bold].meristem/[/bold] store alongside it. Delete the old one "
            "yourself once you've confirmed this one works."
        )

    # 1. Write meristem.toml
    if layout.config.exists() and not force:
        console.print(f"[yellow]·[/yellow] {layout.config.name} exists (use --force to overwrite)")
    else:
        layout.config.write_text(config.render_default(name=name, roots=["."]), encoding="utf-8")
        action = "rewrote" if force else "created"
        console.print(f"[green]✓[/green] {action} {layout.config.relative_to(layout.root)}")

    # 2. Create .meristem/
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    layout.handoffs.mkdir(parents=True, exist_ok=True)
    console.print(f"[green]✓[/green] state dir {layout.state_dir.relative_to(layout.root)}/")

    # 3. Initialize sqlite
    cfg = config.load(layout.config)
    with store.connect(layout.db) as conn:
        version = store.init_schema(conn)
        for raw_root in cfg.workspace.roots:
            rp = _resolve_root_path(layout, raw_root)
            store.register_repo(conn, repo_id=_repo_id(rp), root_path=str(rp))
    console.print(
        f"[green]✓[/green] atoms.sqlite (schema v{version}, "
        f"{len(cfg.workspace.roots)} root(s) registered)"
    )

    # 4. Team sync protocol (SPEC §16) — register the merge driver for the
    # shared JSONL export. `.gitattributes` is committed and shared;
    # the driver *command* it names is local git config, so this step must
    # run again on every clone — safe to re-run, it just overwrites the same
    # two keys. Only meaningful for the implemented mode.
    if cfg.sync.mode in ("git-jsonl", "git-jsonl-sharded"):
        attr_target = (
            cfg.sync.export_path
            if cfg.sync.mode == "git-jsonl"
            else f"{cfg.sync.export_dir}/*.jsonl"
        )
        attr_outcome = _ensure_gitattributes(layout.root, attr_target)
        driver_outcome = _install_merge_driver(layout.root)
        if driver_outcome == "installed":
            console.print(
                f"[green]✓[/green] merge driver registered for {attr_target} "
                f"([dim].gitattributes {attr_outcome}[/dim])"
            )
        else:
            console.print(f"[yellow]·[/yellow] merge driver: {driver_outcome}")

    # 5. Team onboarding: a fresh store in a clone that carries the shared
    # export gets the team's memory now, before any local ingest.
    _import_shared_export_if_fresh(layout, cfg)

    if all:
        console.print()
        console.print("[bold]→ ingest[/bold]")
        # Every argument must be passed explicitly. This is a direct Python call,
        # not a Typer dispatch, so any parameter left out keeps its `OptionInfo`
        # sentinel as its value — and an OptionInfo is truthy, which silently
        # inverted `no_edges` and skipped the whole edge-discovery pass.
        ingest(budget=None, all_adapters=False, no_edges=False, chained=True)
        console.print()
        console.print("[bold]→ embed[/bold]")
        embed(limit=500)
        console.print()
        console.print("[green]✓[/green] workspace ready — try [bold]meristem status[/bold]")
    else:
        console.print()
        console.print(
            f"Next: [bold]meristem ingest[/bold]  (budget: {cfg.bootstrap.budget_seconds}s)"
        )
        console.print(
            "Or run [bold]meristem init --all[/bold] next time to chain init + ingest + embed."
        )


def _shared_export_texts(layout: Layout, cfg: config.Config) -> list[str]:
    """Texts of the shared export if it exists on disk, else []."""
    if cfg.sync.mode == "git-jsonl-sharded":
        in_dir = layout.root / cfg.sync.export_dir
        paths = sorted(in_dir.glob("*.jsonl")) if in_dir.exists() else []
        return [p.read_text(encoding="utf-8") for p in paths]
    if cfg.sync.mode == "git-jsonl":
        in_path = layout.root / cfg.sync.export_path
        return [in_path.read_text(encoding="utf-8")] if in_path.exists() else []
    return []


def _import_shared_export_if_fresh(layout: Layout, cfg: config.Config) -> int:
    """Import the team's shared export into an empty store. Returns the atom
    count imported (0 when there was nothing to do). A store that already has
    atoms is never touched here — that is what `meristem import` is for."""
    texts = _shared_export_texts(layout, cfg)
    if not texts:
        return 0
    with store.connect(layout.db) as conn:
        if store.atom_count(conn) > 0:
            return 0
        sync_protocol.import_jsonl_sharded(conn, texts)
        n = store.atom_count(conn)
    if n:
        console.print(f"[green]✓[/green] imported {n} atoms from the shared export")
        console.print("[dim]  run `meristem embed` (or `meristem sync`) to vectorize them[/dim]")
    return n


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

@app.command()
def ingest(
    budget: int | None = typer.Option(
        None, "--budget", help="Time budget in seconds (overrides meristem.toml)."
    ),
    all_adapters: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Run every registered adapter, ignoring the meristem.toml enabled list. "
        "Use once to backfill a workspace whose config predates a new adapter.",
    ),
    no_edges: bool = typer.Option(
        False, "--no-edges", help="Skip the post-ingest edge-discovery pass (SPEC §4)."
    ),
    chained: bool = typer.Option(
        False,
        "--chained",
        hidden=True,
        help="Internal: the caller embeds immediately after, so suppress the "
        "unembedded-atoms warning. Set by `init --all` and `sync`.",
    ),
) -> None:
    """Run ingestion across configured roots.

    Walks each configured root, runs every enabled adapter
    (readme/manifest/schema/git/symbols/modules), then derives typed edges over
    the resulting atoms (SPEC §4 auto-discovery). `last_indexed_sha` from a
    previous ingest is fed to stateful adapters so incremental runs are cheap.

    Edge discovery runs after the adapters because it connects atoms that only
    all exist once every adapter for that root has finished.
    """
    layout = detect_layout()
    if not layout.config.exists():
        console.print("[red]✗[/red] no meristem.toml — run [bold]meristem init[/bold] first")
        raise typer.Exit(code=2)

    cfg = config.load(layout.config)
    effective_budget = budget if budget is not None else cfg.bootstrap.budget_seconds
    enabled = ingesters.names() if all_adapters else (cfg.ingesters.enabled or [])

    started = time.monotonic()
    table = Table(
        title=f"ingest (budget {effective_budget}s, adapters: {', '.join(enabled) or 'none'})"
    )
    table.add_column("root", style="cyan")
    table.add_column("branch")
    table.add_column("head")
    for name in enabled:
        table.add_column(name, justify="right")
    table.add_column("total", justify="right", style="bold")
    if not no_edges:
        table.add_column("edges", justify="right", style="magenta")

    grand_total = 0
    grand_edges = 0
    grand_reaped = 0
    grand_skipped = 0
    edge_notes: list[str] = []
    reap_notes: list[str] = []
    ingest_notes: list[str] = []
    with store.connect(layout.db) as conn:
        for raw_root in cfg.workspace.roots:
            rp = _resolve_root_path(layout, raw_root)
            if not rp.exists():
                row = [str(rp), "-", "-"] + ["-"] * len(enabled) + ["[red]missing[/red]"]
                if not no_edges:
                    row.append("-")
                table.add_row(*row)
                continue
            rid = _repo_id(rp)
            store.register_repo(conn, repo_id=rid, root_path=str(rp))
            prior_sha = _prior_indexed_sha(conn, rid)
            sha = git_utils.head_sha(rp)
            branch = git_utils.current_branch(rp)
            # Incremental ingest (SPEC §14.2): None on first run → full scan;
            # otherwise only files changed since the last indexed sha.
            changed = git_utils.changed_files_since(rp, prior_sha)

            remaining = max(0.0, effective_budget - (time.monotonic() - started))
            ctx = IngestContext(
                conn=conn,
                root=rp,
                repo_id=rid,
                workspace_id="default",
                last_indexed_sha=prior_sha,
                exclude=cfg.scan.exclude,
                max_file_kb=cfg.scan.max_file_kb,
                changed_files=changed,
                # Atoms record paths relative to the workspace root because that
                # is what liveness resolves against (see `IngestContext.rel_path`).
                workspace_root=layout.root,
            )
            results = ingesters.run(ctx, enabled=enabled, budget_seconds=remaining)

            # Per-adapter `.notes`/`.atoms_skipped` (a swallowed TOMLDecodeError,
            # a git-log lookback fallback, a "first N of M" truncation) used to
            # exist only as IngestResult fields nobody read — only edge-discovery
            # and reap notes were ever printed, so a malformed config file just
            # showed a lower atom count with zero explanation. Surfaced here and
            # persisted below so `doctor` can report it after the fact too.
            rel = (
                str(rp.relative_to(layout.root))
                if rp.is_relative_to(layout.root) else str(rp)
            )
            root_skipped = sum(r.atoms_skipped for r in results.values() if r is not None)
            root_notes = [
                f"{rel}: {name}: {note}"
                for name, r in results.items() if r is not None for note in r.notes
            ]
            if root_skipped:
                root_notes.append(f"{rel}: {root_skipped} atom(s) skipped")
            ingest_notes.extend(root_notes)
            grand_skipped += root_skipped

            # Close atoms whose source file git says was deleted in this
            # interval (SPEC §14.5). Ingestion is additive and never revisits a
            # path that is gone, so without this an atom outlives its subject.
            # Runs after the adapters so a delete-then-recreate in the same
            # interval re-asserts first and is not left closed.
            reaped = reaper.reap(conn, rp, since_sha=prior_sha, repo_id=rid)
            grand_reaped += reaped.count

            # Hold the indexed sha back when an adapter was BUDGET-skipped, so
            # the next run re-derives the same changed set and retries the
            # skipped files rather than orphaning them (SPEC §14.2).
            #
            # A designed cap does not hold it back. That cap is hit on every full
            # scan by construction, so holding the sha meant it was never
            # recorded, so the next run had nothing to diff against and did
            # another full scan into the same cap — incremental ingest never
            # activated on any repo with more than 200 symbols. Advancing keeps
            # the mechanism alive; the coverage gap is reported below instead of
            # being paid for with a permanent full scan.
            held = any(r.budget_deferred for r in results.values() if r is not None)
            if not held:
                store.mark_indexed(
                    conn, repo_id=rid, sha=sha, branch=branch,
                    atoms_skipped=root_skipped, notes=root_notes,
                )
            row_total = 0
            cells: list[str] = []
            for name in enabled:
                r = results.get(name)
                if r is None or not r.ok:
                    label = r.status_label() if r else "n/a"
                    cells.append(f"[dim]{label}[/dim]")
                else:
                    cells.append(str(r.atoms_inserted))
                    row_total += r.atoms_inserted
            grand_total += row_total
            if reaped.count:
                reap_notes.append(
                    f"{rel}: closed {reaped.count} atom(s) whose source was deleted"
                )
            elif reaped.undetermined and prior_sha:
                # Not the same as "nothing was deleted". Saying so keeps a
                # reaper that never ran from reading like one that found nothing.
                reap_notes.append(
                    f"{rel}: could not determine deletions — no atoms were reaped"
                )
            # A designed cap no longer blocks the sha, so it must be said out
            # loud instead — an adapter that stopped early is a coverage gap,
            # and an unreported coverage gap reads as full coverage.
            if capped := [
                n for n, r in results.items()
                if r is not None and r.truncated and not r.budget_deferred
            ]:
                edge_notes.append(
                    f"{rel}: capped adapter(s) {', '.join(capped)} — "
                    "not every file was parsed this run"
                )

            # CO_CHANGED is mined from *this* root's git history, so it runs
            # per root. The path/symbol rules are workspace-wide and run once
            # after the loop, when every root's atoms exist. Advisory: a failure
            # here must not lose the atoms the adapters just committed.
            cells_extra: list[str] = []
            if not no_edges:
                try:
                    disc = discovery.discover_edges(
                        conn, root=rp, workspace_id="default", rules={"co_changed"}
                    )
                    grand_edges += disc.total
                    cells_extra.append(str(disc.total))
                    edge_notes.extend(f"{rel}: {n}" for n in disc.notes)
                except Exception as exc:  # noqa: BLE001 — atoms are already durable
                    cells_extra.append("[red]err[/red]")
                    edge_notes.append(f"{rel}: edge discovery failed — {exc}")

            table.add_row(
                rel, branch or "-", (sha[:7] if sha else "-"), *cells,
                str(row_total), *cells_extra,
            )

        # Workspace-wide rules. REFERENCES and MIRRORS span roots — a doc in one
        # root naming a file in another, or the same symbol mirrored across two
        # apps — so they can only be correct once every root has been ingested.
        if not no_edges:
            try:
                disc = discovery.discover_edges(
                    conn,
                    root=layout.root,
                    workspace_id="default",
                    rules={"references", "mirrors"},
                )
                grand_edges += disc.total
                edge_notes.extend(disc.notes)
            except Exception as exc:  # noqa: BLE001 — atoms are already durable
                edge_notes.append(f"workspace-wide edge discovery failed — {exc}")

    elapsed = time.monotonic() - started
    console.print(table)
    summary = f"[dim]done in {elapsed:.2f}s — {grand_total} atoms inserted"
    if not no_edges:
        summary += f", {grand_edges} edges discovered"
    if grand_reaped:
        summary += f", {grand_reaped} closed as deleted"
    if grand_skipped:
        summary += f", {grand_skipped} atoms skipped"
    console.print(summary + ".[/dim]")
    for note in ingest_notes:
        console.print(f"[dim]  ingest: {note}[/dim]")
    for note in edge_notes:
        console.print(f"[dim]  edges: {note}[/dim]")
    for note in reap_notes:
        console.print(f"[dim]  reap:  {note}[/dim]")

    # `ingest` does not embed — only `init --all` and `sync` do. An atom with
    # no vector cannot be a seed and cannot be returned, so a store left in
    # that state answers every query with silence while reporting a healthy
    # atom count. Saying so here costs one indexed COUNT and closes the gap
    # between "ingest succeeded" and "retrieval works".
    #
    # Suppressed when the caller embeds anyway: `init --all` chains ingest ->
    # embed, so warning in between describes a state already being fixed three
    # lines later. A check that fires on correct behaviour is one people learn
    # to scroll past.
    if chained:
        return
    try:
        with store.connect(layout.db) as conn:
            pending = embeddings.unembedded_count(conn)
    except sqlite3.Error:
        pending = 0
    if pending:
        console.print(
            f"[yellow]⚠[/yellow] [dim]{pending} atom(s) have no embedding yet — "
            "retrieval cannot return them. Run [bold]meristem embed[/bold].[/dim]"
        )


def _prior_indexed_sha(conn, repo_id: str) -> str | None:
    row = conn.execute(
        "SELECT last_indexed_sha FROM repo_state WHERE repo_id = ?",
        (repo_id,),
    ).fetchone()
    return row["last_indexed_sha"] if row else None


# ---------------------------------------------------------------------------
# export / import — team sync protocol (SPEC §16)
# ---------------------------------------------------------------------------

MERGE_DRIVER_NAME = "meristem-atoms"


def _install_merge_driver(root: Path) -> str:
    """Register the git merge driver for the sync export path.

    `.gitattributes` can name a driver but can't supply the command to run —
    that's inherently per-clone local config (a known git limitation), so
    every clone has to run this once. `init` does it automatically; it's
    idempotent (just overwrites the same two keys) so re-running is harmless.
    Returns 'installed' | 'not a git repo'.
    """
    if not git_utils.is_repo(root):
        return "not a git repo"
    git_utils.set_config(
        root, f"merge.{MERGE_DRIVER_NAME}.name", "Meristem atoms.jsonl merge (SPEC §16)"
    )
    git_utils.set_config(
        root, f"merge.{MERGE_DRIVER_NAME}.driver", "meristem merge-driver %O %A %B"
    )
    return "installed"


def _ensure_gitattributes(root: Path, export_rel: str) -> str:
    """Idempotently declare the merge driver for `export_rel` in
    `.gitattributes` (committed — every clone shares this line; only the
    driver *command* behind the name is per-clone, see `_install_merge_driver`).
    Returns 'added' | 'exists'.
    """
    path = root / ".gitattributes"
    line = f"{export_rel} merge={MERGE_DRIVER_NAME} -diff"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if line in text.splitlines():
        return "exists"
    if text and not text.endswith("\n"):
        text += "\n"
    text += line + "\n"
    path.write_text(text, encoding="utf-8")
    return "added"


def _write_managed_hook(hooks: Path, name: str, marker: str, body: str) -> str:
    """Write one git hook file. Returns 'installed' | 'updated' |
    'exists (not ours) — left alone'.

    Refuses to touch a hook this tool did not write. Silently clobbering a
    user's own hook to install a *convenience* would be a poor trade at any
    hit rate, so a foreign hook is reported and skipped — the CLI then prints
    the line to paste.
    """
    target = hooks / name
    if target.exists() and marker not in target.read_text(errors="replace", encoding="utf-8"):
        return "exists (not ours) — left alone"
    hooks.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    target.write_text(body, encoding="utf-8")
    target.chmod(0o755)
    return "updated" if existed else "installed"


# ---------------------------------------------------------------------------
# managed git hooks — one file per git event, listing every workspace
# ---------------------------------------------------------------------------
#
# A repo's hooks are shared by every workspace whose roots include it (a
# worktree checkout, a parent workspace, a sibling project), but a hook file is
# one file. The previous format baked in a single workspace, so the last
# workspace to run `--install-hook` silently evicted the rest — and once that
# workspace was deleted, the hook exited on every commit and nothing said so.
# Now each managed hook carries a *list* of `actions|workspace` entries; an
# install adds this workspace's actions and drops workspaces whose store is gone.

_HOOK_TEMPLATE = """#!/bin/sh
# {marker} (SPEC {spec}) — {purpose}
#
# Workspaces are listed after the `while` loop below, one `actions|path` per
# line; `meristem import --install-hook` / `meristem sync --install-hook` add
# to that list (they never replace it), and drop a workspace whose
# .meristem/atoms.sqlite no longer exists. At run time a missing workspace is
# skipped. Commands are detached and fully redirected: git must never wait on,
# or be failed by, indexing.
#
# Uninstall: delete this file.
#
# Git exports GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE / GIT_PREFIX into hooks,
# scoped to the operation that just happened. Meristem shells out to git in every
# configured root, and an inherited (possibly absolute) index path would make it
# read THIS repo's index while asking about another one. Drop them and let git discover.
unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX GIT_OBJECT_DIRECTORY
{guard}command -v meristem >/dev/null 2>&1 || exit 0
while IFS='|' read -r actions ws; do
  [ -n "$ws" ] || continue
  [ -f "$ws/.meristem/atoms.sqlite" ] || continue
  (
    cd "$ws" || exit 0
    case ",$actions," in *,import,*) meristem import --quiet ;; esac
    case ",$actions," in *,sync,*) meristem sync --quiet ;; esac
  ) </dev/null >/dev/null 2>&1 &
done {ws_open}
{entries}
{ws_close}
exit 0
"""

# event -> (marker, SPEC section, purpose, extra guard line). post-checkout only
# acts on a branch checkout: git passes $3=1 for one (including the checkout
# `git clone` fires) and $3=0 for a plain file checkout, which cannot have
# changed atoms.jsonl under you.
POST_MERGE_MARKER = "meristem-managed post-merge hook"
POST_CHECKOUT_MARKER = "meristem-managed post-checkout hook"
POST_COMMIT_MARKER = "meristem-managed post-commit hook"
POST_REWRITE_MARKER = "meristem-managed post-rewrite hook"

_HOOK_SPECS: dict[str, tuple[str, str, str, str]] = {
    "post-merge": (
        POST_MERGE_MARKER, "§16/§19",
        "after a merge/pull, pull in exported atoms and bring the index level.", "",
    ),
    "post-checkout": (
        POST_CHECKOUT_MARKER, "§16",
        "pull in a teammate's exported atoms after a branch switch.",
        '[ "$3" = "1" ] || exit 0\n',
    ),
    "post-commit": (
        POST_COMMIT_MARKER, "§19",
        "keep the memory index level with the repo.", "",
    ),
    "post-rewrite": (
        POST_REWRITE_MARKER, "§19",
        "an amend/rebase moves HEAD without a post-commit; re-sync.", "",
    ),
}


def _install_managed_hook(
    hooks: Path, event: str, workspace: Path, actions: set[str]
) -> str:
    """Add `workspace` (with `actions`) to the managed `event` hook, creating or
    upgrading it. Returns the same outcomes as `_write_managed_hook`.

    Other workspaces keep their entries and actions; this workspace's actions are
    unioned (so `import --install-hook` never drops a sync step, or vice versa);
    entries whose store no longer exists are pruned. A pre-list single-workspace
    hook is read by `git_utils.managed_hook_workspaces` and rewritten in place.
    """
    marker, spec, purpose, guard = _HOOK_SPECS[event]
    entries = git_utils.managed_hook_workspaces(hooks / event, marker) or {}
    ws = str(workspace)
    entries[ws] = entries.get(ws, set()) | actions
    live = {
        w: a for w, a in entries.items()
        if w == ws or (Path(w) / ".meristem" / "atoms.sqlite").exists()
    }
    body = _HOOK_TEMPLATE.format(
        marker=marker, spec=spec, purpose=purpose, guard=guard,
        ws_open=git_utils.HOOK_WS_OPEN, ws_close=git_utils.HOOK_WS_CLOSE,
        entries="\n".join(f"{','.join(sorted(a))}|{w}" for w, a in live.items()),
    )
    return _write_managed_hook(hooks, event, marker, body)


def _install_hooks(
    layout: Layout, roots: list[Path], plan: dict[str, set[str]]
) -> list[tuple[Path, str]]:
    """Install `plan` (event -> actions) into each git root. Returns
    (path, outcome) pairs, one per event per root."""
    results: list[tuple[Path, str]] = []
    for rp in roots:
        hooks = git_utils.hooks_dir(rp)
        if hooks is None:
            results.append((rp, "not a git repo"))
            continue
        for event, actions in plan.items():
            results.append((
                hooks / event, _install_managed_hook(hooks, event, layout.root, actions)
            ))
    return results


def _install_import_hooks(layout: Layout, roots: list[Path]) -> list[tuple[Path, str]]:
    """Write post-merge and post-checkout hooks into each git root that run
    `meristem import`. Returns (path, outcome) pairs, two per root.

    Two hooks, not one: `git pull` fires post-merge but never post-checkout,
    while switching branches (including the checkout `git clone` performs)
    fires post-checkout but never post-merge — either can be the first place
    a teammate's exported atoms.jsonl becomes visible in the working tree.
    """
    return _install_hooks(layout, roots, {"post-merge": {"import"}, "post-checkout": {"import"}})


def _report_withheld(withheld: list[tuple[str, str]], quiet: bool) -> None:
    """Name what the trust guard kept out of the export (ids and kinds, never values)."""
    if not withheld or quiet:
        return
    atoms = {a for a, _ in withheld}
    console.print(
        f"[yellow]! withheld {len(atoms)} atom(s) containing secrets or "
        "personal data from the export[/yellow]"
    )
    for atom_id, kind in withheld[:10]:
        console.print(f"  {atom_id}: {kind}", markup=False, highlight=False)
    if len(withheld) > 10:
        console.print(f"  … and {len(withheld) - 10} more", markup=False, highlight=False)
    console.print(
        "fix: archive/re-assert without the value, or add [guard] allow_patterns; "
        "see meristem doctor (guard.store)", markup=False, highlight=False,
    )


@app.command(name="export")
def export_cmd(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress the summary line."),
) -> None:
    """Export the shared atom store to a git-mergeable JSONL file (SPEC §16).

    Writes `[sync] export_path` (default `.meristem/atoms.jsonl`, or — for
    `mode = "git-jsonl-sharded"` — one file per shard under `export_dir`)
    from the live local `atoms.sqlite`. Only the shared substrate leaves the
    machine — atoms, atom_summaries, edges, edge_evidence. Per-machine
    operational state (jobs, retrieval_log, session_state, embedding_ledger,
    candidates, repo_state — the last of which is keyed by an absolute local
    path and could never agree across two clones) never enters the export.
    """
    layout = detect_layout()
    _require_store(layout)
    cfg = config.load(layout.config)
    policy = guard_mod.policy_from_config(cfg)
    withheld: list[tuple[str, str]] = []

    if cfg.sync.mode == "git-jsonl-sharded":
        out_dir = layout.root / cfg.sync.export_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        with store.connect(layout.db) as conn:
            shards = sync_protocol.export_jsonl_sharded(
                conn, cfg.sync.shard_prefix_len, policy=policy, withheld=withheld,
            )
        # Only rewrite a shard whose content actually changed — an unchanged
        # shard file is what keeps this mode's git diff/merge cost O(touched
        # atoms), not O(store), which is the entire point of sharding.
        changed = 0
        for shard, text in shards.items():
            path = out_dir / f"{shard}.jsonl"
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
                changed += 1
        n = sum(sum(1 for line in text.splitlines() if line.strip()) for text in shards.values())
        if not quiet:
            console.print(
                f"[green]✓[/green] exported {n} row(s) across {len(shards)} shard(s) "
                f"({changed} changed) to {cfg.sync.export_dir}/"
            )
        _report_withheld(withheld, quiet)
        return

    if cfg.sync.mode != "git-jsonl":
        if not quiet:
            console.print(
                f"[dim]· [sync] mode = {cfg.sync.mode!r} — "
                "skipping (only \"git-jsonl\"/\"git-jsonl-sharded\" are implemented)[/dim]"
            )
        return
    out_path = layout.root / cfg.sync.export_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with store.connect(layout.db) as conn:
        text = sync_protocol.export_jsonl(conn, policy=policy, withheld=withheld)
    out_path.write_text(text, encoding="utf-8")
    n = sum(1 for line in text.splitlines() if line.strip())
    if not quiet:
        console.print(f"[green]✓[/green] exported {n} row(s) to {cfg.sync.export_path}")
    _report_withheld(withheld, quiet)


@app.command(name="import")
def import_cmd(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="JSON output."),
    install_hook: bool = typer.Option(
        False,
        "--install-hook",
        help="Install post-merge/post-checkout git hooks that run this after a pull.",
    ),
) -> None:
    """Merge the shared JSONL export into the local atom store (SPEC §16).

    A union, never destructive: an atom present locally but not yet exported
    by whoever wrote it stays untouched. A row present on both sides is
    reconciled field-by-field (sync_protocol.py); a genuine same-topic
    disagreement between two devs surfaces as a CONTRADICTS edge rather than
    being silently decided by import order.
    """
    layout = detect_layout()
    _require_store(layout)

    if install_hook:
        cfg = config.load(layout.config)
        roots = [_resolve_root_path(layout, r) for r in cfg.workspace.roots]
        for path, outcome in _install_import_hooks(layout, roots):
            done = outcome in ("installed", "updated")
            mark = "[green]✓[/green]" if done else "[yellow]·[/yellow]"
            console.print(f"{mark} {path}  [dim]{outcome}[/dim]")
            if outcome.startswith("exists"):
                console.print(
                    "[dim]  add this line to it by hand:  "
                    f"( cd \"{layout.root}\" && meristem import --quiet >/dev/null 2>&1 & )[/dim]"
                )
        return

    cfg = config.load(layout.config)

    if cfg.sync.mode == "git-jsonl-sharded":
        in_dir = layout.root / cfg.sync.export_dir
        shard_paths = sorted(in_dir.glob("*.jsonl")) if in_dir.exists() else []
        if not shard_paths:
            if quiet:
                typer.echo(json.dumps({"imported": False, "reason": "no export shards"}))
            else:
                console.print(
                    f"[yellow]·[/yellow] nothing to import — {cfg.sync.export_dir}/ "
                    "has no shard files"
                )
            return
        texts = [p.read_text(encoding="utf-8") for p in shard_paths]
        with store.connect(layout.db) as conn:
            stats = sync_protocol.import_jsonl_sharded(conn, texts)
        _report_import_stats(stats, quiet)
        return

    if cfg.sync.mode != "git-jsonl":
        if quiet:
            typer.echo(json.dumps({"imported": False, "reason": f"sync mode {cfg.sync.mode!r}"}))
        else:
            console.print(
                f"[dim]· [sync] mode = {cfg.sync.mode!r} — "
                "skipping (only \"git-jsonl\"/\"git-jsonl-sharded\" are implemented)[/dim]"
            )
        return
    in_path = layout.root / cfg.sync.export_path
    if not in_path.exists():
        if quiet:
            typer.echo(json.dumps({"imported": False, "reason": "no export file"}))
        else:
            console.print(
                f"[yellow]·[/yellow] nothing to import — {cfg.sync.export_path} does not exist"
            )
        return
    text = in_path.read_text(encoding="utf-8")
    with store.connect(layout.db) as conn:
        stats = sync_protocol.import_jsonl(conn, text)
    _report_import_stats(stats, quiet)


def _report_import_stats(stats: sync_protocol.ImportStats, quiet: bool) -> None:
    if quiet:
        typer.echo(json.dumps({
            "imported": True,
            "inserted": stats.inserted,
            "updated": stats.updated,
            "unchanged": stats.unchanged,
            "conflicts": [{"topic_key": t, "kept": k} for t, k in stats.conflicts],
        }))
        return
    console.print(
        f"[green]✓[/green] imported — {stats.inserted} new, {stats.updated} updated, "
        f"{stats.unchanged} unchanged"
    )
    for topic, kept in stats.conflicts:
        console.print(
            f"[yellow]⚠[/yellow] conflict on topic [bold]{topic}[/bold] — "
            f"kept [bold]{kept}[/bold]; see [bold]meristem why {kept}[/bold]"
        )


@app.command(name="merge-driver", hidden=True)
def merge_driver_cmd(
    base: str = typer.Argument(
        ..., help="%O — common ancestor (unused; the per-row policy is base-independent)."
    ),
    ours: str = typer.Argument(
        ..., help="%A — our version. Overwritten in place with the merge result."
    ),
    theirs: str = typer.Argument(..., help="%B — their version."),
) -> None:
    """Git merge driver for the sync export (SPEC §16) — not for direct use.

    Registered via `meristem init` (`git config merge.meristem-atoms.driver`)
    and invoked by git itself on a conflicting `atoms.jsonl` during
    merge/pull/rebase. Always resolves cleanly: every column has a
    deterministic, order-independent policy (sync_protocol.merge_jsonl_texts),
    so this never leaves `<<<<<<<` markers or a nonzero exit for git to
    report as unresolved.
    """
    del base  # part of git's merge-driver contract (%O %A %B); policy doesn't need it
    ours_path = Path(ours)
    theirs_path = Path(theirs)
    merged = sync_protocol.merge_jsonl_texts(
        ours_path.read_text(encoding="utf-8"), theirs_path.read_text(encoding="utf-8"),
    )
    ours_path.write_text(merged, encoding="utf-8")


# ---------------------------------------------------------------------------
# sync — keep the index level with the repo (SPEC §19)
# ---------------------------------------------------------------------------

def _install_post_commit(layout: Layout, roots: list[Path]) -> list[tuple[Path, str]]:
    """Install the hooks that keep the index level with HEAD, into each git root.

    HEAD moves in more ways than `git commit`. The worktree flow is commit on a
    branch, then a `git merge --no-ff` into main — that fires post-merge, not
    post-commit; `git pull` likewise; amend/rebase fire post-rewrite. All four
    would otherwise leave the index behind until someone remembers to sync.
    (`meristem sync` is incremental and debounced, so a burst runs one ingest.)
    """
    return _install_hooks(
        layout, roots,
        {"post-commit": {"sync"}, "post-rewrite": {"sync"}, "post-merge": {"sync"}},
    )


@app.command()
def sync(
    check: bool = typer.Option(
        False, "--check", help="Report drift and change nothing. Exit 1 if behind."
    ),
    force: bool = typer.Option(False, "--force", help="Ignore the debounce window."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="JSON output, for hooks."),
    debounce: int = typer.Option(
        None, "--debounce", help="Minimum seconds between syncs (default 90)."
    ),
    no_embed: bool = typer.Option(
        False, "--no-embed", help="Skip embedding after ingest (default: embed)."
    ),
    install_hook: bool = typer.Option(
        False, "--install-hook", help="Install post-commit/rewrite/merge git hooks that run this."
    ),
) -> None:
    """Bring the index level with the repo, if it has fallen behind.

    Liveness can only ever *shrink* the store; nothing re-adds what the repo
    grew since the last manual `meristem ingest`. So a store drifts silently and
    answers with the same confidence at 56 commits behind as at 0 — measured on
    a live workspace, which is why this exists.

    Cheap when there is nothing to do: one `git rev-list --count` per root, and
    it returns before loading an embedder. Only a drifted root pays for ingest,
    and that ingest is already O(diff) (SPEC §14.2).
    """
    layout = detect_layout()
    _require_store(layout)

    if install_hook:
        cfg = config.load(layout.config)
        roots = [_resolve_root_path(layout, r) for r in cfg.workspace.roots]
        for path, outcome in _install_post_commit(layout, roots):
            done = outcome in ("installed", "updated")
            mark = "[green]✓[/green]" if done else "[yellow]·[/yellow]"
            console.print(f"{mark} {path}  [dim]{outcome}[/dim]")
            if outcome.startswith("exists"):
                console.print(
                    "[dim]  add this line to it by hand:  "
                    f"( cd \"{layout.root}\" && meristem sync --quiet >/dev/null 2>&1 & )[/dim]"
                )
        return

    with store.connect(layout.db) as conn:
        drifts = freshness.workspace_drift(conn)
        atoms_before = store.atom_count(conn)

    def _report(synced: bool, reason: str, after: list | None = None) -> None:
        end = after if after is not None else drifts
        payload = {
            "synced": synced,
            "reason": reason,
            "behind": freshness.total_behind(end),
            "dirty": sum(d.dirty for d in end),
            "roots": [
                {
                    "repo_id": d.repo_id,
                    "root": str(d.root),
                    "behind": d.commits_behind,
                    "dirty": d.dirty,
                    "head": d.head_sha,
                    "indexed": d.last_indexed_sha,
                }
                for d in end
            ],
        }
        if quiet:
            typer.echo(json.dumps(payload))
            return
        glyph = "[green]✓[/green]" if not payload["behind"] else "[yellow]⚠[/yellow]"
        console.print(f"{glyph} {reason}")
        for d in end:
            if d.drifted or d.dirty:
                console.print(
                    f"  [cyan]{d.root}[/cyan]  [dim]{d.describe()}"
                    f"{f', {d.dirty} uncommitted file(s)' if d.dirty else ''}[/dim]"
                )

    stale = [d for d in drifts if d.drifted]
    if check:
        _report(False, freshness.summarize(drifts) or "index is current with HEAD")
        raise typer.Exit(code=1 if stale else 0)

    if not stale and not force:
        _report(False, "index is current with HEAD")
        return

    cfg = config.load(layout.config)
    window = debounce if debounce is not None else cfg.freshness.debounce_seconds
    if not force and (left := freshness.debounce_remaining(layout, window=window)):
        _report(False, f"debounced — synced under {window}s ago, {left}s left")
        return

    lock = freshness.SyncLock(layout)
    if not lock.acquire():
        _report(False, "another sync is already running")
        return
    try:
        # `ingest` and `embed` are Typer commands but plain functions underneath;
        # every parameter must be passed explicitly or it keeps its OptionInfo
        # sentinel (see the note in `init --all`). Rich's `quiet` suppresses the
        # ingest table so a hook run stays a single JSON line on stdout.
        was_quiet = console.quiet
        console.quiet = quiet
        try:
            ingest(budget=None, all_adapters=False, no_edges=False, chained=True)
            if cfg.freshness.embed_after_sync and not no_embed:
                # Without this an ingested atom has no vector, and an atom with
                # no vector fails the relevance gate as `unknown` (§14.6) — it
                # would be stored, live, and unretrievable.
                embed(limit=500)
        finally:
            console.quiet = was_quiet
        with store.connect(layout.db) as conn:
            after = freshness.workspace_drift(conn)
            atoms_after = store.atom_count(conn)
        freshness.write_sync_state(layout, after)
    finally:
        lock.release()

    gained = atoms_after - atoms_before
    # Describe the state the sync actually reached, never the one it aimed at.
    # `ingest` deliberately withholds `mark_indexed` when an adapter was
    # budget-skipped or hit an internal cap, so a sync can complete and leave the
    # root still behind — and a run that reports "index now current" while 25
    # commits behind is the confident-wrong answer this whole surface exists to
    # replace.
    remaining = freshness.summarize(after)
    _report(
        True,
        f"synced — {gained:+d} atoms; " + (remaining or "index now current with HEAD"),
        after=after,
    )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _compact_root(path: str) -> str:
    """A root path short enough for the repos table, keeping the END.

    The tail is what identifies a repo ("…/Projects/meristem"); Rich elides the
    tail by default, so every row rendered as "/home/al…" — identical across
    roots and useless for telling them apart, which is the one thing this
    column exists to do. Home-relative first, then last-two-components.
    """
    p = Path(path)
    try:
        short = "~/" + p.relative_to(Path.home()).as_posix()
    except ValueError:
        short = str(p)
    if len(short) <= 30:
        return short
    parts = p.parts
    return "…/" + "/".join(parts[-2:]) if len(parts) > 2 else short


@app.command()
def status() -> None:
    """Show Meristem state: config path, db path, schema version, atom count, repos."""
    layout = detect_layout()
    if not layout.config.exists() and not layout.db.exists():
        console.print(
            f"[yellow]·[/yellow] Meristem not initialized in {layout.root}. "
            f"Run [bold]meristem init[/bold]."
        )
        raise typer.Exit(code=1)

    console.print(f"[bold]workspace[/bold]  {layout.root}")
    console.print(f"[bold]config[/bold]     {layout.config}"
                  f"{'  [dim](missing)[/dim]' if not layout.config.exists() else ''}")
    console.print(f"[bold]db[/bold]         {layout.db}"
                  f"{'  [dim](missing)[/dim]' if not layout.db.exists() else ''}")

    if not layout.db.exists():
        raise typer.Exit(code=1)

    with store.connect(layout.db) as conn:
        version = store.schema_version(conn)
        n = store.atom_count(conn)
        console.print(f"[bold]schema[/bold]     v{version}")
        console.print(f"[bold]atoms[/bold]      {n} live")
        rows = store.repo_rows(conn)
        drifts = {d.repo_id: d for d in freshness.workspace_drift(conn)}

    if not rows:
        console.print(
            "[dim]no repos registered yet — run `meristem init` or `meristem ingest`[/dim]"
        )
        return

    table = Table(title=f"repos ({len(rows)})")
    # `repo_id` is not shown. It is an opaque 12-char hash that a human never
    # needs and never types, and as a sixth column it was the difference
    # between a readable table and one whose own headers truncated to stubs
    # ("repo… bran… index…") at ordinary terminal widths. Widths are left to
    # Rich deliberately: pinning them made the table exceed the terminal and
    # crop `drift` — the one column the table exists for — off the right edge.
    table.add_column("root", style="cyan", no_wrap=True)
    table.add_column("branch")
    table.add_column("sha")
    table.add_column("indexed")
    # The one column that answers "is what you are about to be told current?".
    # Time-since-index cannot: a root indexed 40 days ago with no commits since
    # is perfectly current, and one indexed this morning can already be 56
    # commits stale.
    table.add_column("drift", justify="right")
    for r in rows:
        ts = r["last_indexed_at"]
        when = (
            dt.datetime.fromtimestamp(ts, tz=dt.UTC).strftime("%Y-%m-%d %H:%MZ")
            if ts else "-"
        )
        d = drifts.get(r["repo_id"])
        if d is None or not d.is_git:
            drift_cell = "[dim]-[/dim]"
        elif d.never_indexed or d.unknown_base:
            drift_cell = "[red]?[/red]"
        elif d.indexed_without_head:
            # Not "current": the baseline it is current against is nothing.
            drift_cell = "[red]re-ingest[/red]"
        elif d.behind:
            drift_cell = f"[yellow]{d.behind} behind[/yellow]"
        else:
            drift_cell = "[green]current[/green]"
        if d is not None and d.dirty:
            drift_cell += f" [dim]+{d.dirty} dirty[/dim]"
        table.add_row(
            _compact_root(r["root_path"]),
            r["last_branch"] or "-",
            (r["last_indexed_sha"][:7] if r["last_indexed_sha"] else "-"),
            when,
            drift_cell,
        )
    console.print(table)
    # Full path + full diagnosis for anything that is not clean, below the
    # table where neither is fighting for column width. `describe()` is the
    # part worth reading and it cannot fit in a cell.
    for r in rows:
        d = drifts.get(r["repo_id"])
        if d is not None and d.drifted:
            console.print(f"  [cyan]{r['root_path']}[/cyan]  [dim]{d.describe()}[/dim]")
    if any(d.indexed_without_head for d in drifts.values()):
        console.print(
            "[yellow]⚠[/yellow] [dim]run `meristem ingest` — these were indexed "
            "before their first commit[/dim]"
        )
    elif any(d.drifted for d in drifts.values()):
        console.print("[yellow]⚠[/yellow] [dim]run `meristem sync` to catch the index up[/dim]")


# ---------------------------------------------------------------------------
# embed
# ---------------------------------------------------------------------------

@app.command()
def embed(
    limit: int = typer.Option(500, "--limit", help="Max atoms to embed this run."),
) -> None:
    """Embed live atoms whose summary changed since last run.

    Uses the deterministic hash backend unless the ``meristem[embed]`` extra is
    installed (then sentence-transformers + bge-small-en-v1.5).
    """
    layout = detect_layout()
    _require_store(layout)
    embedder = embeddings.default_embedder()
    with store.connect(layout.db) as conn:
        n = embeddings.embed_pending(conn, embedder=embedder, limit=limit)
    console.print(f"[green]✓[/green] embedded {n} atom(s) with [bold]{embedder.name}[/bold]")


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

@app.command()
def decide(
    claim: str = typer.Argument(..., help="The constraint, in one sentence."),
    topic: str = typer.Option(
        ..., "--topic", "-t", help="Stable topic key, e.g. 'verified-badge'."
    ),
    status: str = typer.Option(
        "OPEN", "--status", "-s", help="OPEN | CLOSED | DEFERRED."
    ),
    because: str | None = typer.Option(
        None, "--because", "-b", help="Why. Recorded as the 250-word summary."
    ),
    # noqa: B008 is the standard Typer idiom — the Option object *is* the
    # parameter spec, so it has to be constructed in the default.
    blocks: list[str] | None = typer.Option(  # noqa: B008
        None, "--blocks", help="Repo-relative path prefix this decision constrains. Repeatable."
    ),
    liveness_kind: str = typer.Option(
        "regex", "--liveness-kind",
        help="regex (default, needs --liveness-target) | sql (self-referential — "
        "checks Meristem's own atoms store, no --liveness-target).",
    ),
    liveness_target: str | None = typer.Option(
        None, "--liveness-target", help="File whose content should still match --liveness-pattern."
    ),
    liveness_pattern: str | None = typer.Option(
        None, "--liveness-pattern",
        help="regex kind: a pattern proving the decision still holds in the code. "
        "sql kind: a SELECT proving it still holds in the atoms store.",
    ),
    pin: bool = typer.Option(False, "--pin", help="Protect from consolidation."),
) -> None:
    """Record a decision that constrains future work.

    Until this existed, the only producer of `decision` atoms was the git
    ingester — so Meristem could represent a decision git had already closed, but
    not one being made right now, which is exactly when the reasoning is
    cheapest and most accurate to capture. OPEN and DEFERRED were unreachable
    states in practice.

    The atom is classed `constraint`, so it surfaces in the SessionStart digest
    and constraint retrieval rather than being buried under commit records.

        meristem decide "No verified badge — legal liability; is_verified stays false" \\
            --topic verified-badge --status CLOSED \\
            --because "Counsel advised the claim creates liability we can't back." \\
            --blocks src/users --liveness-target src/users/model.py \\
            --liveness-pattern "is_verified.*=.*False"

        meristem decide "Every invariant ships with an owner" --topic invariant-owners \\
            --liveness-kind sql \\
            --liveness-pattern "SELECT 1 FROM live_atoms WHERE type='owner' LIMIT 1"
    """
    status = status.upper()
    if status not in ("OPEN", "CLOSED", "DEFERRED"):
        console.print(f"[red]✗[/red] --status must be OPEN, CLOSED or DEFERRED (got {status!r})")
        raise typer.Exit(code=2)
    if liveness_kind not in ("regex", "sql"):
        console.print(f"[red]✗[/red] --liveness-kind must be regex or sql (got {liveness_kind!r})")
        raise typer.Exit(code=2)
    if liveness_kind == "regex" and bool(liveness_target) != bool(liveness_pattern):
        console.print("[red]✗[/red] --liveness-target and --liveness-pattern must be used together")
        raise typer.Exit(code=2)
    if liveness_kind == "sql" and liveness_target:
        console.print("[red]✗[/red] --liveness-target is not used with --liveness-kind sql "
                       "(sql checks the atoms store itself, not a file)")
        raise typer.Exit(code=2)

    layout = detect_layout()
    _require_store(layout)

    if liveness_kind == "sql" and liveness_pattern:
        live: atoms_mod.Liveness | None = atoms_mod.Liveness(kind="sql", pattern=liveness_pattern)
    elif liveness_target:
        live = atoms_mod.Liveness(kind="regex", target=liveness_target, pattern=liveness_pattern)
    else:
        live = None
    with store.connect(layout.db) as conn:
        atom = atoms_mod.assert_fact(
            conn,
            type="decision",
            decision_status=status,  # type: ignore[arg-type]
            decision_class="constraint",
            topic_key=topic,
            summary_10w=claim[:80],
            summary_50w=claim,
            summary_250w=(f"{claim}\n\nRationale: {because}" if because else claim),
            source_kind="manual",
            liveness=live,
            pinned=pin,
        )
        console.print(
            f"[green]✓[/green] {status} constraint [bold]{topic}[/bold] "
            f"[dim]({atom.id[:12]})[/dim]"
        )
        if atom.conflicts:
            console.print(
                f"[yellow]⚠[/yellow] supersedes {len(atom.conflicts)} divergent "
                f"prior claim(s) on this topic — CONTRADICTS edge raised"
            )
        # BLOCKS edges make the constraint reachable by graph traversal from the
        # code it governs, so PPR surfaces it when work approaches those paths.
        linked = edges_mod.link_blocks(conn, atom.id, blocks or [])
        if blocks:
            console.print(f"[dim]  BLOCKS → {linked} atom(s) under {', '.join(blocks)}[/dim]")
            if not linked:
                console.print(
                    "[dim]  (no atoms matched those paths yet — run `meristem ingest`, "
                    "then re-run to link)[/dim]"
                )
    if not live:
        console.print(
            "[dim]  no liveness predicate — this constraint cannot self-invalidate. "
            "Consider --liveness-target/--liveness-pattern.[/dim]"
        )


@app.command()
def query(
    text: str = typer.Argument(..., help="Natural-language query."),
    files: list[str] = typer.Option(  # noqa: B008
        None, "--file", "-f", help="File context for structural seeding."
    ),
    top_n: int = typer.Option(10, "--top", help="Number of atoms to return."),
    min_relevance: float = typer.Option(
        None, "--min-relevance",
        # No square brackets: rich parses them as markup and silently eats the
        # section name, which is the one part of this sentence that locates it.
        help="Relevance floor. Defaults to retrieval.min_relevance in meristem.toml.",
    ),
    gate: bool = typer.Option(
        False, "--gate",
        help="Drop below-floor atoms instead of showing them marked.",
    ),
    scaffolding: bool = typer.Option(
        False, "--scaffolding",
        help="Include module-tier atoms (retrieval machinery, hidden by default).",
    ),
) -> None:
    """Retrieve relevant atoms via PPR over the typed edge graph.

    This surface used to rank by `score` alone, which SPEC §14.6 measured as
    carrying no relevance information at all — deliberate nonsense outscored a
    genuine query on a real store. `rel` is the number that separates them, so
    it leads the table; `score` is kept because it explains the ordering.

    A human typed this query, so nothing is silently dropped: below-floor atoms
    are shown dimmed under a divider rather than hidden, and `--gate` opts into
    the same suppression the prompt-injection path applies. That asymmetry is
    deliberate — `route()` speaks unbidden into a prompt, this does not.
    """
    layout = detect_layout()
    _require_store(layout)
    floor = (
        min_relevance
        if min_relevance is not None
        else config.load(layout.config).retrieval.min_relevance
    )
    with store.connect(layout.db) as conn:
        results = retrieval.retrieve(
            conn, query=text, file_context=files or None, top_n=top_n,
            repo_root=layout.root,
        )
        views = {}
        for r in results:
            if (view := atoms_mod.get_atom(conn, r.atom_id)) is not None:
                views[r.atom_id] = view
        verdict = retrieval.gate(
            (r for r in results if r.atom_id in views),
            min_relevance=floor,
            topic_of=lambda aid: views[aid].atom.topic_key,
            drop_scaffolding=not scaffolding,
        )
        shown = verdict.kept if gate else [*verdict.kept, *verdict.suppressed]
        if not shown:
            _query_empty_note(verdict, gate=gate)
            return

        table = Table(title=f"top {len(shown)} for: {text!r}")
        table.add_column("rel", justify="right")
        table.add_column("score", justify="right")
        table.add_column("live")
        table.add_column("type")
        table.add_column("topic_key")
        table.add_column("trail")
        table.add_column("summary")
        # An atom whose predicate could not run must not read like one that
        # passed — that confusion is the exact failure Meristem exists to prevent.
        live_glyph = {
            "verified": "[green]✓[/green]",
            "trusted": "[green]✓[/green]",
            "none": "[dim]–[/dim]",
            "unverifiable": "[yellow]?[/yellow]",
        }
        unverified = 0
        for r in shown:
            view = views[r.atom_id]
            if r.unverified:
                unverified += 1
            below = r.relevance < floor
            # Unknown relevance is not low relevance — it means no comparable
            # vector under the querying model, and must not read as a score.
            if not r.relevance_known:
                rel = "[yellow]?[/yellow]"
            else:
                rel = f"{r.relevance:.3f}"
                if below:
                    rel = f"[dim]{rel}[/dim]"
            cells = [
                rel,
                f"{r.score:.3f}",
                live_glyph.get(r.liveness_state, "[dim]–[/dim]"),
                view.atom.type,
                view.atom.topic_key,
                r.trail(),
                view.summaries.get(50, "")[:60],
            ]
            table.add_row(*(f"[dim]{c}[/dim]" if below and i else c
                            for i, c in enumerate(cells)))
    console.print(table)

    kept_n, sup_n = len(verdict.kept), verdict.suppressed_count
    if sup_n and not gate:
        console.print(
            f"[dim]{sup_n} atom(s) below the relevance floor ({floor:.2f}) shown "
            f"dimmed — they did not match this query and would not be injected "
            f"into a prompt. `--gate` hides them.[/dim]"
        )
    elif sup_n and gate:
        console.print(
            f"[dim]{sup_n} atom(s) dropped below the relevance floor "
            f"({floor:.2f}).[/dim]"
        )
    if not kept_n and sup_n:
        console.print(
            f"[yellow]![/yellow] [dim]nothing cleared the floor — the substrate "
            f"has no answer to this query. Best cosine was "
            f"{verdict.top_relevance:.3f}.[/dim]"
        )
    if verdict.scaffolding_count:
        console.print(
            f"[dim]{verdict.scaffolding_count} module-tier atom(s) hidden as "
            f"retrieval scaffolding — `--scaffolding` to show.[/dim]"
        )
    if unverified:
        console.print(
            f"[yellow]?[/yellow] [dim]{unverified} atom(s) could not be verified "
            f"(ast/sql predicates have no runner) — confirm against the source "
            f"before relying on them.[/dim]"
        )


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "goal"


@app.command()
def plan(
    goal: str = typer.Argument(..., help="Natural-language goal, e.g. 'add refund cutoff'."),
    files: list[str] = typer.Option(  # noqa: B008
        None, "--file", "-f", help="File context for structural seeding."
    ),
    top_n: int = typer.Option(30, "--top", help="Number of atoms to retrieve for the goal."),
    min_relevance: float = typer.Option(
        None, "--min-relevance",
        help="Relevance floor. Defaults to retrieval.min_relevance in meristem.toml.",
    ),
    persist: bool = typer.Option(
        False, "--persist", help="Record the plan as a decision atom (queryable later)."
    ),
    json_out: bool = typer.Option(False, "--json", help="JSON output."),
) -> None:
    """Use the memory graph to constrain a plan for `goal` before any edit (SPEC §15).

    Retrieves invariants, schema facts, and CLOSED-decision constraints
    touching the goal, groups them under the module/file surfaces they
    govern, and flags a surface BLOCKED wherever a live BLOCKS edge comes
    from a CLOSED decision (or a live CONTRADICTS edge exists) — so the
    naive approach's collision with prior work surfaces now, not mid-patch.

    Meristem has no LLM in the CLI, so this does not invent task prose — it
    supplies the constraint context; the task list itself is exactly the
    set of module/file/symbol atoms the goal's retrieval touched. Feed the
    result to `/meristem:patch` or a GSD phase as constraint context.
    """
    layout = detect_layout()
    _require_store(layout)
    floor = (
        min_relevance
        if min_relevance is not None
        else config.load(layout.config).retrieval.min_relevance
    )
    from . import planner

    with store.connect(layout.db) as conn:
        result = planner.build_plan(
            conn, goal, top_n=top_n, file_context=files or None,
            min_relevance=floor, repo_root=layout.root,
        )
        if json_out:
            typer.echo(planner.to_json(result))
            return
        # typer.echo, not console.print — the report's own `[decision]`/
        # `[BLOCKED]` brackets are literal text, not Rich markup, and
        # console.print would try (and fail) to parse them as style tags.
        typer.echo(planner.format_report(result))
        if persist:
            atom = planner.persist_plan(conn, result, slug=_slugify(goal))
            console.print(f"\n[green]✓[/green] persisted as {atom.id} (topic {atom.topic_key})")


def _query_empty_note(verdict: retrieval.Gated, *, gate: bool) -> None:
    """Explain an empty result, which is otherwise ambiguous by construction.

    "Nothing relevant exists", "everything was filtered" and "the substrate was
    never built" all render as zero rows. Only the caller's next action differs.
    """
    if gate and verdict.suppressed:
        console.print(
            f"[dim]no atoms cleared the relevance floor "
            f"({verdict.min_relevance:.2f}) — {verdict.suppressed_count} "
            f"retrieved and dropped, best cosine {verdict.top_relevance:.3f}.[/dim]"
        )
    elif verdict.scaffolding:
        console.print(
            f"[dim]no atoms matched — {verdict.scaffolding_count} module-tier "
            f"atom(s) were filtered as scaffolding (`--scaffolding` to show).[/dim]"
        )
    else:
        console.print("[dim]no atoms matched.[/dim]")


# ---------------------------------------------------------------------------
# why (SPEC §16 — observability, the no-hallucination demo surface)
# ---------------------------------------------------------------------------

def _resolve_why_ref(conn, ref: str) -> tuple[atoms_mod.AtomView | None, str | None]:
    """Look up an atom by id, falling back to topic_key. (view, error)."""
    view = atoms_mod.get_atom(conn, ref, include_archived=True)
    if view is not None:
        return view, None
    matches = atoms_mod.query_atoms(conn, topic_key=ref, include_archived=True, limit=5)
    if not matches:
        return None, f"no atom matches id or topic_key {ref!r}"
    if len(matches) > 1:
        ids = ", ".join(a.id for a in matches)
        return None, f"{ref!r} matches {len(matches)} atoms — pass an id instead: {ids}"
    return atoms_mod.get_atom(conn, matches[0].id, include_archived=True), None


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "-"
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).strftime("%Y-%m-%d %H:%MZ")


@app.command()
def why(
    ref: str = typer.Argument(..., help="Atom id or topic_key."),
) -> None:
    """Print an atom's full provenance trail: source, a live liveness re-check, and its edges.

    `meristem query` shows *why an atom was retrieved for a query*; this shows
    *why an atom should be believed*, independent of any query — the
    demonstrable half of the no-hallucination claim (SPEC §16). The liveness
    check here is re-run against the repo right now, not read from cache, so
    the output cannot claim more confidence than the atom currently earns.
    """
    layout = detect_layout()
    _require_store(layout)
    from . import liveness as liveness_mod

    with store.connect(layout.db) as conn:
        view, err = _resolve_why_ref(conn, ref)
        if view is None:
            console.print(f"[red]✗[/red] {err}")
            raise typer.Exit(code=2)
        atom = view.atom

        status_bits = []
        if atom.archived:
            status_bits.append("[dim]archived[/dim]")
        if atom.valid_to:
            status_bits.append(f"[yellow]closed {_fmt_ts(atom.valid_to)}[/yellow]")
        if atom.pinned:
            status_bits.append("[cyan]pinned[/cyan]")
        status = f"  {' '.join(status_bits)}" if status_bits else ""
        console.print(f"[bold]{atom.id}[/bold] [dim]({atom.type})[/dim]{status}")
        console.print(f"topic_key: [bold]{atom.topic_key}[/bold]")
        if 10 in view.summaries:
            console.print(f"\n{view.summaries[10]}")
        if 50 in view.summaries and view.summaries.get(50) != view.summaries.get(10):
            console.print(f"[dim]{view.summaries[50]}[/dim]")

        prov = Table(title="provenance", show_header=False, box=None, pad_edge=False)
        prov.add_column(style="dim")
        prov.add_column()
        source = atom.source_kind + (f" — {atom.source_ref}" if atom.source_ref else "")
        prov.add_row("source", source)
        prov.add_row("asserted", _fmt_ts(atom.asserted_at))
        prov.add_row("valid from", _fmt_ts(atom.valid_from))
        prov.add_row("confidence", f"{atom.confidence:.2f}")
        if atom.type == "decision":
            prov.add_row("decision", f"{atom.decision_status} ({atom.decision_class})")
        if atom.superseded_by:
            prov.add_row("superseded by", atom.superseded_by)
        console.print(prov)

        console.print("\n[bold]liveness[/bold] [dim](re-checked now, not cached)[/dim]")
        if not atom.liveness_kind or atom.liveness_kind == "none":
            console.print("[dim]no predicate — nothing to verify[/dim]")
        else:
            result = liveness_mod.check_atom(conn, atom.id, repo_root=layout.root)
            glyph = {
                "verified": "[green]✓ verified[/green]",
                "trusted": "[green]✓ trusted (cached, recently checked)[/green]",
                "unverifiable": "[yellow]? unverifiable — no runner for this kind[/yellow]",
                "failed": "[red]✗ failed[/red]",
                "none": "[dim]– none[/dim]",
            }.get(result.state, result.state)
            console.print(f"{glyph}\n[dim]{result.reason}[/dim]")
            console.print(
                f"[dim]{atom.liveness_kind}: {atom.liveness_target!r} "
                f"/{atom.liveness_pattern}/[/dim]"
            )

        live_edges = edges_mod.list_edges(conn, atom_id=atom.id, limit=100)
        console.print(f"\n[bold]edges[/bold] ({len(live_edges)})")
        if not live_edges:
            console.print("[dim]none — this atom is unconnected in the graph.[/dim]")
        else:
            etable = Table()
            etable.add_column("dir")
            etable.add_column("kind")
            etable.add_column("other atom")
            etable.add_column("weight", justify="right")
            etable.add_column("evidence")
            for e in live_edges:
                other_id = e.dst_id if e.src_id == atom.id else e.src_id
                other = atoms_mod.get_atom(conn, other_id, include_archived=True)
                other_label = other.atom.topic_key if other else other_id
                direction = "↔" if not e.directed else ("→" if e.src_id == atom.id else "←")
                ev_rows = conn.execute(
                    "SELECT kind, payload FROM edge_evidence WHERE edge_id = ? ORDER BY rowid",
                    (e.id,),
                ).fetchall()
                ev_text = "; ".join(f"{r['kind']}={r['payload']}" for r in ev_rows)
                etable.add_row(
                    direction, e.kind, other_label, f"{e.weight:.2f}",
                    ev_text or "[dim]–[/dim]",
                )
            console.print(etable)


# ---------------------------------------------------------------------------
# enumerate (edge-case enumerator, SPEC §11)
# ---------------------------------------------------------------------------

@app.command(name="enumerate")
def enumerate_cmd(
    json_out: bool = typer.Option(False, "--json", help="JSON output."),
    max_findings: int = typer.Option(5, "--max", help="Cap on findings."),
) -> None:
    """Enumerate edge cases for the staged diff (SPEC §11)."""
    from . import enumerator
    findings = enumerator.enumerate_edge_cases(max_findings=max_findings)
    if json_out:
        typer.echo(enumerator.to_json(findings))
    else:
        typer.echo(enumerator.format_report(findings))


# ---------------------------------------------------------------------------
# statusline (Memory Pulse, SPEC §13)
# ---------------------------------------------------------------------------

@app.command()
def statusline(
    compact: bool = typer.Option(False, "--compact", help="Single-line output."),
) -> None:
    """Render the Memory Pulse statusline."""
    from . import statusline as sl_mod
    line = sl_mod.compact_line() if compact else sl_mod.build_pulse().render()
    typer.echo(line)


# ---------------------------------------------------------------------------
# handoff (PreCompact hook output)
# ---------------------------------------------------------------------------

@app.command()
def handoff(
    next_action: str = typer.Option("", "--next", help="Literal verb+target."),
    title: str = typer.Option("", "--title", help="Current tick title."),
    reason: str = typer.Option("user_handoff", "--reason"),
    narrative: str = typer.Option("", "--narrative"),
) -> None:
    """Write a handoff document to .meristem/handoffs/ (and refresh LATEST.md)."""
    from . import handoff as handoff_mod
    inp = handoff_mod.HandoffInput(
        current_tick_title=title or None,
        next_action=next_action,
        ended_reason=reason,
        narrative=narrative,
    )
    path = handoff_mod.write(inp)
    typer.echo(str(path))


# ---------------------------------------------------------------------------
# watch (PostToolUse hook output)
# ---------------------------------------------------------------------------

_WATCH_ALL_MAX_BATCHES = 200  # safety cap, not the normal exit path — see below


@app.command()
def watch(
    files: list[str] | None = typer.Argument(None, help="Files touched by the tool call."),  # noqa: B008
    all_stale: bool = typer.Option(
        False, "--all",
        help="Re-check the stalest atoms across the whole store instead of named files.",
    ),
    limit: int = typer.Option(
        200, "--limit",
        help="Atoms per batch with --all. `--all` loops this batch size until the "
        "staleness frontier is clear, so raising it trades fewer, larger batches "
        "for the same total work rather than changing how much gets checked.",
    ),
    pretty: bool = typer.Option(False, "--pretty"),
) -> None:
    """Re-run liveness predicates. JSON output.

    With `--all`, sweeps the stalest atoms store-wide (SPEC §14.5) rather than
    following a file list — which is what `meristem doctor` has always told users to
    run when predicates go stale. Each underlying sweep call stays bounded to
    `--limit` atoms (SPEC §14.5's fixed-batch-cost guarantee), but the CLI now
    loops that bounded call until the frontier actually clears — one round no
    longer leaves the bulk of a large backlog stale, which is what `doctor`'s own
    remediation text ("run `meristem watch --all`") has always implied it would do.
    A predicate that genuinely fails never advances its own staleness clock (by
    design — a failing fact should keep surfacing as stale), so the loop stops
    once a batch makes no further progress, rather than re-checking the same
    failing atoms forever; `frontier_cleared: false` in the output says so.
    """
    from . import liveness, store, watcher

    if all_stale:
        layout = detect_layout()
        _require_store(layout)
        with store.connect(layout.db) as conn:
            checked = passed = 0
            failed: list[str] = []
            unverifiable: list[str] = []
            batches = 0
            while batches < _WATCH_ALL_MAX_BATCHES:
                remaining_before = liveness.count_stale(conn)
                if remaining_before == 0:
                    break
                res = liveness.revalidate_sweep(conn, repo_root=layout.root, limit=limit)
                checked += res.checked
                passed += res.passed
                failed.extend(res.failed)
                unverifiable.extend(res.unverifiable)
                batches += 1
                remaining_after = liveness.count_stale(conn)
                if remaining_after >= remaining_before:
                    # Nothing in this batch left the stale set — every candidate
                    # either failed or was unverifiable, so every atom in it will
                    # be selected again next batch with no new information.
                    # Stop rather than loop on atoms that cannot pass right now.
                    break
            frontier_cleared = liveness.count_stale(conn) == 0
        payload = {
            "checked": checked,
            "passed": passed,
            "failed": failed,
            # A predicate with no runner is a coverage gap, not proof of rot.
            "unverifiable": unverifiable,
            "batches": batches,
            "frontier_cleared": frontier_cleared,
        }
        typer.echo(json.dumps(payload, indent=2 if pretty else None))
        raise typer.Exit(code=1 if failed else 0)

    if not files:
        console.print("[red]✗[/red] pass file paths, or use [bold]--all[/bold]")
        raise typer.Exit(code=2)
    rep = watcher.watch(files)
    typer.echo(rep.to_json(pretty=pretty))
    if rep.has_violations:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# route (UserPromptSubmit hook output)
# ---------------------------------------------------------------------------

@app.command()
def route(
    prompt: str = typer.Argument(..., help="The user prompt to classify + retrieve for."),
    file_context: list[str] = typer.Option(  # noqa: B008
        None, "--file", "-f", help="Files providing structural seed context."
    ),
) -> None:
    """Classify a prompt and emit a token-capped atom injection block (JSON)."""
    from . import router
    typer.echo(router.route(prompt, file_context=file_context or None).to_json())


# ---------------------------------------------------------------------------
# digest (SessionStart hook output)
# ---------------------------------------------------------------------------

@app.command()
def digest(
    pretty: bool = typer.Option(False, "--pretty", help="Indent JSON output."),
) -> None:
    """Print the SessionStart digest as JSON (consumed by the SessionStart hook)."""
    from . import digest as digest_mod
    typer.echo(digest_mod.build_digest().to_json(pretty=pretty))


# ---------------------------------------------------------------------------
# hook / hooks — the Claude Code ambient layer, shipped in-package
# (Roadmap Phase 0 #1-#2: port the ~/.claude/hooks/dlms-*.js scripts in,
# with a heartbeat so a dead hook can no longer go unnoticed.)
# ---------------------------------------------------------------------------

@app.command(name="hook", hidden=True)
def hook_cmd(
    event: str = typer.Argument(
        ..., help="Claude Code hook event: " + " | ".join(hooks_mod.HOOK_EVENTS),
    ),
) -> None:
    """Dispatch one Claude Code hook event, reading its JSON payload from stdin.

    Wired up by `meristem hooks install`; not meant to be typed by hand.
    Always exits 0 — a hook that fails loudly stalls the host turn, which is
    worse than any error this could report. Every outcome (including a crash)
    is instead recorded as a heartbeat (`meristem hooks status`, `meristem doctor`).
    """
    if event not in hooks_mod.HOOK_EVENTS:
        console.print(
            f"[red]✗[/red] unknown hook event {event!r} — expected one of "
            f"{', '.join(hooks_mod.HOOK_EVENTS)}"
        )
        raise typer.Exit(code=2)
    raise typer.Exit(code=hooks_mod.run_hook(event))


# ---------------------------------------------------------------------------
# setup — detect coding agents and wire Meristem into them
# ---------------------------------------------------------------------------

_SETUP_HOOK_EVENTS = ("post-commit", "post-rewrite", "post-merge")


def _setup_git_hooks(layout: Layout, dry_run: bool) -> list[setup_mod.Step]:
    """The `sync --install-hook` install path, as setup steps. Reports
    'unchanged' when the managed hook files come out byte-identical."""
    cfg = config.load(layout.config)
    roots = [_resolve_root_path(layout, r) for r in cfg.workspace.roots]
    hook_files = []
    for rp in roots:
        hd = git_utils.hooks_dir(rp)
        if hd is not None:
            hook_files += [hd / e for e in _SETUP_HOOK_EVENTS]

    def _snap() -> dict[Path, str | None]:
        return {
            p: (p.read_text(errors="replace", encoding="utf-8") if p.exists() else None)
            for p in hook_files
        }

    if dry_run:
        if not hook_files:
            return [setup_mod.Step("git", "(workspace)", "skipped", "no git repo among the roots")]
        return [setup_mod.Step(
            "git", ", ".join(str(p) for p in hook_files), "added",
            "post-commit/post-rewrite/post-merge hooks that run `meristem sync` "
            "(skipped for any hook file that is not ours)",
        )]
    before = _snap()
    steps = []
    for path, outcome in _install_post_commit(layout, roots):
        if outcome in ("installed", "updated") and before.get(path) == _snap().get(path):
            outcome = "unchanged"
        elif outcome in ("installed", "updated"):
            outcome = "added"
        elif outcome.startswith("exists"):
            steps.append(setup_mod.Step(
                "git", str(path), "left alone",
                "a hook we did not write exists; add by hand: "
                f"( cd \"{layout.root}\" && meristem sync --quiet >/dev/null 2>&1 & )",
            ))
            continue
        else:
            outcome = "skipped"
        steps.append(setup_mod.Step("git", str(path), outcome))
    return steps


def _print_setup_steps(steps: list[setup_mod.Step], *, dry_run: bool) -> None:
    glyph = {"added": "+", "updated": "+", "unchanged": "·", "snippet": "!",
             "left alone": "!", "skipped": "·"}
    for st in steps:
        verb = st.outcome
        if dry_run and st.changes:
            verb = "would " + st.outcome.replace("added", "add").replace("updated", "update")
        console.print(f"{glyph.get(st.outcome, '·')} [bold]{st.agent}[/bold] {st.target}  "
                      f"[dim]{verb}{' — ' + st.note if st.note else ''}[/dim]",
                      soft_wrap=True)
        if st.snippet and (st.outcome in ("snippet", "left alone") or dry_run):
            if st.outcome == "snippet":
                console.print(f"  add this to {st.target}:", soft_wrap=True)
            console.print("\n".join("    " + ln for ln in st.snippet.splitlines()),
                          markup=False, highlight=False, soft_wrap=True)


@app.command()
def setup(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print exactly what would change; write nothing."
    ),
    agents: str = typer.Option(
        "", "--agents",
        help="Comma-separated subset of: " + ", ".join(setup_mod.AGENTS) + " (default: detected).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without asking."),
) -> None:
    """Detect your coding agents and wire Meristem into them.

    Claude Code gets its hooks plus a project `.mcp.json`; Cursor and Windsurf
    get an `mcpServers` entry; Codex and Gemini CLI get a snippet to paste (their
    formats are not guessed). The current workspace's sync git hooks are
    installed too. Idempotent; foreign config keys are never overwritten, and a
    file is backed up once to `<file>.meristem-bak` before its first change.
    Try it with `uvx meristem setup`.
    """
    home = Path.home()
    if agents.strip():
        chosen = [a.strip().lower() for a in agents.split(",") if a.strip()]
        unknown = [a for a in chosen if a not in setup_mod.AGENTS]
        if unknown:
            console.print(
                f"[red]✗[/red] unknown agent(s): {', '.join(unknown)} "
                f"(choose from {', '.join(setup_mod.AGENTS)})"
            )
            raise typer.Exit(code=2)
    else:
        chosen = setup_mod.detect_agents(home)
        if not chosen:
            console.print(
                "[yellow]·[/yellow] no coding agents detected "
                f"(looked for {', '.join(setup_mod.AGENTS)}); "
                "name one with --agents to set it up anyway."
            )
    layout = detect_layout()
    workspace = layout.root if layout.db.exists() else None

    def _plan(dry: bool) -> list[setup_mod.Step]:
        return setup_mod.run(
            home=home, workspace=workspace, agents=chosen, dry_run=dry,
            git_hooks=(lambda d: _setup_git_hooks(layout, d)) if workspace else None,
        )

    steps = _plan(True)
    if dry_run or not any(s.changes for s in steps):
        console.print(f"[bold]meristem setup[/bold]{'  [dim](dry run)[/dim]' if dry_run else ''}")
        _print_setup_steps(steps, dry_run=dry_run)
        if dry_run:
            console.print("[dim]dry run — nothing was written[/dim]")
        return
    console.print("[bold]meristem setup[/bold]  [dim](plan)[/dim]")
    _print_setup_steps(steps, dry_run=True)
    if not yes and not typer.confirm("Apply these changes?", default=False):
        console.print("[dim]nothing written[/dim]")
        return
    console.print()
    _print_setup_steps(_plan(False), dry_run=False)


hooks_app = typer.Typer(
    name="hooks",
    no_args_is_help=True,
    help="Install, remove, or inspect the Claude Code hook wiring for Meristem.",
)
app.add_typer(hooks_app, name="hooks")


def _resolve_settings_path(
    *, project: bool, use_global: bool, settings_path: Path | None,
) -> Path:
    del project  # --project is the default lane; --global is the only override
    if settings_path is not None:
        return settings_path
    return hooks_mod.default_settings_path(use_global=use_global)


def _print_changes(changes: list[hooks_mod.SettingsChange], *, verb: str) -> None:
    glyph = {
        "installed": "[green]✓[/green]", "updated": "[green]✓[/green]", "set": "[green]✓[/green]",
        "removed": "[green]✓[/green]",
        "unchanged": "[dim]·[/dim]", "not_installed": "[dim]·[/dim]",
    }
    for c in changes:
        mark = glyph.get(c.outcome, "[yellow]·[/yellow]")
        console.print(f"{mark} {c.event}: {c.outcome}")
    if not any(c.outcome not in ("unchanged", "not_installed") for c in changes):
        console.print(f"[dim]nothing to {verb} — already up to date[/dim]")


@hooks_app.command("install")
def hooks_install(
    project: bool = typer.Option(
        True, "--project",
        help="Install into <cwd>/.claude/settings.json (default; implied unless --global).",
    ),
    use_global: bool = typer.Option(
        False, "--global", help="Install into ~/.claude/settings.json instead of the project.",
    ),
    settings_path: Path = typer.Option(  # noqa: B008
        None, "--settings-path", help="Explicit settings.json path (overrides --project/--global).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change; write nothing.",
    ),
    statusline: bool = typer.Option(
        False, "--statusline",
        help="Also set statusLine to `meristem statusline --compact` (only if absent or ours).",
    ),
) -> None:
    """Wire the five Meristem hooks into a Claude Code settings.json.

    Idempotent — re-running changes nothing once installed. Merges in place:
    every other key, and every other tool's hook entries, are preserved
    byte-for-byte. Identifies its own entries by the `meristem hook ` command
    prefix, the same way `_write_managed_hook`'s git-hook markers work.
    """
    path = _resolve_settings_path(
        project=project, use_global=use_global, settings_path=settings_path,
    )
    changes = hooks_mod.install_hooks(settings_path=path, dry_run=dry_run, statusline=statusline)
    console.print(f"[bold]{path}[/bold]{'  [dim](dry run)[/dim]' if dry_run else ''}")
    _print_changes(changes, verb="install")


@hooks_app.command("uninstall")
def hooks_uninstall(
    project: bool = typer.Option(
        True, "--project",
        help="Target the project settings.json (default; implied unless --global).",
    ),
    use_global: bool = typer.Option(
        False, "--global", help="Target ~/.claude/settings.json instead.",
    ),
    settings_path: Path = typer.Option(  # noqa: B008
        None, "--settings-path", help="Explicit settings.json path (overrides --project/--global).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change; write nothing."),
) -> None:
    """Remove only Meristem's five hook entries. Everything else in the file
    — including a foreign statusLine — is left exactly as it was."""
    path = _resolve_settings_path(
        project=project, use_global=use_global, settings_path=settings_path,
    )
    changes = hooks_mod.uninstall_hooks(settings_path=path, dry_run=dry_run)
    console.print(f"[bold]{path}[/bold]{'  [dim](dry run)[/dim]' if dry_run else ''}")
    _print_changes(changes, verb="remove")


@hooks_app.command("status")
def hooks_status_cmd() -> None:
    """Show, per hook event: where it's installed, and the last heartbeat
    this workspace recorded for it — plus any stale `dlms-*` wiring left over
    from before the rename."""
    layout = detect_layout()
    project_path = hooks_mod.default_settings_path(use_global=False, cwd=layout.root)
    global_path = hooks_mod.default_settings_path(use_global=True)

    statuses = hooks_mod.hooks_status(
        project_path=project_path, global_path=global_path, layout=layout,
    )
    table = Table(title="meristem hooks")
    table.add_column("event")
    table.add_column("installed")
    table.add_column("last invoked")
    table.add_column("last outcome")
    table.add_column("count", justify="right")
    for s in statuses:
        where = "+".join(s.installed_in) if s.installed_in else "[dim]none[/dim]"
        table.add_row(
            s.event, where,
            s.last_invoked_at or "[dim]never[/dim]",
            s.last_outcome or "[dim]-[/dim]",
            str(s.invocations),
        )
    console.print(table)

    user_hb = hooks_mod.read_user_heartbeats()
    if user_hb and not layout.db.exists():
        console.print(
            "[dim]hooks have fired on this machine for other/non-workspace directories — "
            f"see {hooks_mod.user_state_dir() / 'hook_heartbeat.json'}[/dim]"
        )

    legacy = hooks_mod.scan_legacy_wiring(project_path, global_path)
    if legacy:
        console.print()
        for event_name, cmd, path in legacy:
            console.print(
                f"[red]⚠[/red] legacy DLMS hook (dead since rename) — remove it: "
                f"{path} [{event_name}] {cmd!r}"
            )


# ---------------------------------------------------------------------------
# mcp
# ---------------------------------------------------------------------------

@app.command()
def mcp(
    transport: str = typer.Option(
        "stdio", "--transport", "-t",
        help="MCP transport: stdio (local clients) | http | sse (remote/web agents).",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host for http/sse."),
    port: int = typer.Option(8765, "--port", help="Bind port for http/sse."),
) -> None:
    """Run the Meristem MCP server. Requires the ``meristem[mcp]`` extra.

    Standard Model Context Protocol — usable by any MCP client (Claude Code,
    Claude Desktop, Cursor, Windsurf, Cline, Continue). Default ``stdio`` suits
    local clients that spawn it as a subprocess; use ``--transport http`` to
    serve remote/web agents over a port.
    """
    from . import mcp_server
    mcp_server.run(transport, host=host, port=port)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

@app.command(name="doctor")
def doctor_cmd(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show detail line for each finding."
    ),
) -> None:
    """Health check across schema, atoms, liveness, embeddings, edges, retrieval.

    Exit code = number of fail-level findings (0 = green). Warnings do not
    affect exit code — use them as nudges, not gates.
    """
    layout = detect_layout()
    report = doctor.run(layout)

    glyph = {"ok": "[green]✓[/green]", "warn": "[yellow]⚠[/yellow]", "fail": "[red]✗[/red]"}
    for f in report.findings:
        console.print(f"{glyph[f.severity]} [bold]{f.name}[/bold]  {f.summary}")
        if verbose and f.detail:
            for line in f.detail.splitlines():
                console.print(f"    [dim]{line}[/dim]")

    fails, warns = report.fail_count, report.warn_count
    if fails == 0 and warns == 0:
        console.print("\n[green]doctor: all green[/green]")
    else:
        console.print(
            f"\n[bold]doctor:[/bold] {fails} fail, {warns} warn, "
            f"{len(report.findings) - fails - warns} ok"
        )
    # Cap exit code at 1 — unix convention is binary success/fail, and bytes
    # >125 wrap on POSIX so a literal fail-count is unreliable past that.
    raise typer.Exit(code=0 if fails == 0 else 1)


# ---------------------------------------------------------------------------
# calibrate (SPEC §14.6 — the relevance floor is not a constant)
# ---------------------------------------------------------------------------

@app.command(name="calibrate")
def calibrate_cmd(
    write: bool = typer.Option(
        False, "--write", help="Write the recommendation into meristem.toml."
    ),
    limit: int = typer.Option(
        200, "--limit", help="Most-recent distinct logged prompts to replay."
    ),
    json_out: bool = typer.Option(False, "--json", help="JSON output."),
) -> None:
    """Re-derive `[retrieval] min_relevance` from this workspace's own prompts.

    The shipped 0.70 was hand-derived from one embedder against one corpus. It
    is a starting point: on a near-empty store it suppresses genuine matches,
    and under a different embedder it may not gate at all. This replays your
    logged prompts and a fixed nonsense probe set through real retrieval, and
    puts the floor in the gap between them.

    Read-only unless `--write`. Exit code 0 when a floor was derived, 1 when
    there is not enough data to derive one — that is a "come back later", not a
    breakage.
    """
    layout = detect_layout()
    _require_store(layout)
    current = config.load(layout.config).retrieval.min_relevance
    with store.connect(layout.db) as conn:
        result = calibrate.derive(conn, current=current, limit=limit)

    if json_out:
        console.print_json(data={
            "status": result.status,
            "recommended": result.recommended,
            "current": result.current,
            "nonsense_ceiling": result.nonsense_ceiling,
            "genuine_floor": result.genuine_floor,
            "separation": result.separation,
            "n_genuine": result.n_genuine,
            "n_nonsense": result.n_nonsense,
            "n_embedded": result.n_embedded,
            "reason": result.reason,
        })
    else:
        _print_calibration(result)

    if result.status != "ok":
        raise typer.Exit(code=1)

    if write:
        if result.recommended == current:
            console.print(
                f"[dim]meristem.toml already has min_relevance = {current} — "
                f"nothing to write.[/dim]"
            )
            return
        if result.recommended is None:
            console.print(
                "[yellow]·[/yellow] no recommendation to write — "
                "not enough scored turns yet."
            )
            return
        config.set_min_relevance(layout.config, result.recommended)
        console.print(
            f"[green]✓[/green] wrote min_relevance = {result.recommended} "
            f"to {layout.config} (was {current})"
        )
    elif result.recommended != current:
        console.print(
            f"\n[dim]`meristem calibrate --write` to store {result.recommended} "
            f"in meristem.toml.[/dim]"
        )


def _print_calibration(result: calibrate.Calibration) -> None:
    if result.status == "insufficient_data":
        console.print(f"[yellow]⚠[/yellow] cannot calibrate — {result.reason}")
        return
    table = Table(title="relevance calibration")
    table.add_column("measure")
    table.add_column("value", justify="right")
    table.add_column("from", justify="right")
    table.add_row(
        "nonsense ceiling (p95)", f"{result.nonsense_ceiling:.4f}",
        f"{result.n_nonsense} probes",
    )
    table.add_row(
        "genuine floor (p25)", f"{result.genuine_floor:.4f}",
        f"{result.n_genuine} prompts",
    )
    sep = result.separation or 0.0
    table.add_row(
        "separation",
        f"[{'green' if sep > 0 else 'red'}]{sep:+.4f}[/]",
        "",
    )
    console.print(table)

    if result.status == "not_separable":
        console.print(f"[red]✗[/red] no usable floor — {result.reason}")
        return
    console.print(f"[dim]{result.reason}[/dim]")
    drift = result.drift or 0.0
    verdict = (
        "[green]matches your config[/green]" if abs(drift) < 0.005
        else f"[yellow]config is {abs(drift):.3f} too "
             f"{'high' if drift > 0 else 'low'}[/yellow]"
    )
    console.print(
        f"\nrecommended [bold]{result.recommended}[/bold]  "
        f"(configured {result.current}) — {verdict}"
    )
    if drift > 0.005:
        console.print(
            "[dim]A floor set too high is the mute failure: real matches get "
            "suppressed and Meristem looks empty rather than wrong.[/dim]"
        )
    elif drift < -0.005:
        console.print(
            "[dim]A floor set too low leaks unrelated atoms into every prompt, "
            "which is how a memory layer teaches its reader to ignore it.[/dim]"
        )


# ---------------------------------------------------------------------------
# reap (SPEC §14.5 — an atom must not outlive its source)
# ---------------------------------------------------------------------------

@app.command()
def reap(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would close, change nothing."
    ),
    since: str = typer.Option(
        None, "--since",
        help="Diff from this sha instead of each root's last indexed sha. "
             "Use a repo's first commit to sweep a store that predates reaping.",
    ),
) -> None:
    """Close atoms whose source file was deleted from the repo.

    `meristem ingest` reaps the interval it just indexed, so this is for the
    backlog: a store built before reaping existed has atoms for every file ever
    deleted, all still live. Point `--since` at an old sha (or the root commit)
    to sweep them in one pass.

    Reaping closes atoms — it never deletes rows. Acts only on deletions git
    reports; an atom the reaper cannot positively tie to a deleted path is left
    alone.
    """
    layout = detect_layout()
    _require_store(layout)
    cfg = config.load(layout.config)
    total = 0
    undetermined: list[str] = []
    with store.connect(layout.db) as conn:
        for raw_root in cfg.workspace.roots:
            rp = _resolve_root_path(layout, raw_root)
            if not rp.exists():
                continue
            rid = _repo_id(rp)
            base = since or _prior_indexed_sha(conn, rid)
            out = reaper.reap(
                conn, rp, since_sha=base, repo_id=rid, dry_run=dry_run
            )
            label = str(rp.relative_to(layout.root)) if rp.is_relative_to(layout.root) else str(rp)
            if out.undetermined:
                undetermined.append(label)
                continue
            total += out.count
            if out.count:
                verb = "would close" if dry_run else "closed"
                console.print(
                    f"[green]✓[/green] {label}: {verb} {out.count} atom(s) "
                    f"({len(out.atom_ids)} file-backed, "
                    f"{len(out.dir_atom_ids)} directory-backed) across "
                    f"{len(out.deleted_paths)} deleted path(s)"
                )
    for label in undetermined:
        # An unanswerable question must not render as a clean sweep.
        console.print(
            f"[yellow]⚠[/yellow] {label}: could not determine deletions "
            f"(no indexed sha, or git could not answer) — nothing reaped"
        )
    if not total and not undetermined:
        console.print("[dim]nothing to reap — no atom outlives its source.[/dim]")
    elif dry_run:
        console.print(f"\n[dim]dry run — {total} atom(s) left untouched.[/dim]")


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------

@app.command()
def migrate() -> None:
    """Apply pending schema migrations to the workspace database.

    Brings a DB created under an older schema up to the current version:
    additive column adds (`atoms.tier`), constraint widening (`edges.kind`
    ROLLS_UP via table rebuild), and any missing indexes/views. Idempotent —
    safe to run repeatedly. This is the command `meristem doctor` points you to
    when it reports a schema-version gap.
    """
    import sqlite3

    layout = detect_layout()
    _require_store(layout)

    # Read the version with a raw connection FIRST — store.connect() runs
    # migrations on open, so going through it would mask the "before" state.
    raw = sqlite3.connect(layout.db)
    try:
        row = raw.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        before = int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        before = 0
    finally:
        raw.close()

    with store.connect(layout.db) as conn:
        after = store.init_schema(conn)
        edges_ok = bool(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'"
            ).fetchone()[0].find("ROLLS_UP") >= 0
        )

    if after > before:
        console.print(f"[green]✓[/green] migrated schema v{before} → v{after}")
    else:
        console.print(f"[green]✓[/green] schema already current (v{after})")
    console.print(f"[dim]edges.kind accepts ROLLS_UP: {edges_ok}[/dim]")


# ---------------------------------------------------------------------------
# capture / propose / review — the write path (SPEC §18)
# ---------------------------------------------------------------------------

def _open_workspace() -> tuple[Layout, sqlite3.Connection]:
    """Layout + connection, or exit with the standard not-initialized message.

    The return type was `tuple[object, object]`, which silently disabled type
    checking at all ten call sites — every `conn` and `layout` downstream was
    an opaque `object`, so nothing could be checked about how they were used.
    Naming the real types is what surfaced the rest of this module's errors.
    """
    layout = detect_layout()
    if not layout.db.exists():
        console.print(
            f"[yellow]·[/yellow] Meristem not initialized in {layout.root} — "
            "run [bold]meristem init[/bold]"
        )
        raise typer.Exit(code=2)
    return layout, store.connect(layout.db)


@app.command()
def capture(
    transcript: Path = typer.Option(  # noqa: B008
        ..., "--transcript", "-t", help="Path to the session transcript JSONL."
    ),
    threshold: int = typer.Option(
        None, "--threshold", help="Durability score cutoff (default 4)."
    ),
    messages: int = typer.Option(
        None, "--messages", "-n", help="Only scan the last N user messages."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="JSON output, for hooks."),
) -> None:
    """Extract durable facts from a transcript into the review queue.

    Proposes only — nothing becomes memory until `meristem review --accept`.
    """
    from . import capture as capture_mod
    from . import config as config_mod

    layout, conn = _open_workspace()
    try:
        cfg = config_mod.load(layout.config)
        fresh = capture_mod.capture_transcript(
            conn,
            transcript,
            threshold=(
                threshold if threshold is not None else capture_mod.DEFAULT_THRESHOLD
            ),
            limit_messages=messages,
            exclude_terms=cfg.capture.exclude_terms,
        )
        total = candidates.pending_count(conn)
    finally:
        conn.close()

    if quiet:
        typer.echo(json.dumps({"captured": len(fresh), "pending": total}))
        return
    if not fresh:
        console.print(f"[dim]· nothing new captured ({total} pending)[/dim]")
        return
    console.print(f"[green]✓[/green] captured {len(fresh)} new — {total} pending review")
    for f in fresh[:5]:
        console.print(f"  [dim]{f.score:>2}[/dim] {f.text[:88]}")


@app.command()
def propose(
    text: str = typer.Argument(..., help="The fact to propose."),
    type_: str = typer.Option("convention", "--type", help="Proposed atom type."),
) -> None:
    """Queue a fact for review without going through a transcript."""
    layout, conn = _open_workspace()
    try:
        added = candidates.queue(conn, text=text, proposed_type=type_, source="cli")
        total = candidates.pending_count(conn)
    finally:
        conn.close()
    if added:
        console.print(f"[green]✓[/green] queued — {total} pending review")
    else:
        console.print("[yellow]·[/yellow] already known (queued, accepted or rejected)")


@app.command()
def review(
    accept: str = typer.Option(None, "--accept", help="Fingerprint prefix to accept."),
    reject: str = typer.Option(None, "--reject", help="Fingerprint prefix to reject."),
    type_: str = typer.Option(None, "--type", help="Override type when accepting."),
    topic: str = typer.Option(None, "--topic", help="Override topic_key when accepting."),
    limit: int = typer.Option(20, "--limit", help="How many pending to list."),
    noise: bool = typer.Option(
        False,
        "--noise",
        help="List pending candidates the current filter would no longer propose.",
    ),
    reject_noise: bool = typer.Option(
        False,
        "--reject-noise",
        help="Reject exactly the candidates `--noise` lists (explicit; never automatic).",
    ),
) -> None:
    """List, accept or reject captured facts.

    Accepting mints an atom carrying a reconfirmation horizon instead of a
    liveness predicate — there is no code to check a domain rule against, so
    the honest expiry is a date by which a human must say it is still true.
    """
    layout, conn = _open_workspace()
    try:
        if accept:
            c = candidates.resolve(conn, accept)
            if c is None:
                console.print("[red]✗[/red] no unique candidate for that prefix")
                raise typer.Exit(code=2)
            atom = candidates.accept(conn, c.fingerprint, type_=type_, topic_key=topic)
            if atom is None:
                console.print("[yellow]·[/yellow] not pending — already reviewed")
                raise typer.Exit(code=1)
            console.print(f"[green]✓[/green] accepted → {atom.id} ({atom.type})")
            console.print("[dim]  run `meristem embed` to make it retrievable[/dim]")
            return
        if reject:
            c = candidates.resolve(conn, reject)
            if c is None:
                console.print("[red]✗[/red] no unique candidate for that prefix")
                raise typer.Exit(code=2)
            if candidates.reject(conn, c.fingerprint):
                console.print("[green]✓[/green] rejected — it will not be proposed again")
            else:
                console.print("[yellow]·[/yellow] not pending — already reviewed")
            return

        if noise or reject_noise:
            flagged = candidates.noise(conn)
            if reject_noise:
                done = sum(1 for c, _ in flagged if candidates.reject(conn, c.fingerprint))
                console.print(
                    f"[green]✓[/green] rejected {done} noise candidate(s) — "
                    "they will not be proposed again"
                )
                return
            if not flagged:
                console.print("[dim]· no pending candidate would be dropped by the filter[/dim]")
                return
            for c, rule in flagged:
                hint = candidates.render_suggestion(layout, c.fingerprint)
                typer.echo(
                    f"{c.fingerprint[:8]}  {rule:<18} {c.text[:60]}"
                    + (f"  [{hint}]" if hint else "")
                )
            typer.echo(
                f"{len(flagged)} pending candidate(s) the current filter would not propose "
                "— `meristem review --reject-noise` rejects them"
            )
            return

        rows = candidates.pending(conn, limit=limit)
        stale = candidates.unconfirmed_atoms(conn)
    finally:
        conn.close()

    if not rows:
        console.print("[dim]· nothing pending review[/dim]")
    else:
        table = Table(title=f"pending review ({len(rows)})", show_lines=False)
        table.add_column("id", style="dim", no_wrap=True)
        table.add_column("score", justify="right")
        table.add_column("type")
        table.add_column("fact")
        table.add_column("agent")
        for c in rows:
            table.add_row(
                c.fingerprint[:8], str(c.score), c.proposed_type, c.text[:76],
                candidates.render_suggestion(layout, c.fingerprint),
            )
        console.print(table)
        console.print(
            "[dim]meristem review --accept <id> [--type T] [--topic K] | --reject <id>[/dim]"
        )
    if stale:
        console.print(
            f"[yellow]⚠[/yellow] {len(stale)} accepted fact(s) past their "
            "reconfirmation horizon — they surface marked `unconfirmed`"
        )


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

@app.command()
def version() -> None:
    """Print the Meristem CLI version."""
    console.print(f"meristem {__version__}")


# Keep `layout_for` importable from this module for tests/external callers.
__all__ = ["app", "layout_for"]
