"""Post-ingest edge discovery — the SPEC §4 auto-discovery rules.

Ingesters emit *atoms*. Until this module existed nothing emitted *edges*
except the ROLLS_UP pass inside the `modules` adapter and SUPERSEDES /
CONTRADICTS from `assert_fact`. With no edges, Personalized PageRank has no
graph to propagate through: `_forward_reachable` returns just the seeds, all
rank mass leaks back to the personalization vector, and retrieval degenerates
to embedding k-NN. That is the entire difference between Meristem and a plain
vector store (SPEC §4), so this pass is load-bearing rather than an
enhancement. `retrieval._seed_set` even documents the gap in a comment —
"LOCATED_IN edges are populated by the post-ingest edge-discovery pass" — for
a pass that was never wired.

This module is that pass. It runs after every ingester for a root has
completed (so all atoms exist) and derives typed edges from data Meristem already
holds. Deterministic and stdlib-only: no LLM in the hot path (SPEC §1).

Rules implemented here
----------------------
1. Shared file refs         → REFERENCES  (weight 0.6)
2. Same symbol across repos → MIRRORS     (weight <= 0.8, scaled by mirror-group size)
3. Commit-couple >= 3       → CO_CHANGED  (weight min(1, count/10))

Rules deliberately not here
---------------------------
4. Embedding cosine >= 0.88 → *suggested* edge. Needs vectors, so it belongs
   to `meristem embed`, not to ingest.
5. LLM extraction → needs a model call; SPEC §1 keeps LLMs out of the hot path.
6. `git blame` → OWNS. Lands with the owner-atom ingester, which must create
   the `owner` atoms an OWNS edge points at.

Bounding
--------
Every rule is capped. Edge discovery over N atoms sharing one hub path is
naturally quadratic, so each rule has a per-group cap and the pass as a whole
honours `max_edges`. A repo cannot turn one popular file into an edge
explosion that swamps PPR.
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from .edges import upsert_edge

# --- Rule 1: REFERENCES -----------------------------------------------------
REFERENCES_WEIGHT = 0.6
# Atoms declared at one path that a single mentioning atom may link to. A README
# naming `auth.py` should reach that file's symbols, not 400 of them.
MAX_REFS_PER_MENTION = 8

# --- Rule 2: MIRRORS --------------------------------------------------------
# Ceiling weight, at the tightest possible group (exactly 2 repos share the
# name). Unlike CO_CHANGED's weight (min(1, count/10), derived from real commit
# counts) or the retrieval relevance floor (calibrate.py, derived from real
# queries), there is no equivalent workspace-local signal to derive a MIRRORS
# *threshold* from — "is this name a genuine cross-app concept or a
# coincidence" has no ground truth this codebase can measure without a labeled
# corpus. What IS measurable locally, and load-bearing here, is group size:
# `by_symbol[name]` growing from 2 toward MAX_MIRROR_GROUP is itself the same
# "how generic is this" signal the hard cutoff already uses, so weight is
# graduated across that range instead of applied flat — see `_mirror_weight`.
# A name shared by exactly 2 repos was, until this fix, stored at the same
# weight-0.8 confidence as one shared by 7, despite the latter being much
# closer to the MAX_MIRROR_GROUP cutoff that exists specifically because that
# many occurrences reads as generic infrastructure, not a deliberate mirror.
MIRRORS_WEIGHT = 0.8
# Weight at the loosest still-accepted group (MAX_MIRROR_GROUP). Not 0: a
# group under the cap is still real evidence, just the weakest of it — a floor
# near REFERENCES_WEIGHT's neighborhood keeps it a meaningful signal rather
# than a rounding error PPR ignores.
MIRRORS_MIN_WEIGHT = 0.4
# Symbol names below this length ("run", "get", "id") collide across unrelated
# repos and would wire the graph into a hairball rather than a signal.
MIN_SYMBOL_LEN = 4
# A name appearing in more than this many atoms is generic infrastructure, not a
# cross-app mirror of one concept.
MAX_MIRROR_GROUP = 8
# Names that clear the length bar but are still ubiquitous.
_SYMBOL_STOPLIST = frozenset({
    "main", "init", "setup", "build", "index", "test", "tests", "config",
    "handler", "handlers", "router", "routes", "server", "client", "utils",
    "helper", "helpers", "model", "models", "schema", "types", "constants",
    "__init__", "run", "start", "stop", "create", "update", "delete", "list",
})


def _mirror_weight(group_size: int) -> float:
    """Linear ramp from `MIRRORS_WEIGHT` (group_size 2) down to
    `MIRRORS_MIN_WEIGHT` (group_size MAX_MIRROR_GROUP). Group size is already
    the signal `MAX_MIRROR_GROUP` uses to reject a name outright past the cap;
    this applies the same signal gradedly to everything under it instead of
    treating group sizes 2 and 7 as equally confident.
    """
    span = MAX_MIRROR_GROUP - 2
    if span <= 0 or group_size <= 2:
        return MIRRORS_WEIGHT
    frac = min(1.0, (group_size - 2) / span)
    return round(MIRRORS_WEIGHT - frac * (MIRRORS_WEIGHT - MIRRORS_MIN_WEIGHT), 4)

# --- Rule 3: CO_CHANGED -----------------------------------------------------
CO_CHANGE_MIN = 3          # SPEC §4: "edited in same commit >= 3 times"
CO_CHANGE_LOOKBACK = 500   # commits mined
# A commit touching more files than this is a merge, a rename sweep or a
# formatter run. Its file pairs are noise and it alone would emit O(n^2) edges.
CO_CHANGE_MAX_FILES = 25
MAX_CO_CHANGE_PAIRS = 400  # highest-count file pairs kept
MAX_ATOMS_PER_FILE = 6     # atoms linked per side of a coupled file pair

# --- Global ----------------------------------------------------------------
MAX_EDGES_DEFAULT = 5000

# Filename-ish tokens inside summary text: `src/auth.py`, `schema.sql`, `App.tsx`.
_PATH_TOKEN = re.compile(r"[\w./\\-]*\w\.\w{1,8}\b")
_REC_SEP = "\x1e"


@dataclass
class DiscoveryResult:
    """Per-rule edge counts, surfaced in the ingest table and by `meristem doctor`."""

    references: int = 0
    mirrors: int = 0
    co_changed: int = 0
    notes: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def total(self) -> int:
        return self.references + self.mirrors + self.co_changed


@dataclass(frozen=True)
class _AtomRef:
    """The projection of an atom that edge discovery actually needs."""

    id: str
    path: str | None       # repo-relative path the atom pertains to
    repo_id: str | None
    symbol: str | None     # symbol name, for symbol atoms
    text: str              # concatenated summaries, for mention scanning


def atom_path(source_ref: str | None, source_kind: str) -> str | None:
    """The repo-relative path an atom pertains to, or None.

    Ingesters record paths in `source_ref` two ways: bare (`README.md`, from
    readme/manifest/schema) and path-with-line (`src/auth.py:0`, from the
    symbols adapter). Commit atoms carry a SHA there instead, which is not a
    path — hence the `source_kind` guard rather than a pure string heuristic.
    """
    if not source_ref or source_kind == "commit":
        return None
    head, sep, tail = source_ref.rpartition(":")
    if sep and tail.isdigit() and head:
        return head
    return source_ref or None


def _symbol_name(topic_key: str) -> str | None:
    """Symbol name out of a `symbol:<relpath>:<name>` topic key.

    `rpartition` (not `split`) so a path containing a colon still yields the
    trailing name rather than a fragment of the path.
    """
    if not topic_key.startswith("symbol:"):
        return None
    _, sep, name = topic_key.rpartition(":")
    return name if sep and name else None


def _load_atoms(conn, *, workspace_id: str | None) -> list[_AtomRef]:
    """Live atoms plus their summary text, as the projection the rules need."""
    sql = """
        SELECT a.id, a.source_ref, a.source_kind, a.repo_id, a.topic_key,
               COALESCE(GROUP_CONCAT(s.text, ' '), '') AS text
          FROM live_atoms a
          LEFT JOIN atom_summaries s ON s.atom_id = a.id
    """
    params: list[object] = []
    if workspace_id is not None:
        sql += " WHERE a.workspace_id = ?"
        params.append(workspace_id)
    sql += " GROUP BY a.id ORDER BY a.id"
    out: list[_AtomRef] = []
    for r in conn.execute(sql, params):
        out.append(
            _AtomRef(
                id=r["id"],
                path=atom_path(r["source_ref"], r["source_kind"]),
                repo_id=r["repo_id"],
                symbol=_symbol_name(r["topic_key"]),
                text=r["text"] or "",
            )
        )
    return out


class _EdgeBudget:
    """Shared cap so one rule cannot consume the whole pass's edge allowance."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.spent = 0

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit

    def charge(self) -> None:
        self.spent += 1


