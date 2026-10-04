"""Health-check command — `meristem doctor`.

Composes existing helpers to produce a one-screen health report. Read-only:
opens the db, runs a fixed set of integrity + freshness queries, never writes.

Severity ladder (one finding per check):
  ok       — green ✓, nothing to do
  warn     — yellow ⚠, surface but don't fail
  fail     — red ✗, exit code = count of fails

The goal is to surface silent rot — schema drift, embedding-model drift,
liveness coverage erosion, dead retrieval pipeline — before they turn into
mystery hallucinations during a real tick.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import config, git_utils, liveness, store, treesitter
from .paths import Layout, legacy_state_present

Severity = Literal["ok", "warn", "fail"]

EXPECTED_SCHEMA_VERSION = 7  # v7: hook_heartbeat — proof a Claude Code hook process ran
# How long a hook_heartbeat row counts as "recent" before `hooks.heartbeat`
# stops calling it current. Matches `retrieval.activity`'s window — both ask
# "is the ambient layer actually running", just at different layers.
HOOK_HEARTBEAT_RECENT_SECONDS = 7 * 86_400
# Commits behind HEAD before drift stops being a nudge and becomes a failure.
# Below this the store is merely a little behind; above it, whole subsystems can
# have been added, renamed or deleted since the index was built, so retrieval is
# describing a repo that no longer exists.
DRIFT_FAIL_COMMITS = 20
RETRIEVAL_IDLE_WARN_SECONDS = 7 * 86_400        # no routed queries in 7d
LIVENESS_STALE_THRESHOLD_SECONDS = 24 * 3_600   # matches statusline default
# Above this many graph-eligible atoms, an edgeless store is a failure rather
# than a nudge — a workspace this size has had every chance to build a graph.
# "Graph-eligible" excludes source_kind='commit': commit atoms are a log of
# what happened, never linked by any ingester (SPEC §4), so a workspace made
# entirely of commit history (e.g. a non-code assignment archive) is
# correctly edgeless forever, not broken. Counting them toward this threshold
# was a real false positive — a 53-atom, all-commit repo failed doctor for
# having "no graph" when there was nothing ingestible to graph in the first
# place. Symbol/module/owner/etc. atoms — the kinds ingesters actually link —
# still fail this check exactly as before.
EDGELESS_FAIL_ATOM_THRESHOLD = 20
# Queries that returned nothing, as a share of recent queries, before we call
# retrieval broken rather than merely quiet.
EMPTY_RESULT_FAIL_RATIO = 0.8


@dataclass(frozen=True)
class Finding:
    name: str
    severity: Severity
    summary: str          # one-line, ≤80 chars
    detail: str | None = None  # multi-line, shown under -v


@dataclass(frozen=True)
class Report:
    findings: list[Finding]

    @property
    def fail_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "fail")

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warn")


def build_checks(
    conn: sqlite3.Connection,
    layout: Layout,
    findings: list[Finding],
    *,
    quick: bool = False,
) -> list[tuple[str, Callable[[], None]]]:
    """The ordered (name, check) registry `run` executes.

    Split out of `run` so the check *names* are introspectable without needing
    a populated store. Several checks return early without emitting a finding
    when their subject is absent — `retrieval.quality` says nothing when no
    query has ever been logged — so collecting names by running `doctor`
    against a fresh workspace silently under-reports the surface. Anything
    asserting on the full set of checks (see `tests/test_surface.py`) has to
    read the registry itself.
    """
    return [
        ("schema.version", lambda: _check_schema(conn, findings)),
        *([] if quick else [("db.integrity", lambda: _check_integrity(conn, findings))]),
        ("db.journal", lambda: _check_journal_mode(conn, findings)),
        ("atoms.census", lambda: _check_atom_census(conn, findings)),
        ("liveness.coverage", lambda: _check_liveness_coverage(conn, findings)),
        ("liveness.freshness", lambda: _check_liveness_freshness(conn, findings)),
        ("liveness.unverifiable", lambda: _check_liveness_unverifiable(conn, findings)),
        ("embeddings.drift", lambda: _check_embedding_drift(conn, findings)),
        ("embeddings.model_match", lambda: _check_embedding_model_match(conn, findings)),
        ("edges.density", lambda: _check_edge_density(conn, findings)),
        ("ingest.completed", lambda: _check_ingest_completed(conn, findings)),
        ("ingest.notes", lambda: _check_ingest_notes(conn, findings)),
        ("retrieval.quality", lambda: _check_retrieval_quality(conn, findings)),
        ("retrieval.calibration", lambda: _check_relevance_calibration(conn, findings)),
        ("capture.queue", lambda: _check_capture_queue(conn, findings)),
        ("capture.unconfirmed", lambda: _check_unconfirmed(conn, findings)),
        ("guard.store", lambda: _check_guard_store(conn, layout, findings)),
        ("retrieval.activity", lambda: _check_retrieval_activity(conn, findings)),
        ("hooks.heartbeat", lambda: _check_hooks_heartbeat(conn, findings)),
        ("repo.freshness", lambda: _check_repo_freshness(conn, findings)),
        ("hooks.git", lambda: _check_git_hooks(layout, findings)),
        ("handoff.dir", lambda: _check_handoff_dir(layout, findings)),
    ]


def run(layout: Layout, *, quick: bool = False) -> Report:
    """Run every check against `layout`. Each check is independent; one failing
    check never short-circuits the others — operators need the full picture.

    `quick` skips whole-database scans (`PRAGMA integrity_check`) so callers on
    a latency budget — the SessionStart digest, the statusline pulse — can ask
    for health on every invocation without paying a full page walk. Everything
    else is an indexed count and stays cheap regardless of store size.
    """
    findings: list[Finding] = []

    # Layout presence is the precondition for every db-touching check.
    layout_ok = _check_layout(layout, findings)
    if not layout_ok:
        return Report(findings)

    # All db checks share one connection (read-only intent, but sqlite3 in
    # Python doesn't expose read-only mode without a URI; we just never write).
    try:
        conn = store.connect(layout.db)
    except sqlite3.Error as e:
        findings.append(Finding(
            name="db.open",
            severity="fail",
            summary=f"cannot open atoms.sqlite: {e}",
        ))
        return Report(findings)

    checks = build_checks(conn, layout, findings, quick=quick)
    try:
        for name, fn in checks:
            try:
                fn()
            except sqlite3.OperationalError as e:
                # A check that could not RUN is not a check that passed. This
                # sat at `warn` and hid the single most consequential defect in
                # the system: a real workspace had a `live_edges` view left
                # dangling by the v2 edges migration, so `edges.density` — the
                # one check that reports a missing graph — threw every time and
                # was filed as a yellow "skipped" nobody read. Same swallowing
                # pattern as treating an unrunnable liveness predicate as a
                # pass, and the same fix: say you could not tell.
                findings.append(Finding(
                    name=name, severity="fail",
                    summary=f"check COULD NOT RUN: {e}",
                    detail=(
                        "This is not a pass — the check errored, so its subject is "
                        "unverified. A dangling view is the usual cause; `meristem migrate` "
                        "repairs those."
                    ),
                ))
            except Exception as e:  # noqa: BLE001 — one bad check must not kill the report
                findings.append(Finding(
                    name=name, severity="fail",
                    summary=f"check crashed: {type(e).__name__}: {e}",
                ))
    finally:
        conn.close()

    return Report(findings)


# ---------------------------------------------------------------------------
# individual checks — each appends 0 or 1 finding
# ---------------------------------------------------------------------------

def _check_layout(layout: Layout, findings: list[Finding]) -> bool:
    if not layout.state_dir.exists():
        if legacy_state_present(layout):
            findings.append(Finding(
                name="layout.state_dir",
                severity="fail",
                summary=(
                    f"found an old .dlms/ dir (renamed from DLMS to Meristem) — "
                    f"run `meristem init` to build {layout.state_dir}"
                ),
            ))
        else:
            findings.append(Finding(
                name="layout.state_dir",
                severity="fail",
                summary=f"missing {layout.state_dir} — run `meristem init`",
            ))
        return False
    if not layout.db.exists():
        findings.append(Finding(
            name="layout.db",
            severity="fail",
            summary=f"missing {layout.db} — run `meristem init`",
        ))
        return False
    findings.append(Finding(
        name="layout",
        severity="ok",
        summary=f"workspace {layout.root}",
    ))
    return True


def _check_schema(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    v = store.schema_version(conn)
    if v == EXPECTED_SCHEMA_VERSION:
        findings.append(Finding(
            name="schema.version",
            severity="ok",
            summary=f"v{v}",
        ))
    elif v < EXPECTED_SCHEMA_VERSION:
        # warn (not fail): doctor is the tool you reach for when something is
        # wrong — bumping exit code here discourages exactly the diagnosis run
        # the user needs. Later checks may surface table-missing warnings as
        # the schema gap manifests.
        findings.append(Finding(
            name="schema.version",
            severity="warn",
            summary=f"db at v{v}, expected v{EXPECTED_SCHEMA_VERSION} — run `meristem migrate`",
        ))
    else:
        # v > expected: db newer than installed CLI. Don't auto-downgrade.
        findings.append(Finding(
            name="schema.version",
            severity="warn",
            summary=f"db at v{v}, CLI expects v{EXPECTED_SCHEMA_VERSION} — CLI may be out of date",
        ))


def _check_integrity(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    msg = row[0] if row else "no result"
    if msg == "ok":
        findings.append(Finding(name="db.integrity", severity="ok", summary="integrity_check ok"))
    else:
        findings.append(Finding(
            name="db.integrity",
            severity="fail",
            summary="sqlite integrity_check failed",
            detail=msg,
        ))


def _check_journal_mode(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    row = conn.execute("PRAGMA journal_mode").fetchone()
    mode = (row[0] if row else "").lower()
    if mode == "wal":
        findings.append(Finding(name="db.journal", severity="ok", summary="WAL active"))
    else:
        # schema.sql asks for WAL; if we got something else the pragma may have
        # been overridden by a tool. Not fatal, but worth surfacing.
        findings.append(Finding(
            name="db.journal",
            severity="warn",
            summary=f"journal_mode={mode!r}, expected wal — concurrent writers may block",
        ))


def _check_atom_census(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    total = store.atom_count(conn)
    if total == 0:
        findings.append(Finding(
            name="atoms.census",
            severity="warn",
            summary="0 live atoms — run `meristem ingest` or start asserting facts",
        ))
        return
    by_type = dict(conn.execute(
        "SELECT type, COUNT(*) c FROM live_atoms GROUP BY type ORDER BY c DESC"
    ).fetchall())
    pinned = conn.execute("SELECT COUNT(*) FROM live_atoms WHERE pinned = 1").fetchone()[0]
    detail = ", ".join(f"{t}:{n}" for t, n in by_type.items())
    findings.append(Finding(
        name="atoms.census",
        severity="ok",
        summary=f"{total} live atoms ({pinned} pinned)",
        detail=detail,
    ))


def _check_liveness_coverage(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    total = store.atom_count(conn)
    if total == 0:
        return  # already flagged by census
    with_kind = conn.execute(
        "SELECT COUNT(*) FROM live_atoms WHERE liveness_kind IS NOT NULL "
        "AND liveness_kind != 'none'"
    ).fetchone()[0]
    pct = (with_kind / total) * 100
    summary = f"{with_kind}/{total} atoms carry a liveness predicate ({pct:.0f}%)"
    if pct >= 60:
        sev: Severity = "ok"
    elif pct >= 30:
        sev = "warn"
    else:
        sev = "warn"  # not fatal — some atom types (decision, owner) legitimately lack predicates
    findings.append(Finding(name="liveness.coverage", severity=sev, summary=summary))


def _check_liveness_unverifiable(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    """Atoms whose predicate cannot currently be run.

    `regex` and `sql` always have a runner. `ast` (SPEC §16) does too, but
    whether it can actually execute for a given atom depends on which
    tree-sitter grammar packages are importable right now — a per-atom,
    per-environment gap, not a blanket per-kind one, so this walks live `ast`
    atoms and checks each one's language rather than a static kind list
    (`liveness.UNRUNNABLE_KINDS`, kept for any future kind that ships with no
    runner at all — currently empty).

    Surfaced as its own line because these atoms are shown to callers while
    being unverified. Folding them into freshness would hide the coverage gap;
    dropping them would discard knowledge. They get counted, out loud.
    """
    n = 0
    missing_langs: set[str] = set()

    if liveness.UNRUNNABLE_KINDS:
        kinds = ",".join(f"'{k}'" for k in sorted(liveness.UNRUNNABLE_KINDS))
        n += conn.execute(
            f"SELECT COUNT(*) FROM live_atoms WHERE liveness_kind IN ({kinds})"
        ).fetchone()[0]

    for row in conn.execute(
        "SELECT liveness_target FROM live_atoms WHERE liveness_kind = 'ast'"
    ):
        target = row["liveness_target"]
        lang_key = treesitter.language_for_suffix(Path(target).suffix) if target else None
        if lang_key is None or not treesitter.is_available(lang_key):
            n += 1
            missing_langs.add(lang_key or "unknown")

    if n == 0:
        return
    detail = "These are surfaced marked `unverifiable`, never as verified. "
    if missing_langs:
        detail += (
            f"Missing tree-sitter grammar for: {', '.join(sorted(missing_langs))}. "
            "Reinstalling Meristem's dependencies (`uv sync` / `pip install -e .`) "
            "picks up the missing grammar package(s) and clears this."
        )
    findings.append(Finding(
        name="liveness.unverifiable",
        severity="warn",
        summary=f"{n} atom(s) carry a predicate with no runner available — shown but unverified",
        detail=detail,
    ))


def _check_liveness_freshness(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    cutoff = int(time.time()) - LIVENESS_STALE_THRESHOLD_SECONDS
    total_with_kind = conn.execute(
        "SELECT COUNT(*) FROM live_atoms "
        " WHERE liveness_kind IS NOT NULL AND liveness_kind != 'none'"
    ).fetchone()[0]
    # "Never checked" is not automatically "gone stale". Every atom starts with
    # liveness_last_ok = NULL, so treating NULL as stale made a brand-new
    # workspace open with a red 100%-stale FAIL the moment `meristem ingest`
    # finished — noise that trains people to ignore the report meant to catch
    # real rot. But a predicate that has gone unrun since well *before* the
    # staleness window is a genuine problem: the liveness machinery isn't
    # running at all. `asserted_at` separates the two honestly.
    stale = conn.execute(
        "SELECT COUNT(*) FROM live_atoms "
        " WHERE liveness_kind IS NOT NULL AND liveness_kind != 'none' "
        "   AND CASE WHEN liveness_last_ok IS NULL THEN asserted_at "
        "            ELSE liveness_last_ok END < ?",
        (cutoff,),
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM live_atoms "
        " WHERE liveness_kind IS NOT NULL AND liveness_kind != 'none' "
        "   AND liveness_last_ok IS NULL AND asserted_at >= ?",
        (cutoff,),
    ).fetchone()[0]
    if stale == 0 and pending:
        findings.append(Finding(
            name="liveness.freshness",
            severity="warn",
            summary=(
                f"{pending}/{total_with_kind} predicates not run yet — "
                f"run `meristem watch --all`"
            ),
            detail="Not staleness: these atoms were ingested recently and have never been checked.",
        ))
        return
    if stale == 0:
        findings.append(Finding(
            name="liveness.freshness",
            severity="ok",
            summary="no stale atoms (all predicates passed in last 24h)",
        ))
        return
    # Use a relative threshold so the boundary scales with workspace size.
    # Absolute floor (5) prevents nuisance fails on tiny workspaces.
    pct_stale = (stale / total_with_kind * 100) if total_with_kind else 0
    if pct_stale >= 20 and stale >= 5:
        sev: Severity = "fail"
    else:
        sev = "warn"
    findings.append(Finding(
        name="liveness.freshness",
        severity=sev,
        summary=(
            f"{stale}/{total_with_kind} predicates stale "
            f"({pct_stale:.0f}%) — last_ok > 24h"
        ),
        detail="Run `meristem watch --all` or investigate via `meristem query`.",
    ))


def _check_embedding_drift(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    # Live atoms only. Superseded atoms keep their old embedding rows, so a
    # store that has been re-embedded onto a new model still holds stragglers
    # under the previous one — never compared against, since every retrieval
    # path joins live_atoms, but enough to report a split that affects nothing.
    rows = conn.execute(
        """SELECT e.model AS model, COUNT(*) c
             FROM atom_embeddings e
             JOIN live_atoms a ON a.id = e.atom_id
            GROUP BY e.model"""
    ).fetchall()
    if not rows:
        findings.append(Finding(
            name="embeddings.drift",
            severity="ok",
            summary="no embeddings written yet",
        ))
        return
    if len(rows) == 1:
        m, c = rows[0]
        findings.append(Finding(
            name="embeddings.drift",
            severity="ok",
            summary=f"{c} embeddings, single model: {m}",
        ))
    else:
        models = ", ".join(f"{r[0]}({r[1]})" for r in rows)
        findings.append(Finding(
            name="embeddings.drift",
            severity="fail",
            summary=f"{len(rows)} embedding models present — retrieval will be inconsistent",
            detail=models,
        ))


def _check_embedding_model_match(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    """Do the stored vectors come from the embedder this install would use?

    `default_embedder()` returns bge-small-en-v1.5 when the `embed` extra is
    present and hash-3gram-v1 when it isn't — so the *same workspace* answers
    differently depending on which install touches it. Both emit 384 dims, so a
    dimension check never fires; the cosine just quietly ranks noise.

    Retrieval now filters to matching vectors, which turns the failure from
    "confident nonsense" into "no results". This check is what explains that.
    """
    from . import embeddings

    stored = embeddings.stored_models(conn)
    if not stored:
        return  # embeddings.drift already covers the no-embeddings case
    current = getattr(embeddings.default_embedder(), "name", None)
    if current is None or current in stored:
        return
    listed = ", ".join(f"{m} ({n})" for m, n in sorted(stored.items()))
    findings.append(Finding(
        name="embeddings.model_match",
        severity="fail",
        summary=f"stored embeddings are {listed}, but this install queries with {current}",
        detail=(
            "Vectors from different models are not comparable, so embedding-seeded "
            "retrieval returns nothing here. Run `meristem embed` to re-embed onto the "
            "current model, or install the matching extra so this install uses the "
            "model already stored (`pip install 'meristem[embed]'` for bge-small-en-v1.5)."
        ),
    ))


def _check_edge_density(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    total = conn.execute("SELECT COUNT(*) FROM live_edges").fetchone()[0]
    if total == 0:
        atom_n = store.atom_count(conn)
        if atom_n == 0:
            return  # census already noted no atoms
        # A populated store of atoms ingesters actually link (symbols, modules,
        # owners, ...) with no edges is not a nudge — PPR has no graph to
        # propagate through, so retrieval has silently degraded to embedding
        # k-NN and every claim that distinguishes Meristem from a vector store
        # is false in practice. This check sat at `warn` (exit code 0) while
        # two real workspaces ran edgeless for months, which is precisely how
        # the problem went unheard. Small/new stores stay a warning.
        #
        # Commit atoms (source_kind='commit') don't count toward that verdict:
        # they're a log of what happened, never linked by any ingester by
        # design (git_commits.py — "a commit records what happened, it
        # forbids nothing"). A workspace of pure commit/history atoms — a
        # non-code repo with nothing else to ingest — is correctly edgeless
        # forever, not broken, no matter how many commits it has.
        graph_eligible_n = int(conn.execute(
            "SELECT COUNT(*) FROM live_atoms WHERE source_kind != 'commit'"
        ).fetchone()[0])
        if graph_eligible_n == 0:
            findings.append(Finding(
                name="edges.density",
                severity="warn",
                summary=(
                    f"0 edges across {atom_n} atoms — all commit/history atoms, "
                    "nothing to link"
                ),
                detail=(
                    "No symbol, module, or owner atoms exist to build edges from — this "
                    "workspace has no ingestible source content (or none has been ingested "
                    "yet), so an edgeless graph is expected, not a defect. If source files "
                    "are expected here, check that they match a supported language/suffix."
                ),
            ))
            return
        severe = graph_eligible_n >= EDGELESS_FAIL_ATOM_THRESHOLD
        findings.append(Finding(
            name="edges.density",
            severity="fail" if severe else "warn",
            summary=f"0 edges across {atom_n} atoms — retrieval is k-NN only, not a graph",
            detail=(
                "Run `meristem ingest` to build edges (SPEC §4 auto-discovery). If this "
                "persists, check that the `modules` adapter is enabled in meristem.toml — "
                "`meristem ingest --all` runs every registered adapter."
            ),
        ))
        return
    by_kind = dict(conn.execute(
        "SELECT kind, COUNT(*) c FROM live_edges GROUP BY kind ORDER BY c DESC"
    ).fetchall())
    detail = ", ".join(f"{k}:{n}" for k, n in by_kind.items())
    findings.append(Finding(
        name="edges.density",
        severity="ok",
        summary=f"{total} edges across {len(by_kind)} kind(s)",
        detail=detail,
    ))


def _check_ingest_completed(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    """Repos registered by `init` whose `ingest` never finished.

    `register_repo` writes the row; only a *complete* `ingest` calls
    `mark_indexed`. A row it never touched means the workspace was set up and
    then never populated — which is exactly the state one real workspace sat in
    while it served 8 empty queries. Nothing reported it, because nothing looked.

    Keyed on `last_indexed_at`, not `last_indexed_sha`: `mark_indexed` stores
    whatever `head_sha` returned, and that is NULL outside a git repo. Keying on
    the sha therefore told every non-git workspace it had "never ingested — the
    store is empty" forever, immediately after a successful ingest, with the
    atoms sitting right there in `atoms.census` on the line above. The timestamp
    is written by every completed ingest whether or not git is involved.
    """
    rows = conn.execute(
        "SELECT repo_id, root_path FROM repo_state WHERE last_indexed_at IS NULL"
    ).fetchall()
    if not rows:
        return
    paths = ", ".join(r["root_path"] for r in rows[:3])
    findings.append(Finding(
        name="ingest.completed",
        severity="fail",
        summary=f"{len(rows)} repo(s) registered but never ingested — the store is empty",
        detail=f"{paths}\nRun `meristem ingest` in the workspace root.",
    ))


def _check_ingest_notes(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    """Atoms an adapter had to skip on the last completed `ingest`, surfaced later.

    Gated on `last_atoms_skipped`, NOT on `last_ingest_notes` being non-empty.
    Most notes are routine status ("no manifests at root", "no new commits",
    per-directory owner attributions) — every adapter emits them on a totally
    healthy run, so warning on their mere presence would fire on nearly every
    real repo and train people to ignore this check exactly the way a
    warn-on-correct-behaviour check already did to `retrieval.quality` (see its
    docstring). `atoms_skipped` is paired specifically with a per-file failure
    (a swallowed TOMLDecodeError, an unreadable doc, a bad .sql file — see
    readme.py/schema_sql.py/invariants.py/manifest.py), which is the part
    `ingest`'s live printout is otherwise the only record of.

    `detail` still lists every note for that repo, not just the skip-flagged
    ones, since the skip note is usually what explains WHICH file failed.
    """
    rows = conn.execute(
        "SELECT repo_id, root_path, last_atoms_skipped, last_ingest_notes"
        "  FROM repo_state"
        " WHERE last_atoms_skipped > 0"
    ).fetchall()
    if not rows:
        return
    total_skipped = sum(r["last_atoms_skipped"] for r in rows)
    lines: list[str] = []
    for r in rows:
        lines.append(f"{r['root_path']}: {r['last_atoms_skipped']} atom(s) skipped")
        if r["last_ingest_notes"]:
            lines.extend(json.loads(r["last_ingest_notes"]))
    findings.append(Finding(
        name="ingest.notes",
        severity="warn",
        summary=(
            f"last ingest left {total_skipped} atom(s) skipped across {len(rows)} repo(s)"
        ),
        detail="\n".join(lines) + "\nRe-run `meristem ingest` to see full adapter output.",
    ))


def _check_retrieval_quality(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    """Queries that came back empty.

    Retrieval returning `[]` is indistinguishable, to the caller, from "nothing
    relevant exists" — so a substrate that is empty, unembedded or edgeless
    fails silently on every question asked of it. Counting empty results turns
    that silence into a signal.
    """
    total = conn.execute("SELECT COUNT(*) FROM retrieval_log").fetchone()[0]
    if total == 0:
        return  # retrieval.activity covers the never-queried case
    # A turn silenced by the relevance gate (§14.6) is empty ON PURPOSE: atoms
    # were retrieved and dropped for not matching. Counting those as failures
    # would make the health check punish the feature that fixed the substrate's
    # worst behaviour — and a check that fires on correct behaviour is a check
    # people learn to ignore, which is exactly how `edges.density` went unread.
    empty = conn.execute(
        "SELECT COUNT(*) FROM retrieval_log "
        " WHERE (atoms_returned IS NULL OR TRIM(atoms_returned) IN ('', '[]'))"
        "   AND COALESCE(n_suppressed, 0) = 0"
    ).fetchone()[0]
    gated = conn.execute(
        "SELECT COUNT(*) FROM retrieval_log WHERE COALESCE(n_suppressed, 0) > 0"
    ).fetchone()[0]
    if empty == 0:
        findings.append(Finding(
            name="retrieval.quality",
            severity="ok",
            summary=(
                f"all {total} logged queries returned atoms"
                if not gated
                else f"{total} logged queries, {gated} correctly silenced by the gate"
            ),
        ))
        return
    ratio = empty / total
    findings.append(Finding(
        name="retrieval.quality",
        severity="fail" if ratio >= EMPTY_RESULT_FAIL_RATIO else "warn",
        summary=f"{empty}/{total} queries returned nothing ({ratio * 100:.0f}%)",
        detail=(
            "An empty result is silent — the caller cannot tell 'nothing relevant' "
            "from 'substrate not built'. Check `meristem status`, then `meristem ingest` "
            "and `meristem embed`."
        ),
    ))


# Gate-outcome shares, over recent scored turns, that read as a miscalibrated
# floor. These are deliberately wide: the point is to catch a floor that is
# wrong by a lot and send you to `meristem calibrate`, not to second-guess a floor
# that was calibrated. A narrow band here would fire constantly and become the
# next check people learn to scroll past.
CALIBRATION_MIN_SCORED_TURNS = 20
CALIBRATION_SILENCE_WARN_RATIO = 0.7   # this share of turns silenced → too high
CALIBRATION_NEAR_MISS_MARGIN = 0.05    # silenced but this close to the floor
CALIBRATION_NEVER_GATES_RATIO = 0.02   # this few atoms ever dropped → too low


def _check_relevance_calibration(
    conn: sqlite3.Connection, findings: list[Finding]
) -> None:
    """Is `min_relevance` plausible for THIS workspace? (SPEC §14.6)

    The floor is embedder- and corpus-specific — 0.70 was derived from one model
    against one 607-atom store — but it ships as a constant, so every workspace
    inherits another workspace's calibration until someone re-derives it. Nobody
    ever did, because the tool to do it was a script you had to know about.

    This reads it off `retrieval_log`, which already records `top_relevance` and
    `min_relevance` per turn, so the check costs two indexed counts rather than
    a retrieval replay. It cannot recommend a number — that needs the probe set
    in `meristem calibrate` — but it can tell you the one you have is wrong, which
    is the part that was invisible.
    """
    row = conn.execute(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN COALESCE(n_suppressed, 0) > 0
                            AND (atoms_returned IS NULL
                                 OR TRIM(atoms_returned) IN ('', '[]'))
                           THEN 1 ELSE 0 END) AS silenced,
                  SUM(CASE WHEN COALESCE(n_suppressed, 0) > 0
                           THEN 1 ELSE 0 END) AS gated,
                  SUM(CASE WHEN COALESCE(n_suppressed, 0) > 0
                            AND (atoms_returned IS NULL
                                 OR TRIM(atoms_returned) IN ('', '[]'))
                            AND top_relevance >= min_relevance - ?
                           THEN 1 ELSE 0 END) AS near_miss
             FROM retrieval_log
            WHERE top_relevance IS NOT NULL AND min_relevance IS NOT NULL""",
        (CALIBRATION_NEAR_MISS_MARGIN,),
    ).fetchone()
    n = row["n"] or 0
    if n < CALIBRATION_MIN_SCORED_TURNS:
        # Pre-v4 turns carry NULL scores and must not be read as zeros, so a
        # store that predates the gate lands here and stays quiet.
        return
    silenced, gated, near_miss = row["silenced"] or 0, row["gated"] or 0, row["near_miss"] or 0
    floor = conn.execute(
        "SELECT min_relevance FROM retrieval_log"
        " WHERE min_relevance IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()["min_relevance"]

    silence_ratio = silenced / n
    if silence_ratio >= CALIBRATION_SILENCE_WARN_RATIO:
        findings.append(Finding(
            name="retrieval.calibration",
            severity="warn",
            summary=(
                f"{silenced}/{n} turns fully silenced at floor {floor:.2f} "
                f"({silence_ratio * 100:.0f}%) — floor may be too high"
            ),
            detail=(
                f"{near_miss} of those came within {CALIBRATION_NEAR_MISS_MARGIN} "
                f"of clearing it, which is the signature of a floor calibrated "
                f"for a different corpus rather than a store with nothing to say. "
                f"Run `meristem calibrate` to re-derive it from your own prompts."
            ),
        ))
        return
    if gated / n <= CALIBRATION_NEVER_GATES_RATIO:
        findings.append(Finding(
            name="retrieval.calibration",
            severity="warn",
            summary=(
                f"floor {floor:.2f} dropped nothing in {n} turns — "
                f"may be too low to gate"
            ),
            detail=(
                "A gate that never fires is indistinguishable from no gate. That "
                "is the pre-§14.6 behaviour the relevance floor exists to fix: "
                "every prompt answered with equal confidence, including the ones "
                "the store knew nothing about. Run `meristem calibrate`."
            ),
        ))
        return
    findings.append(Finding(
        name="retrieval.calibration",
        severity="ok",
        summary=(
            f"floor {floor:.2f} looks calibrated — {gated}/{n} turns gated, "
            f"{silenced} fully silenced"
        ),
    ))


CAPTURE_BACKLOG_WARN = 25


def _check_capture_queue(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    """Captured facts waiting on review (SPEC §18).

    A backlog is not a defect — it means capture is working — but an unbounded
    one means nobody is disposing, and unreviewed candidates are not memory. The
    warn threshold exists because a queue people stopped emptying is the same
    failure mode as a health check people stopped reading.
    """
    from . import candidates

    try:
        n = candidates.pending_count(conn)
    except sqlite3.Error as exc:
        findings.append(Finding(
            name="capture.queue",
            severity="fail",
            summary="COULD NOT RUN",
            detail=f"{exc} — run `meristem migrate` to apply schema v5.",
        ))
        return
    if n == 0:
        findings.append(Finding(
            name="capture.queue", severity="ok", summary="no facts pending review"
        ))
        return
    findings.append(Finding(
        name="capture.queue",
        severity="warn" if n >= CAPTURE_BACKLOG_WARN else "ok",
        summary=f"{n} captured fact(s) pending review",
        detail="Run `meristem review` — a candidate is not memory until accepted.",
    ))


GUARD_DETAIL_MAX = 20


def _check_guard_store(conn: sqlite3.Connection, layout: Layout, findings: list[Finding]) -> None:
    """Live atoms whose text trips the trust guard (secrets, personal data).

    Reports atom ids and finding kinds only — never the matched text. Such
    atoms are withheld from the shared export; they remain in the local store
    until archived or re-asserted without the value.
    """
    from . import config, guard

    try:
        policy = guard.policy_from_config(config.load(layout.config))
    except Exception:  # noqa: BLE001 — an unreadable config falls back to defaults
        policy = guard.Policy()
    if not policy.enabled:
        findings.append(Finding(
            name="guard.store", severity="ok", summary="trust guard disabled ([guard] enabled)"
        ))
        return
    texts: dict[str, list[str]] = {}
    for r in conn.execute("SELECT id, topic_key FROM live_atoms"):
        texts.setdefault(r["id"], []).append(r["topic_key"] or "")
    for r in conn.execute(
        "SELECT s.atom_id, s.text FROM atom_summaries s "
        "JOIN live_atoms a ON a.id = s.atom_id"
    ):
        texts.setdefault(r["atom_id"], []).append(r["text"] or "")
    hits: dict[str, list[str]] = {}
    for atom_id, parts in texts.items():
        ks = list(dict.fromkeys(k for t in parts for k in guard.kinds_in(t, policy=policy)))
        if ks:
            hits[atom_id] = ks
    if not hits:
        findings.append(Finding(
            name="guard.store", severity="ok", summary="no secrets or personal data in live atoms"
        ))
        return
    lines = [f"{aid}: {', '.join(ks)}" for aid, ks in sorted(hits.items())[:GUARD_DETAIL_MAX]]
    if len(hits) > GUARD_DETAIL_MAX:
        lines.append(f"... and {len(hits) - GUARD_DETAIL_MAX} more")
    findings.append(Finding(
        name="guard.store",
        severity="warn",
        summary=f"{len(hits)} live atom(s) hold secrets or personal data",
        detail=(
            "Withheld from the shared export. Archive them or re-assert without the value.\n"
            + "\n".join(lines)
        ),
    ))


def _check_unconfirmed(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    """Accepted facts past their reconfirmation horizon.

    These are the atoms no predicate can check — domain rules, team conventions.
    They do not rot visibly the way a regex against a moved file does, so the
    horizon is the only thing standing between "remembered" and "still true".
    """
    from . import candidates

    try:
        stale = candidates.unconfirmed_atoms(conn)
    except sqlite3.Error as exc:
        findings.append(Finding(
            name="capture.unconfirmed",
            severity="fail",
            summary="COULD NOT RUN",
            detail=f"{exc} — run `meristem migrate` to apply schema v5.",
        ))
        return
    if not stale:
        findings.append(Finding(
            name="capture.unconfirmed", severity="ok", summary="all confirmed facts current"
        ))
        return
    findings.append(Finding(
        name="capture.unconfirmed",
        severity="warn",
        summary=f"{len(stale)} fact(s) past their reconfirmation horizon",
        detail=(
            "They still surface, marked `unconfirmed`, and are no longer trusted "
            "silently. Re-assert or archive them."
        ),
    ))


def _check_retrieval_activity(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    cutoff = int(time.time()) - RETRIEVAL_IDLE_WARN_SECONDS
    recent = conn.execute(
        "SELECT COUNT(*) FROM retrieval_log WHERE ts >= ?",
        (cutoff,),
    ).fetchone()[0]
    if recent > 0:
        findings.append(Finding(
            name="retrieval.activity",
            severity="ok",
            summary=f"{recent} retrieval(s) logged in last 7 days",
        ))
    else:
        # Empty log can mean (a) router/hook isn't firing or (b) router doesn't
        # log yet. Either way, surface as a warning, not a fail.
        findings.append(Finding(
            name="retrieval.activity",
            severity="warn",
            summary=(
                "no retrievals logged in last 7 days — router may not be wired; "
                "check `meristem hooks status`"
            ),
        ))


def _check_hooks_heartbeat(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    """Did the Claude Code hooks actually fire in THIS workspace? (Roadmap
    Phase 0 #2 — fail-loud self-observability.)

    `retrieval.activity` asks whether *retrieval* happened; this asks the
    more fundamental question underneath it — did the hook process run at
    all. A hook wired into settings.json but pointed at a binary that no
    longer exists (the exact state the DLMS->Meristem rename left this
    project in for 8 days) produces zero evidence anywhere unless something
    records that it was even invoked, before deciding whether to speak.
    `meristem hook <event>` writes that record — `hook_heartbeat` here, on
    every invocation, success or not.
    """
    from . import hooks as hooks_mod

    try:
        rows = {
            r["event"]: dict(r)
            for r in conn.execute(
                "SELECT event, last_invoked_at, last_outcome, invocations FROM hook_heartbeat"
            )
        }
    except sqlite3.OperationalError:
        rows = {}

    now = time.time()
    missing: list[str] = []
    stale: list[str] = []
    for event in hooks_mod.HOOK_EVENTS:
        row = rows.get(event)
        ts = hooks_mod.parse_heartbeat_ts(row["last_invoked_at"]) if row else None
        if ts is None:
            missing.append(event)
        elif (now - ts) > HOOK_HEARTBEAT_RECENT_SECONDS:
            stale.append(event)

    # An episodic event (pre-compact) that has not fired recently is not a
    # fault — it fires only when a context compaction happens. Only the
    # periodic events carry a staleness signal. An episodic event that has
    # NEVER fired anywhere is still caught by the all-missing branch below.
    periodic_unseen = [
        e for e in (missing + stale) if e not in hooks_mod.EPISODIC_EVENTS
    ]
    episodic_unseen = [
        e for e in (missing + stale) if e in hooks_mod.EPISODIC_EVENTS
    ]
    if not periodic_unseen and (missing or stale):
        findings.append(Finding(
            name="hooks.heartbeat",
            severity="ok",
            summary=(
                f"all {len(hooks_mod.HOOK_EVENTS) - len(episodic_unseen)} periodic "
                f"hook events invoked in this workspace within 7d "
                f"({', '.join(episodic_unseen)} idle — fires only on compaction)"
            ),
        ))
        return

    if not missing and not stale:
        findings.append(Finding(
            name="hooks.heartbeat",
            severity="ok",
            summary=(
                f"all {len(hooks_mod.HOOK_EVENTS)} hook events invoked in this "
                "workspace within 7d"
            ),
        ))
        return

    if len(missing) == len(hooks_mod.HOOK_EVENTS):
        # No workspace heartbeat for ANY event. Check the per-user fallback
        # file for evidence hooks fire on this machine at all, just not here
        # — a different diagnosis (wrong-workspace wiring) from "never
        # installed anywhere".
        user_hb = hooks_mod.read_user_heartbeats()
        if not user_hb:
            findings.append(Finding(
                name="hooks.heartbeat",
                severity="warn",
                summary="hooks never invoked on this machine — run `meristem hooks install`",
            ))
            return
        last_event, last_row = max(
            user_hb.items(), key=lambda kv: kv[1].get("last_invoked_at") or ""
        )
        findings.append(Finding(
            name="hooks.heartbeat",
            severity="warn",
            summary=(
                f"hooks fire on this machine but not in this workspace "
                f"(last: {last_event} @ {last_row.get('last_invoked_at', '?')} "
                f"in {last_row.get('last_cwd', '?')})"
            ),
            detail="Run `meristem hooks install` in this workspace to wire it up here too.",
        ))
        return

    findings.append(Finding(
        name="hooks.heartbeat",
        severity="warn",
        summary=(
            f"{', '.join(periodic_unseen)} not seen in 7d — "
            "run `meristem hooks status` for detail"
        ),
        detail=(
            "A hook that never fires is invisible without this. `meristem hooks status` "
            "shows per-event install location, last invocation time, and last outcome."
        ),
    ))


def _check_repo_freshness(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    """How far the index has fallen behind the repo (SPEC §19).

    This check measured wall-clock age until 2026-08-06, which answers the wrong
    question in both directions: a root indexed 40 days ago with no commits
    since is perfectly current, and a root indexed this morning can already be
    56 commits stale — the state a live workspace was actually found in, while
    this check reported green. Commits behind HEAD is the honest number, so it
    is the one reported.
    """
    from . import freshness

    rows = store.repo_rows(conn)
    if not rows:
        findings.append(Finding(
            name="repo.freshness",
            severity="warn",
            summary="no repos registered — run `meristem ingest`",
        ))
        return
    drifts = freshness.workspace_drift(conn)
    dirty = sum(d.dirty for d in drifts)
    # A root registered in the store but absent from disk cannot be indexed,
    # queried against, or repaired by any sync — report it as its own thing
    # rather than as a stale index, which would send the reader after the wrong
    # fix. Checked first because everything below assumes the root is readable.
    if missing := [d for d in drifts if not d.exists]:
        findings.append(Finding(
            name="repo.freshness",
            severity="fail",
            summary=f"{len(missing)}/{len(rows)} registered root(s) no longer exist on disk",
            detail=(
                "\n".join(str(d.root) for d in missing)
                + "\nRe-point `roots` in meristem.toml and re-run `meristem init`, or accept that "
                  "atoms sourced from these paths can no longer be verified."
            ),
        ))
        return
    stale = [d for d in drifts if d.drifted]
    if not stale:
        oldest = min((d.last_indexed_at or 0) for d in drifts)
        age = ""
        if oldest:
            age = f", oldest indexed {(time.time() - oldest) / 86_400:.0f}d ago"
        # Don't claim currency "with HEAD" for roots that have no HEAD.
        subject = (
            "current with HEAD" if any(d.is_git for d in drifts)
            else "indexed (no git — drift not measurable)"
        )
        findings.append(Finding(
            name="repo.freshness",
            severity="ok",
            summary=f"{len(rows)} repo(s) {subject}{age}",
            detail=(f"{dirty} uncommitted file(s) not yet indexed" if dirty else None),
        ))
        return

    behind = freshness.total_behind(stale)
    # A root indexed against a sha git can no longer resolve cannot be caught up
    # incrementally at all — every later `meristem sync` falls back to a full scan or
    # silently indexes nothing. That is worse than being merely behind, so it
    # fails regardless of the commit count.
    unresolvable = [d for d in stale if d.unknown_base]
    never = [d for d in stale if d.never_indexed]
    # Indexed while the repo had no commits: `behind` is 0 and neither of the
    # two buckets above matches, so without this the final `else` reported
    # "drift unmeasurable — 0/N repo(s) never indexed", naming a count of zero
    # and pointing at the wrong check.
    no_base = [d for d in stale if d.indexed_without_head]
    detail = "\n".join(f"{d.root}: {d.describe()}" for d in stale)
    if behind:
        severity: Severity = (
            "fail" if (unresolvable or behind >= DRIFT_FAIL_COMMITS) else "warn"
        )
        summary = (
            f"index is {behind} commit(s) behind HEAD across "
            f"{len(stale)}/{len(rows)} repo(s) — run `meristem sync`"
        )
    elif unresolvable:
        severity = "fail"
        summary = (
            f"{len(unresolvable)}/{len(rows)} repo(s) indexed against a sha git "
            f"cannot resolve — run `meristem sync --force`"
        )
    elif no_base:
        severity = "warn"
        summary = (
            f"{len(no_base)}/{len(rows)} repo(s) indexed before their first "
            f"commit — run `meristem ingest`"
        )
    else:
        # Only never-indexed roots left. `ingest.completed` already fails on
        # exactly this, so say what THIS check can and cannot tell rather than
        # reporting the same defect twice in different words.
        severity = "warn"
        summary = (
            f"drift unmeasurable — {len(never)}/{len(rows)} repo(s) never "
            f"indexed (see ingest.completed)"
        )
    findings.append(Finding(
        name="repo.freshness",
        severity=severity,
        summary=summary,
        detail=(
            f"{detail}\n"
            "Atoms describe the repo as of the last indexed sha; anything committed "
            "since is invisible to retrieval. `meristem sync --install-hook` keeps this "
            "at zero automatically."
        ),
    ))


# The managed hook that carries the `sync` action; post-commit is the one every
# install writes, so it is the canonical "is auto-sync wired" witness.
_SYNC_HOOK = "post-commit"
_SYNC_HOOK_MARKER = "meristem-managed post-commit hook"


def _check_git_hooks(layout: Layout, findings: list[Finding]) -> None:
    """Is anything actually keeping the index level with HEAD (SPEC §19)?

    The managed post-commit hook used to bake in one workspace, so the last
    workspace to install it owned the repo's hook; when that workspace was
    deleted the hook exited on every commit and the index drifted with no
    signal. Checks, per configured root: a managed sync hook exists, it lists
    *this* workspace, and every workspace it lists still has a store. Read-only
    — the fix is a command the operator runs.
    """
    cfg = config.load(layout.config)
    ws = str(layout.root)
    problems: list[str] = []
    checked = 0
    for raw in cfg.workspace.roots:
        p = Path(raw)
        root = (p if p.is_absolute() else layout.root / p).resolve()
        hooks = git_utils.hooks_dir(root) if root.exists() else None
        if hooks is None:
            continue  # not a git repo (or gone — repo.freshness reports that)
        checked += 1
        listed = git_utils.managed_hook_workspaces(hooks / _SYNC_HOOK, _SYNC_HOOK_MARKER)
        if listed is None:
            problems.append(f"{root}: no meristem-managed {_SYNC_HOOK} hook")
            continue
        if "sync" not in listed.get(ws, set()):
            problems.append(f"{root}: managed {_SYNC_HOOK} hook does not list this workspace")
        for other in listed:
            if not (Path(other) / ".meristem" / "atoms.sqlite").exists():
                problems.append(f"{root}: hook lists a workspace that no longer exists: {other}")
    if not checked:
        return
    if problems:
        findings.append(Finding(
            name="hooks.git",
            severity="warn",
            summary=(
                f"{len(problems)} git-hook problem(s) — the index will not auto-sync; "
                "run `meristem sync --install-hook`"
            ),
            detail="\n".join(problems),
        ))
        return
    findings.append(Finding(
        name="hooks.git",
        severity="ok",
        summary=f"managed sync hook lists this workspace in {checked} root(s)",
    ))


def _check_handoff_dir(layout: Layout, findings: list[Finding]) -> None:
    if not layout.handoffs.exists():
        findings.append(Finding(
            name="handoff.dir",
            severity="warn",
            summary="no handoffs directory — first session hasn't written one",
        ))
        return
    files = [p for p in layout.handoffs.iterdir() if p.is_file() and p.suffix == ".md"]
    if not files:
        findings.append(Finding(
            name="handoff.dir",
            severity="ok",
            summary="handoffs dir exists, no files yet",
        ))
        return
    newest = max(files, key=lambda p: p.stat().st_mtime)
    age_h = (time.time() - newest.stat().st_mtime) / 3600
    findings.append(Finding(
        name="handoff.dir",
        severity="ok",
        summary=f"{len(files)} handoff(s), latest {age_h:.1f}h ago",
    ))
