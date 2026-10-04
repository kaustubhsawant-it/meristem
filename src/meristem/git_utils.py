"""Thin wrappers around git. All silent on non-repo paths."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _run(args: list[str], cwd: Path, *, strip: bool = True) -> str | None:
    """Run git, return stdout. `strip=False` for NUL-separated output.

    Stripping is right for `rev-parse`-style single values and WRONG for
    `--porcelain -z`, whose first record is ` M path` — a *significant* leading
    space. Stripping ate it, so `entry[3:]` then cut one character into the
    filename and produced `EADME.md`. The mangled path matched nothing on disk,
    which silently dropped the first modified-but-unstaged file from every
    incremental ingest — the most common dirty state there is.
    """
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )
        return out.stdout.strip() if strip else out.stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def head_sha(cwd: Path) -> str | None:
    return _run(["rev-parse", "HEAD"], cwd)


def current_branch(cwd: Path) -> str | None:
    return _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd)


def is_repo(cwd: Path) -> bool:
    return _run(["rev-parse", "--is-inside-work-tree"], cwd) == "true"


def set_config(cwd: Path, key: str, value: str) -> bool:
    """Set a local (repo-scoped) git config key. Returns False on a non-repo
    or missing git — local config is inherently per-clone (SPEC §16's merge
    driver registration), never something a committed file can carry."""
    return _run(["config", key, value], cwd) is not None


def dirty_paths(cwd: Path) -> set[str] | None:
    """Repo-relative paths with uncommitted changes, including untracked files.

    ``None`` means git could not answer (not a repo, git missing) — distinct
    from an empty set, which means "the working tree is clean".
    """
    dirty = _run(["status", "--porcelain", "-z"], cwd, strip=False)
    if dirty is None:
        return None
    # porcelain -z: each record is "XY <path>"; rename/copy adds a second
    # NUL-separated field (the OLD path) which we skip — we want the new path.
    files: set[str] = set()
    tokens = [t for t in dirty.split("\0") if t]
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        files.add(entry[3:])  # 2-char status + space, then the path
        if entry[0] in ("R", "C"):  # rename/copy: the next token is the old path
            i += 1
    return files


def commits_between(cwd: Path, since_sha: str | None, until: str = "HEAD") -> int | None:
    """How many commits `until` is ahead of `since_sha`.

    ``None`` means *unknown*, and the distinction from ``0`` is the whole point
    of this function: a sha git cannot resolve — rebased away, force-pushed
    over, or from a different repo entirely — is not a repo that happens to be
    up to date. Reporting 0 there would let a store that can never be
    incrementally caught up claim it is current, which is precisely the silent
    staleness §19 exists to end.
    """
    if not since_sha:
        return None
    out = _run(["rev-list", "--count", f"{since_sha}..{until}"], cwd)
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def hooks_dir(cwd: Path) -> Path | None:
    """Absolute path to this repo's hooks directory, or ``None`` outside a repo.

    Asks git rather than assuming `.git/hooks`: that assumption is wrong in a
    linked worktree (where `.git` is a file) and wrong again when `core.hooksPath`
    redirects hooks elsewhere.
    """
    out = _run(["rev-parse", "--git-path", "hooks"], cwd)
    if out is None:
        return None
    p = Path(out)
    return p if p.is_absolute() else (cwd / p).resolve()


def changed_files_since(cwd: Path, since_sha: str | None) -> set[str] | None:
    """Repo-relative paths changed since `since_sha`, for incremental ingest.

    Returns the union of committed changes (`git diff <since>..HEAD`) and the
    current working-tree changes (`git status --porcelain`, incl. untracked) so
    edits picked up between commits are re-ingested too. Returns ``None`` when
    `since_sha` is falsy or git is unavailable — the caller treats ``None`` as
    "do a full scan" (first-run semantics), distinct from an empty set ("repo
    unchanged, nothing to do").
    """
    if not since_sha:
        return None
    files: set[str] = set()
    # `-z` gives NUL-separated, *unquoted* paths — git otherwise quotes/escapes
    # names with spaces or non-ASCII bytes (core.quotepath), which would never
    # match a real path and silently drop those files from incremental ingest.
    committed = _run(["diff", "--name-only", "-z", since_sha, "HEAD"], cwd, strip=False)
    if committed is None:
        return None  # git failed / bad sha — fall back to full scan
    files.update(p for p in committed.split("\0") if p)
    dirty = dirty_paths(cwd)
    if dirty is None:
        return None  # status failed — full scan beats trusting a partial set
    files.update(dirty)
    return files


def deleted_files_since(cwd: Path, since_sha: str | None) -> set[str] | None:
    """Repo-relative paths DELETED since `since_sha` (SPEC §14.5, deletion reaper).

    Deliberately narrower than `changed_files_since`. Reaping closes atoms, so it
    must act on positive evidence that a path was removed — never on "the file
    isn't there, so it probably went away". Atom `source_ref` values are not all
    paths (`commit:` atoms carry a sha), and nothing in the schema distinguishes
    them, so an absence test would happily reap every commit atom in the store.
    `--diff-filter=D` gives deletion as a fact git asserts.

    Returns ``None`` when `since_sha` is falsy or git could not answer — the
    caller must treat that as "reap nothing", which is the opposite of how
    `changed_files_since` treats ``None``. Being unable to tell what was deleted
    is a reason to close no atoms at all, not a reason to rescan and close many.
    """
    if not since_sha:
        return None
    out = _run(
        ["diff", "--diff-filter=D", "--name-only", "-z", since_sha, "HEAD"],
        cwd,
        strip=False,
    )
    if out is None:
        return None
    deleted = {p for p in out.split("\0") if p}
    # Working-tree deletions too: a file removed but not yet committed is gone
    # from the reader's point of view, and the atom describing it is already
    # wrong. ` D` (unstaged) and `D ` (staged) both count; `R` renames report
    # the old path under `status --porcelain -z` as a separate record.
    status = _run(["status", "--porcelain", "-z"], cwd, strip=False)
    if status is None:
        return None
    for record in status.split("\0"):
        if len(record) > 3 and record[0:2] in ("D ", " D", "DD", "AD"):
            deleted.add(record[3:])
    return deleted


def tracked_under(cwd: Path, prefix: str) -> int | None:
    """How many tracked files remain under `prefix`. ``None`` if git can't say.

    Directory-backed atoms (`module:`, `owner:`) are not reaped by file
    deletions, because git reports deleted files and never deleted directories.
    A subsystem whose last file is gone would otherwise keep a live module atom
    forever — and module atoms are the highest-degree nodes in the graph, so a
    dead one keeps seeding drill-down into a subsystem that no longer exists.
    """
    out = _run(["ls-files", "-z", "--", prefix], cwd, strip=False)
    if out is None:
        return None
    return len([p for p in out.split("\0") if p])


# ---------------------------------------------------------------------------
# meristem-managed hook files — the workspace list they carry
# ---------------------------------------------------------------------------

# A managed hook lists its workspaces in a quoted heredoc, one `actions|path`
# line each (`actions` = comma-joined subset of import/sync). A quoted heredoc
# is used so the shell never expands or word-splits a path — workspaces live
# under directories like "New Implementation". The parser below and the writer
# in cli.py must agree on these two delimiters.
HOOK_WS_OPEN = "<<'MERISTEM_WORKSPACES'"
HOOK_WS_CLOSE = "MERISTEM_WORKSPACES"

_LEGACY_WS = re.compile(r'^\[ -f "(?P<ws>.+)/\.meristem/atoms\.sqlite" \] \|\| exit 0$', re.M)
_LEGACY_ACTION = re.compile(r"meristem (?P<cmd>import|sync) --quiet")


def managed_hook_workspaces(hook: Path, marker: str) -> dict[str, set[str]] | None:
    """Workspace -> actions carried by a meristem-managed hook file.

    Returns ``None`` when the file is absent or is not ours (no `marker`), so a
    caller can tell "no managed hook" from "managed hook listing nobody". Also
    reads the pre-list format (one hard-coded workspace in a `[ -f ... ]`
    guard), so an existing hook is upgraded in place rather than orphaned. Never
    writes.
    """
    try:
        text = hook.read_text(errors="replace", encoding="utf-8")
    except OSError:
        return None
    if marker not in text:
        return None
    found: dict[str, set[str]] = {}
    lines = text.splitlines()
    if any(ln.rstrip().endswith(HOOK_WS_OPEN) for ln in lines):
        inside = False
        for ln in lines:
            if ln.rstrip().endswith(HOOK_WS_OPEN):
                inside = True
            elif ln == HOOK_WS_CLOSE:
                inside = False
            elif inside and "|" in ln:
                actions, ws = ln.split("|", 1)
                acts = {a for a in actions.split(",") if a}
                if ws and acts:
                    found.setdefault(ws, set()).update(acts)
        return found
    legacy = _LEGACY_WS.search(text)
    action = _LEGACY_ACTION.search(text)
    if legacy and action:
        found[legacy.group("ws")] = {action.group("cmd")}
    return found