def _emit(conn, budget: _EdgeBudget, **kw) -> bool:
    """Create one edge. Returns True when a call actually landed.

    Edge creation is advisory: a missing endpoint or a self-edge must not abort
    the discovery pass over the remaining rules, so endpoint errors are
    swallowed the same way `assert_fact` treats its CONTRADICTS edges.
    """
    if budget.exhausted:
        return False
    try:
        upsert_edge(conn, **kw)
    except (KeyError, ValueError):
        return False
    budget.charge()
    return True


# ---------------------------------------------------------------------------
# Rule 1 — shared file refs → REFERENCES (weight 0.6)
# ---------------------------------------------------------------------------

def _rule_references(conn, atoms: list[_AtomRef], budget: _EdgeBudget) -> int:
    """Link an atom to the atoms declared at each path its summary names.

    The valuable direction is *cross-file*: a README atom that mentions
    `src/auth.py` should reach that file's symbol atoms, because PPR can then
    flow query mass from prose into code. Atoms that merely share a declaring
    file are already tied together through their module's ROLLS_UP edges, so
    linking those siblings again would add quadratic edge mass and no reach.
    """
    by_path: dict[str, list[str]] = defaultdict(list)
    # Keyed on (repo_id, basename), NOT basename alone: a doc in one root
    # mentioning "config.py" must not resolve to every same-named atom across
    # every OTHER root in a multi-repo workspace — a real false-positive risk
    # in exactly the monorepo topology this rule is supposed to serve. Full
    # paths are already workspace-root-relative (see IngestContext.rel_path),
    # so `by_path` doesn't need the same scoping: two roots sharing an
    # internal layout still produce distinct full paths.
    by_basename: dict[tuple[str | None, str], list[str]] = defaultdict(list)
    for a in atoms:
        if a.path:
            by_path[a.path].append(a.id)
            by_basename[(a.repo_id, Path(a.path).name)].append(a.id)

    created = 0
    for a in atoms:
        if budget.exhausted:
            break
        if not a.text:
            continue
        linked: set[str] = set()
        for token in set(_PATH_TOKEN.findall(a.text)):
            # Full-path match first; fall back to same-repo basename so
            # `auth.py` still resolves when the summary omits the directory.
            targets = by_path.get(token) or by_basename.get((a.repo_id, Path(token).name)) or []
            for target_id in targets[:MAX_REFS_PER_MENTION]:
                if target_id == a.id or target_id in linked:
                    continue
                # Skip atoms declared at the path they mention — that is a
                # self-description, not a reference to somewhere else.
                if a.path and target_id in by_path.get(a.path, ()):
                    continue
                if _emit(
                    conn, budget,
                    src_id=a.id, dst_id=target_id, kind="REFERENCES",
                    source="shared_ref", weight=REFERENCES_WEIGHT,
                    evidence=[("file_path", token)],
                ):
                    linked.add(target_id)
                    created += 1
    return created


