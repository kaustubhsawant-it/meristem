"""Meristem MCP server — exposes the atom store over the Model Context Protocol.

This is a standard MCP server: any MCP-capable client (Claude Code, Claude
Desktop, Cursor, Windsurf, Cline, Continue, …) can launch it and call its
tools. Nothing here is Claude-specific. See the README "Connecting other
agents" section for per-client config snippets.

Tools exposed (matching SPEC §6 + sub-agent JSON contract in §10):

* ``query_facts(query, file_context, top_n, ...)`` — PPR retrieval, gated on
  relevance (SPEC §14.6). Returns ``{atoms, suppressed, top_relevance, …}``:
  an empty ``atoms`` with a non-zero ``suppressed`` is the substrate saying it
  has no answer, which is a result and not an error.
* ``assert_fact(type, topic_key, summary_50w, ...)`` — write-back path used
  by the Remember phase.
* ``supersede(old_id, new_id)`` — explicit closure of a prior fact.
* ``graph_neighbors(atom_id, hops, kinds)`` — BFS over live edges.
* ``why(ref)`` — full provenance trail for one atom: source, a live liveness
  re-check, and its edges. Mirrors the CLI's ``meristem why`` (SPEC §16).
* ``pending_facts(limit)`` / ``suggest_review(id, verdict, reason)`` — read the
  review queue and record an accept/reject *suggestion* (sidecar JSON; never
  disposes — a human still runs ``meristem review``).
* ``embed_status()`` — number of embedded atoms + model name in use.
* ``ping()`` — health check.

Run with ``meristem mcp`` (stdio, default — what desktop/CLI MCP clients use) or
``meristem mcp --transport http --port 8765`` for remote/web agents.

FastMCP is an *optional* extra (``meristem[mcp]``). When the import fails we
print a helpful install hint instead of crashing.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import atoms as atoms_mod
from . import config, embeddings, retrieval, store
from . import edges as edges_mod
from .paths import detect_layout

# Transports we accept on the `meristem mcp` surface. stdio is the local default;
# http/sse expose the server over a port for remote or web-based agents.
SUPPORTED_TRANSPORTS: frozenset[str] = frozenset({"stdio", "http", "sse"})


def _open() -> Any:
    layout = detect_layout()
    if not layout.db.exists():
        raise FileNotFoundError(
            f"no atoms.sqlite at {layout.db}; run `meristem init` then `meristem ingest`"
        )
    return store.connect(layout.db)


def _atom_to_dict(view: atoms_mod.AtomView) -> dict[str, Any]:
    return {
        "id": view.atom.id,
        "type": view.atom.type,
        "topic_key": view.atom.topic_key,
        "decision_status": view.atom.decision_status,
        "decision_class": view.atom.decision_class,
        "summary_10w": view.summaries.get(10),
        "summary_50w": view.summaries.get(50),
        "summary_250w": view.summaries.get(250),
        "source_kind": view.atom.source_kind,
        "source_ref": view.atom.source_ref,
        "valid_from": view.atom.valid_from,
        "confidence": view.atom.confidence,
        "repo_id": view.atom.repo_id,
        "pinned": view.atom.pinned,
    }


def query_facts(
    query: str,
    file_context: list[str] | None = None,
    top_n: int = 10,
    min_relevance: float | None = None,
    include_suppressed: bool = False,
    include_scaffolding: bool = False,
) -> dict[str, Any]:
    """Retrieve atoms relevant to `query`, optionally seeded by file paths.

    Results are gated on relevance (SPEC §14.6). `atoms` may be empty while
    `suppressed` is non-zero: that means Meristem retrieved candidates and judged
    none of them to match — it is an answer ("this workspace does not know"),
    not a failure, and must not be retried as if the store were broken.
    `min_relevance` overrides the configured floor; `include_suppressed`
    returns the rejected atoms for inspection, each flagged
    `_retrieval.suppressed = true` — never treat those as facts. `include_scaffolding`
    mirrors the CLI's `meristem query --scaffolding`: module-tier atoms are
    normally hidden as retrieval scaffolding regardless of relevance; set this
    to let them through the normal relevance gate like any other atom instead.

    Returns a dict, not a bare list, because a silenced query has to be able to
    say why it is silent. An empty list cannot.

    Defined at module level, not inside `build_server`, so it is testable
    without standing up FastMCP or reaching into its internals — those are
    private and have already been renamed once across a major version.
    """
    layout = detect_layout()
    floor = (
        min_relevance
        if min_relevance is not None
        else config.load(layout.config).retrieval.min_relevance
    )
    with _open() as conn:
        hits = retrieval.retrieve(
            conn, query=query, file_context=file_context, top_n=top_n,
            repo_root=layout.root,
        )
        views = {}
        for h in hits:
            if (view := atoms_mod.get_atom(conn, h.atom_id)) is not None:
                views[h.atom_id] = view
        verdict = retrieval.gate(
            (h for h in hits if h.atom_id in views),
            min_relevance=floor,
            topic_of=lambda aid: views[aid].atom.topic_key,
            drop_scaffolding=not include_scaffolding,
        )

        def row_for(h: retrieval.Retrieved, *, suppressed: bool) -> dict[str, Any]:
            row = _atom_to_dict(views[h.atom_id])
            row["_retrieval"] = {
                "score": h.score,
                # The only number here that measures whether this atom answers
                # the query. `score` is a PPR mass share: it ranks atoms within
                # one query and is not comparable across them.
                "relevance": h.relevance,
                # -1.0 relevance means UNKNOWN (no comparable vector under the
                # querying model), not irrelevant. Do not read it as 0.
                "relevance_known": h.relevance_known,
                "suppressed": suppressed,
                "trail": h.trail(),
                "depth": h.depth,
                "via_kind": h.via_kind,
                "seed": h.seed,
                # How far Meristem can vouch for this claim right now:
                # verified | trusted | unverifiable | none. An agent must be
                # able to tell a re-checked fact from one nothing could check —
                # treat "unverifiable" as needing confirmation against the
                # source before you rely on it.
                "liveness_state": h.liveness_state,
            }
            return row

        out = [row_for(h, suppressed=False) for h in verdict.kept]
        if include_suppressed:
            out += [row_for(h, suppressed=True) for h in verdict.suppressed]
        return {
            "atoms": out,
            "suppressed": verdict.suppressed_count,
            "scaffolding_hidden": verdict.scaffolding_count,
            "top_relevance": verdict.top_relevance,
            "min_relevance": floor,
            "silenced": verdict.silenced,
        }


def assert_fact(
    type: str,
    topic_key: str,
    summary_50w: str,
    source_kind: str = "manual",
    source_ref: str | None = None,
    decision_status: str | None = None,
    decision_class: str | None = None,
    summary_10w: str | None = None,
    summary_250w: str | None = None,
    confidence: float = 1.0,
    pinned: bool = False,
    blocks: list[str] | None = None,
) -> dict[str, Any]:
    """Write a new atom (or no-op if identical to an existing live atom).

    `decision_class` ("constraint" | "change") separates a rule that forbids
    future work from a record of what already happened — see `meristem decide`,
    the CLI surface this mirrors. `pinned` protects the atom from consolidation.
    `blocks` is a list of repo-relative path prefixes: every live atom already
    sourced under one of them gets a BLOCKS edge from this atom, so a
    constraint becomes reachable by graph traversal from the code it governs
    (only meaningful for `type="decision"`).

    Defined at module level, not inside `build_server` — same reasoning as
    `query_facts`.
    """
    with _open() as conn:
        atom = atoms_mod.assert_fact(
            conn,
            type=type,  # type: ignore[arg-type]
            topic_key=topic_key,
            summary_50w=summary_50w,
            summary_10w=summary_10w,
            summary_250w=summary_250w,
            source_kind=source_kind,  # type: ignore[arg-type]
            source_ref=source_ref,
            decision_status=decision_status,  # type: ignore[arg-type]
            decision_class=decision_class,  # type: ignore[arg-type]
            confidence=confidence,
            pinned=pinned,
        )
        linked = edges_mod.link_blocks(conn, atom.id, blocks or [])
        out: dict[str, Any] = {
            "id": atom.id, "topic_key": atom.topic_key, "type": atom.type,
        }
        if blocks:
            out["blocks_linked"] = linked
        return out


def _resolve_why_ref(conn: Any, ref: str) -> atoms_mod.AtomView:
    """Look up an atom by id, falling back to topic_key.

    Mirrors `cli.py`'s private helper of the same name — kept in sync with
    it deliberately, since `why` is the one tool whose entire job is to say
    exactly what the CLI says. Raises `KeyError` (this module's established
    not-found convention — see `atoms.supersede`/`liveness.check_atom`)
    rather than returning an error string: FastMCP turns an uncaught
    exception into a normal `isError` tool result without dropping the
    session, so there is no dict-shaped "error" convention to match here.
    """
    view = atoms_mod.get_atom(conn, ref, include_archived=True)
    if view is not None:
        return view
    matches = atoms_mod.query_atoms(conn, topic_key=ref, include_archived=True, limit=5)
    if not matches:
        raise KeyError(f"no atom matches id or topic_key {ref!r}")
    if len(matches) > 1:
        ids = ", ".join(a.id for a in matches)
        raise KeyError(f"{ref!r} matches {len(matches)} atoms — pass an id instead: {ids}")
    view = atoms_mod.get_atom(conn, matches[0].id, include_archived=True)
    assert view is not None  # just matched by query_atoms; can't vanish mid-lookup
    return view


def why(ref: str) -> dict[str, Any]:
    """Full provenance trail for one atom: source, edges, and a liveness
    re-check re-run right now (never read from cache).

    `query_facts` shows *why an atom was retrieved for a query*; this shows
    *why an atom should be believed*, independent of any query — the
    demonstrable half of the no-hallucination claim (SPEC §16). `ref` is an
    atom id or a topic_key; an ambiguous topic_key raises `KeyError` listing
    the candidates instead of guessing one.

    Defined at module level, not inside `build_server` — same reasoning as
    `query_facts`.
    """
    layout = detect_layout()
    from . import liveness as liveness_mod

    with _open() as conn:
        view = _resolve_why_ref(conn, ref)
        atom = view.atom

        if not atom.liveness_kind or atom.liveness_kind == "none":
            liveness: dict[str, Any] = {
                "state": "none", "reason": "no predicate — nothing to verify",
            }
        else:
            result = liveness_mod.check_atom(conn, atom.id, repo_root=layout.root)
            liveness = {
                "state": result.state,
                "reason": result.reason,
                "kind": atom.liveness_kind,
                "target": atom.liveness_target,
                "pattern": atom.liveness_pattern,
            }

        edges_out = []
        for e in edges_mod.list_edges(conn, atom_id=atom.id, limit=100):
            other_id = e.dst_id if e.src_id == atom.id else e.src_id
            other = atoms_mod.get_atom(conn, other_id, include_archived=True)
            ev_rows = conn.execute(
                "SELECT kind, payload FROM edge_evidence WHERE edge_id = ? ORDER BY rowid",
                (e.id,),
            ).fetchall()
            edges_out.append({
                "direction": "both" if not e.directed else ("out" if e.src_id == atom.id else "in"),
                "kind": e.kind,
                "other_id": other_id,
                "other_topic_key": other.atom.topic_key if other else None,
                "weight": e.weight,
                "evidence": [{"kind": r["kind"], "payload": r["payload"]} for r in ev_rows],
            })

        out: dict[str, Any] = {
            "id": atom.id,
            "type": atom.type,
            "topic_key": atom.topic_key,
            "archived": atom.archived,
            "pinned": atom.pinned,
            "valid_to": atom.valid_to,
            "summary_10w": view.summaries.get(10),
            "summary_50w": view.summaries.get(50),
            "provenance": {
                "source_kind": atom.source_kind,
                "source_ref": atom.source_ref,
                "asserted_at": atom.asserted_at,
                "valid_from": atom.valid_from,
                "confidence": atom.confidence,
                "superseded_by": atom.superseded_by,
            },
            "liveness": liveness,
            "edges": edges_out,
        }
        if atom.type == "decision":
            out["provenance"]["decision_status"] = atom.decision_status
            out["provenance"]["decision_class"] = atom.decision_class
        return out


def pending_facts(limit: int = 20) -> list[dict[str, Any]]:
    """The review queue, for an agent to read: pending candidates, best first.

    `id` is the 8-char fingerprint prefix `meristem review` shows. `noise_rule`
    names the capture-filter rule that would no longer propose the item (or
    None). `suggestion` is an earlier `suggest_review` verdict, if any. Nothing
    here is retrievable memory until a human accepts it.
    """
    from . import candidates as cand

    layout = detect_layout()
    with _open() as conn:
        rows = cand.pending(conn, limit=limit)
        noisy = {c.fingerprint: rule for c, rule in cand.noise(conn)}
    suggestions = cand.read_suggestions(layout)
    out: list[dict[str, Any]] = []
    for c in rows:
        s = suggestions.get(c.fingerprint)
        out.append({
            "id": c.fingerprint[:8],
            "text": c.text,
            "score": c.score,
            "proposed_type": c.proposed_type,
            "noise_rule": noisy.get(c.fingerprint),
            "suggestion": (
                {"verdict": s["verdict"], "reason": s.get("reason", "")}
                if isinstance(s, dict) and "verdict" in s else None
            ),
        })
    return out


def suggest_review(id: str, verdict: str, reason: str) -> dict[str, Any]:
    """Record a *suggestion* ("accept" | "reject") for a pending candidate.

    This never accepts or rejects anything: the candidate stays pending and the
    suggestion is shown beside it in `meristem review`, where a human decides.
    Raises KeyError for an id that matches no single pending candidate.
    """
    from . import candidates as cand

    if verdict not in cand.SUGGESTION_VERDICTS:
        raise ValueError(f"verdict must be 'accept' or 'reject', got {verdict!r}")
    with _open() as conn:
        c = cand.resolve(conn, id) if id else None
    if c is None or c.status != "pending":
        raise KeyError(
            f"no pending candidate matches id {id!r} — call pending_facts for current ids"
        )
    entry = cand.record_suggestion(detect_layout(), c.fingerprint, verdict, reason)
    return {
        "recorded": True,
        "id": c.fingerprint[:8],
        "verdict": entry["verdict"],
        "note": "suggestion only — the candidate is still pending human review",
    }


def build_server():  # noqa: ANN201 — return type is FastMCP, not always importable
    """Build (but don't run) the FastMCP server. Importable for tests."""
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover — exercised in `meristem mcp`
        raise RuntimeError(
            "fastmcp not installed. Install with: pip install 'meristem[mcp]'"
        ) from exc

    server = FastMCP("meristem")

    @server.tool()
    def ping() -> dict[str, str]:
        """Health check. Returns workspace path."""
        return {"status": "ok", "workspace": str(detect_layout().root)}

    server.tool()(query_facts)
    server.tool()(assert_fact)
    server.tool()(why)
    server.tool()(pending_facts)
    server.tool()(suggest_review)

    @server.tool()
    def propose_fact(
        text: str,
        proposed_type: str = "convention",
    ) -> dict[str, Any]:
        """Propose a durable fact for human review — the low-risk write path.

        Use this, not `assert_fact`, when you notice something in conversation
        that looks worth remembering: a project rule, a constraint the user
        stated, a convention they corrected you on. A proposal costs the user
        one keystroke to reject; a wrong `assert_fact` becomes a fact that gets
        retrieved and believed later.

        Nothing proposed here is retrievable until accepted via `meristem review`.
        """
        from . import candidates as cand

        with _open() as conn:
            added = cand.queue(conn, text=text, proposed_type=proposed_type, source="agent")
            pending = cand.pending_count(conn)
        return {
            "queued": added,
            "pending_review": pending,
            "note": (
                "queued for review"
                if added
                else "already known — queued, accepted or previously rejected"
            ),
        }

    @server.tool()
    def supersede(old_id: str, new_id: str) -> dict[str, str]:
        """Close `old_id` and point its supersession at `new_id`."""
        with _open() as conn:
            atoms_mod.supersede(conn, old_id=old_id, new_id=new_id)
        return {"status": "ok", "old": old_id, "new": new_id}

    @server.tool()
    def graph_neighbors(
        atom_id: str,
        hops: int = 1,
        kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """BFS over live edges from `atom_id`. Returns hop metadata + atom summary."""
        with _open() as conn:
            hops_out = edges_mod.graph_neighbors(
                conn, atom_id, hops=hops, kinds=kinds  # type: ignore[arg-type]
            )
            out: list[dict[str, Any]] = []
            for hop in hops_out:
                view = atoms_mod.get_atom(conn, hop.atom_id)
                if not view:
                    continue
                row = _atom_to_dict(view)
                row["_hop"] = {
                    "depth": hop.depth,
                    "via_kind": hop.via_kind,
                    "via_edge": hop.via_edge,
                    "weight_acc": hop.weight_acc,
                }
                out.append(row)
            return out

    @server.tool()
    def embed_status() -> dict[str, Any]:
        """Embeddings progress for the live atom set."""
        with _open() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM live_atoms").fetchone()["c"]
            embedded = conn.execute(
                """SELECT COUNT(*) AS c FROM atom_embeddings e
                     JOIN live_atoms a ON a.id = e.atom_id"""
            ).fetchone()["c"]
            model_row = conn.execute(
                "SELECT model FROM atom_embeddings GROUP BY model ORDER BY COUNT(*) DESC LIMIT 1"
            ).fetchone()
        return {
            "live_atoms": total,
            "embedded": embedded,
            "model": (model_row["model"] if model_row else embeddings.default_embedder().name),
        }

    return server


def run(
    transport: str = "stdio",
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Entry point — build and run the MCP server on the chosen transport.

    * ``stdio`` (default) — for local clients that spawn the server as a
      subprocess (Claude Code/Desktop, Cursor, Windsurf, Cline, Continue).
    * ``http`` / ``sse`` — bind ``host:port`` so remote or browser-based
      agents can connect over the network.

    Transport is validated before FastMCP is imported so an obvious typo fails
    fast with a clear message rather than a deep library traceback.
    """
    if transport not in SUPPORTED_TRANSPORTS:
        print(
            json.dumps({
                "error": f"unsupported transport {transport!r}; "
                         f"choose one of {sorted(SUPPORTED_TRANSPORTS)}"
            }),
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        server = build_server()
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(2)
    if transport == "stdio":
        server.run()  # FastMCP defaults to stdio transport.
    else:
        server.run(transport=transport, host=host, port=port)


__all__ = ["SUPPORTED_TRANSPORTS", "build_server", "run"]
