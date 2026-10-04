"""Filesystem layout helpers — workspace root discovery + canonical paths.

Meristem lives in `<workspace_root>/.meristem/` and is configured by
`<workspace_root>/meristem.toml`. The workspace root is the git toplevel when
available; otherwise the current working directory at `init` time.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

# Renamed from DLMS (2026-08-11) — see SPEC.md's "Renamed from DLMS" note.
# A workspace initialized under the old name has its state here instead.
LEGACY_STATE_DIRNAME = ".dlms"


@dataclass(frozen=True)
class Layout:
    root: Path           # workspace root (git toplevel or init cwd)
    config: Path         # <root>/meristem.toml
    state_dir: Path      # <root>/.meristem/
    db: Path             # <root>/.meristem/atoms.sqlite
    handoffs: Path       # <root>/.meristem/handoffs/
    jobs_log: Path       # <root>/.meristem/jobs.ndjson


def git_toplevel(start: Path, *, timeout: float | None = None) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return Path(out.stdout.strip())


def layout_for(root: Path) -> Layout:
    root = root.resolve()
    state = root / ".meristem"
    return Layout(
        root=root,
        config=root / "meristem.toml",
        state_dir=state,
        db=state / "atoms.sqlite",
        handoffs=state / "handoffs",
        jobs_log=state / "jobs.ndjson",
    )


def legacy_state_present(layout: Layout) -> bool:
    """True when an old `.dlms/` workspace exists but hasn't been re-init'd
    under the new name yet. Pure check — callers decide how loudly to say so."""
    legacy = layout.root / LEGACY_STATE_DIRNAME
    return legacy.exists() and not layout.state_dir.exists()


def detect_layout(start: Path | None = None) -> Layout:
    """Detect the layout from CWD or `start`. Prefer git toplevel."""
    start = (start or Path.cwd()).resolve()
    root = git_toplevel(start) or start
    return layout_for(root)


def git_common_dir(start: Path, *, timeout: float | None = None) -> Path | None:
    """Absolute path to the repo's shared `.git` directory: the MAIN
    checkout's `.git/`, even when `start` is inside a linked worktree
    (`git worktree add`) — that is what distinguishes it from
    `git_toplevel`, which returns the *worktree's own* root in that case.
    Never raises, same as `git_toplevel`: a missing git binary or a non-repo
    `start` both come back as `None`."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=start,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return Path(out.stdout.strip())


# Per git probe in `resolve_workspace`. It runs in `hooks.dispatch` BEFORE the
# handler's bounded budget (`hooks.DEFAULT_BUDGET_SECONDS`, 8s) starts, so an
# unbounded git (stuck index.lock, a hanging fsmonitor, a stalled network
# mount) would stall the host turn indefinitely — found in review 2026-09-23.
# Two probes at most, so the worst case stays well inside that budget;
# `git rev-parse` normally answers in milliseconds.
GIT_PROBE_TIMEOUT_SECONDS = 2.0


def resolve_workspace(cwd: Path) -> Layout:
    """The Layout for the Meristem workspace that actually governs `cwd`.

    `hooks.dispatch`/`hooks.record_heartbeat` use this instead of plain
    `layout_for(cwd)` (added 2026-09-23, sibling-root fix).
    `layout_for(cwd)` only ever finds a workspace whose `.meristem/` sits
    directly under `cwd` — wrong for a hook firing from a subdirectory of a
    workspace, and *always* wrong from inside a `git worktree add` checkout,
    since `.meristem/` is untracked and therefore exists only in the main
    checkout, never in a linked worktree. A real project with 106 worktrees
    under it got zero recall and zero recorded heartbeat for every worktree
    session as a result — the hooks silently treated every one of them as
    `not_workspace`.

    Tries, in order, and returns the first candidate whose
    `.meristem/atoms.sqlite` actually exists — a directory that hasn't run
    `init` yet must still read as `not_workspace`, not silently adopt an
    unrelated ancestor just because one happens to be nearby:
      1. `cwd` itself — exactly `layout_for(cwd)`. A workspace nested inside
         another repo (or with no git at all) resolves to itself, same as
         before this function existed.
      2. `git rev-parse --show-toplevel` from `cwd` — a subdirectory of an
         ordinary (non-worktree) workspace.
      3. The MAIN checkout of a linked worktree: the parent directory of
         `git_common_dir(cwd)`, which points at the main checkout's `.git/`
         even when run from inside a linked worktree.
    None of the three finds an initialized store → falls back to
    `layout_for(cwd)` regardless, so the `not_workspace` outcome is
    preserved for a genuinely uninitialized directory. A git probe that
    times out (`GIT_PROBE_TIMEOUT_SECONDS`) counts as "not found"."""
    cwd = cwd.resolve()
    candidate = layout_for(cwd)
    if candidate.db.exists():
        return candidate

    toplevel = git_toplevel(cwd, timeout=GIT_PROBE_TIMEOUT_SECONDS)
    if toplevel is not None:
        candidate = layout_for(toplevel)
        if candidate.db.exists():
            return candidate

    common_dir = git_common_dir(cwd, timeout=GIT_PROBE_TIMEOUT_SECONDS)
    if common_dir is not None:
        candidate = layout_for(common_dir.parent)
        if candidate.db.exists():
            return candidate

    return layout_for(cwd)


def schema_sql_text() -> str:
    """Return the bundled schema.sql contents.

    Resolution order:
      1. Packaged resource (`meristem/schema.sql`, when installed).
      2. Source-tree sibling (`<repo root>/schema.sql`, when running from a checkout).
    """
    try:
        with resources.files("meristem").joinpath("schema.sql").open("r", encoding="utf-8") as fh:
            return fh.read()
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    # Dev checkout: src/meristem/paths.py -> parents[2] == repo root
    fallback = Path(__file__).resolve().parents[2] / "schema.sql"
    if fallback.exists():
        return fallback.read_text(encoding="utf-8")
    raise RuntimeError("schema.sql not found in package data or source tree")