# ---------------------------------------------------------------------------
# Rule 2 — same symbol across repos → MIRRORS (weight <= 0.8, by group size)
# ---------------------------------------------------------------------------

def _rule_mirrors(conn, atoms: list[_AtomRef], budget: _EdgeBudget) -> int:
    """Connect same-named symbols that live in *different* repos.

    This is the cross-app schema-coupling signal from SPEC §4: service A and
    service B both naming `session_token` is exactly the connection a k-NN
    search over one repo's embeddings cannot see. Within a single repo the same
    name is usually just a re-export, so same-repo pairs are skipped.

    Every pair within a symbol's group gets the SAME weight (`_mirror_weight`
    of the group's total size), not one scaled per-pair — the evidence being
    graded is "how common is this name across the workspace", which is a
    property of the group, not of any one pair within it.
    """
    by_symbol: dict[str, list[_AtomRef]] = defaultdict(list)
    for a in atoms:
        if not a.symbol or len(a.symbol) < MIN_SYMBOL_LEN:
            continue
        if a.symbol.lower() in _SYMBOL_STOPLIST:
            continue
        by_symbol[a.symbol].append(a)

    created = 0
    for symbol, group in sorted(by_symbol.items()):
        if budget.exhausted:
            break
        if len(group) < 2 or len(group) > MAX_MIRROR_GROUP:
            continue
        if len({a.repo_id for a in group}) < 2:
            continue  # single-repo name collision, not a cross-app mirror
        weight = _mirror_weight(len(group))
        for left, right in combinations(group, 2):
            if left.repo_id == right.repo_id:
                continue
            if _emit(
                conn, budget,
                src_id=left.id, dst_id=right.id, kind="MIRRORS",
                source="shared_ref", weight=weight,
                evidence=[("symbol", symbol)],
            ):
                created += 1
    return created


# ---------------------------------------------------------------------------
# Rule 3 — commit-couple >= 3 → CO_CHANGED (weight min(1, count/10))
# ---------------------------------------------------------------------------

def _git_log_files(root: Path, *, lookback: int) -> list[list[str]]:
    """File lists per commit, newest first. Empty when `root` is not a repo."""
    try:
        out = subprocess.run(
            ["git", "log", "-n", str(lookback), "--name-only", "--no-merges",
             f"--pretty=format:{_REC_SEP}"],
            cwd=root, check=True, capture_output=True, text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []
    commits: list[list[str]] = []
    for chunk in out.split(_REC_SEP):
        files = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if files:
            commits.append(files)
    return commits


def co_change_pairs(
    root: Path, *, lookback: int = CO_CHANGE_LOOKBACK
) -> dict[tuple[str, str], int]:
    """Count how often each file pair changed in the same commit.

    Wide commits are dropped: a 300-file formatter run would contribute ~45k
    pairs of pure noise and drown the genuine couplings this rule exists to
    find. Pair keys are sorted so the count is direction-free.
    """
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for files in _git_log_files(root, lookback=lookback):
        if len(files) < 2 or len(files) > CO_CHANGE_MAX_FILES:
            continue
        for pair in combinations(sorted(set(files)), 2):
            counts[pair] += 1
    return dict(counts)


def _rule_co_changed(
    conn, atoms: list[_AtomRef], root: Path, budget: _EdgeBudget
) -> tuple[int, list[str]]:
    """Link atoms of files that repeatedly change together.

    CO_CHANGED is the one edge kind that encodes a coupling nobody wrote down —
    it comes from what the team actually did, not from what the code declares.
    """
    notes: list[str] = []
    by_path: dict[str, list[str]] = defaultdict(list)
    for a in atoms:
        if a.path:
            by_path[a.path].append(a.id)
    if not by_path:
        return 0, notes

    pairs = co_change_pairs(root)
    coupled = sorted(
        ((p, c) for p, c in pairs.items() if c >= CO_CHANGE_MIN),
        key=lambda t: (-t[1], t[0]),
    )
    if not coupled:
        notes.append(f"no file pair co-changed >= {CO_CHANGE_MIN} times")
        return 0, notes
    if len(coupled) > MAX_CO_CHANGE_PAIRS:
        notes.append(f"co-change pairs capped at {MAX_CO_CHANGE_PAIRS} of {len(coupled)}")
        coupled = coupled[:MAX_CO_CHANGE_PAIRS]

    created = 0
    for (left_path, right_path), count in coupled:
        if budget.exhausted:
            break
        left_ids = by_path.get(left_path, [])[:MAX_ATOMS_PER_FILE]
        right_ids = by_path.get(right_path, [])[:MAX_ATOMS_PER_FILE]
        if not left_ids or not right_ids:
            continue  # one side has no atoms — nothing to connect
        weight = min(1.0, count / 10.0)
        for left_id in left_ids:
            for right_id in right_ids:
                if left_id == right_id:
                    continue
                if _emit(
                    conn, budget,
                    src_id=left_id, dst_id=right_id, kind="CO_CHANGED",
                    source="commit_couple", weight=weight,
                    evidence=[("file_path", f"{left_path} + {right_path} x{count}")],
                ):
                    created += 1
    return created, notes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def discover_edges(
    conn,
    *,
    root: Path,
    workspace_id: str | None = "default",
    max_edges: int = MAX_EDGES_DEFAULT,
    rules: set[str] | None = None,
) -> DiscoveryResult:
    """Derive typed edges over the atoms already in the store (SPEC §4).

    Idempotent: `upsert_edge` refreshes an existing live edge rather than
    duplicating it, so re-running after an incremental ingest is safe and
    converges instead of accumulating.

    `rules` selects a subset by name (`references`, `mirrors`, `co_changed`);
    None runs all of them.
    """
    res = DiscoveryResult()
    budget = _EdgeBudget(max_edges)
    wanted = rules or {"references", "mirrors", "co_changed"}

    atoms = _load_atoms(conn, workspace_id=workspace_id)
    if not atoms:
        res.notes.append("no live atoms to connect")
        return res

    if "references" in wanted:
        res.references = _rule_references(conn, atoms, budget)
    if "mirrors" in wanted:
        res.mirrors = _rule_mirrors(conn, atoms, budget)
    if "co_changed" in wanted:
        created, notes = _rule_co_changed(conn, atoms, root, budget)
        res.co_changed = created
        res.notes.extend(notes)

    if budget.exhausted:
        res.truncated = True
        res.notes.append(f"edge budget of {max_edges} exhausted; some edges skipped")
    return res


__all__ = ["DiscoveryResult", "atom_path", "co_change_pairs", "discover_edges"]
