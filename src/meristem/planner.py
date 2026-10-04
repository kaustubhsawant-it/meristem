"""Planning mode (SPEC §15) — `meristem plan <goal>`.

Meristem has no LLM inside the CLI, so this does not invent a task
breakdown — that would be exactly the kind of unsourced claim SPEC §1 exists
to prevent. What it does is retrieve the graph's answer to "what governs
this?" and lay it out so a human or an execution agent (GSD, a subagent
loop) can write the actual tasks with the constraints already in hand,
instead of discovering them mid-patch.

Algorithm:
1. One `retrieval.retrieve()` call over the goal — the same PPR-over-typed-
   edges traversal `meristem query` uses, gated the same way (SPEC §14.6),
   except module-tier atoms are kept rather than dropped as scaffolding:
   here they ARE the signal ("which subsystem does this touch?"), not noise
   to hide before showing a human the result.
2. Partition the kept hits into **surfaces** (tier='module', or the
   individual symbol/glossary atoms when no module rolled up — SPEC's
   "affected module-tier atoms") and **constraints** (invariant,
   schema_fact, or a decision atom classed `constraint`).
3. For each surface, look up its live edges directly (not just what PPR
   happened to retrieve) and attach every touching constraint — this is
   how `meristem decide --blocks <path>` constraints reach a plan even when
   the decision atom itself didn't clear the relevance floor for `goal`.
   A live BLOCKS edge from a CLOSED decision, or any live CONTRADICTS edge,
   marks the surface BLOCKED.
4. Order tasks schema-first, then invariant-bearing, then everything else
   (schema/migration risk is what most often turns "quick fix" into a
   multi-file change).

`--persist` records the plan as one CLOSED `change`-class decision atom
(a plan is a record of what was considered, not itself a new constraint) so
"why did we scope it this way?" stays queryable via `meristem why`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import atoms as atoms_mod
from . import edges as edges_mod
from . import embeddings, retrieval

DEFAULT_TOP_N = 30
# schema_fact-bearing tasks first, then invariant-bearing, then everything
# else — see module docstring point 4.
_CONSTRAINT_TYPE_RANK = {"schema_fact": 0, "invariant": 1}


@dataclass(frozen=True)
class ConstraintRef:
    atom_id: str
    type: str
    decision_class: str | None
    decision_status: str | None
    topic_key: str
    summary: str
    trail: str  # "seed" / "N-hop via KIND" (retrieval) or "linked" (direct edge only)

    def label(self) -> str:
        kind = self.type if self.type != "decision" else "decision"
        return f"[{kind}] {self.summary}  ({self.trail})"


@dataclass
class Task:
    atom_id: str
    title: str
    tier: str
    source_ref: str | None
    trail: str
    constraints: list[ConstraintRef] = field(default_factory=list)
    owners: list[ConstraintRef] = field(default_factory=list)
    blocked_by: list[ConstraintRef] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_by)


@dataclass
class Plan:
    goal: str
    tasks: list[Task] = field(default_factory=list)
    unattached_constraints: list[ConstraintRef] = field(default_factory=list)
    top_relevance: float = -1.0
    # Atoms that cleared the relevance floor but were neither a code surface
    # nor a constraint (e.g. commit/owner records) — tracked so an empty plan
    # can say "matched but nothing plannable" rather than falsely claiming
    # nothing cleared the floor at all.
    matched_non_plannable: int = 0

    @property
    def all_constraints(self) -> list[ConstraintRef]:
        seen: dict[str, ConstraintRef] = {}
        for t in self.tasks:
            for c in t.constraints:
                seen.setdefault(c.atom_id, c)
        for c in self.unattached_constraints:
            seen.setdefault(c.atom_id, c)
        return list(seen.values())

    @property
    def owners(self) -> list[ConstraintRef]:
        """De-duped union of every task's attached owners — SPEC §15's own
        section, separate from `all_constraints`. An owner is informational
        context (who to loop in), never a constraint on the approach, so it
        does not belong mixed into the same list as invariants/decisions."""
        seen: dict[str, ConstraintRef] = {}
        for t in self.tasks:
            for c in t.owners:
                seen.setdefault(c.atom_id, c)
        return list(seen.values())

    @property
    def blocking(self) -> list[ConstraintRef]:
        seen: dict[str, ConstraintRef] = {}
        for t in self.tasks:
            for c in t.blocked_by:
                seen.setdefault(c.atom_id, c)
        return list(seen.values())


def _is_constraint_atom(atom: atoms_mod.Atom) -> bool:
    if atom.type in ("invariant", "schema_fact"):
        return True
    return atom.type == "decision" and atom.decision_class == "constraint"


def _surface_title(atom: atoms_mod.Atom) -> str:
    if atom.tier == "module" and atom.source_ref:
        return f"touches module {atom.source_ref}"
    if atom.source_ref:
        return f"touches {atom.source_ref}"
    return atom.topic_key


def _constraint_ref(atom: atoms_mod.Atom, summary: str, trail: str) -> ConstraintRef:
    return ConstraintRef(
        atom_id=atom.id,
        type=atom.type,
        decision_class=atom.decision_class,
        decision_status=atom.decision_status,
        topic_key=atom.topic_key,
        summary=summary,
        trail=trail,
    )


def _task_sort_key(task: Task) -> tuple:
    best = min((_CONSTRAINT_TYPE_RANK.get(c.type, 2) for c in task.constraints), default=2)
    return (0 if task.blocked else 1, best, task.title)


def build_plan(
    conn: sqlite3.Connection,
    goal: str,
    *,
    top_n: int = DEFAULT_TOP_N,
    file_context: list[str] | None = None,
    min_relevance: float | None = None,
    repo_root: Path | None = None,
    embedder: embeddings.Embedder | None = None,
) -> Plan:
    results = retrieval.retrieve(
        conn, query=goal, file_context=file_context, top_n=top_n, repo_root=repo_root,
        embedder=embedder,
    )
    views: dict[str, atoms_mod.AtomView] = {}
    for r in results:
        if r.atom_id not in views and (v := atoms_mod.get_atom(conn, r.atom_id)) is not None:
            views[r.atom_id] = v

    floor = min_relevance if min_relevance is not None else -1.0
    gated = retrieval.gate(
        (r for r in results if r.atom_id in views),
        min_relevance=floor,
        topic_of=lambda aid: views[aid].atom.topic_key,
        drop_scaffolding=False,  # module-tier atoms are the signal here, not noise
    )
    top_relevance = gated.top_relevance

    trail_by_id = {r.atom_id: r.trail() for r in gated.kept}
    kept_atoms = [views[r.atom_id].atom for r in gated.kept]

    # A "surface" is code the goal would touch — glossary atoms (module/file/
    # symbol tier). Decision atoms (commit records, constraints) and other
    # metadata types are never surfaces, even as a fallback: a commit SHA is
    # not something a plan tells you to go touch.
    surface_atoms = [a for a in kept_atoms if a.tier == "module" and a.type == "glossary"] or [
        a for a in kept_atoms if a.type == "glossary"
    ]
    constraint_atoms = {a.id: a for a in kept_atoms if _is_constraint_atom(a)}
    plannable_ids = {a.id for a in surface_atoms} | set(constraint_atoms)
    matched_non_plannable = sum(1 for a in kept_atoms if a.id not in plannable_ids)

    tasks: list[Task] = []
    for atom in surface_atoms:
        task = Task(
            atom_id=atom.id, title=_surface_title(atom), tier=atom.tier,
            source_ref=atom.source_ref, trail=trail_by_id.get(atom.id, "seed"),
        )
        attached: dict[str, ConstraintRef] = {}
        attached_owners: dict[str, ConstraintRef] = {}
        for e in edges_mod.list_edges(conn, atom_id=atom.id, limit=100):
            other_id = e.dst_id if e.src_id == atom.id else e.src_id
            other = atoms_mod.get_atom(conn, other_id)
            if other is None:
                continue
            other_atom = other.atom
            # SPEC §15 step 1: invariants/decisions/owners/schema facts
            # touching the goal, not just BLOCKS/CONTRADICTS. An owner is
            # informational context (never blocking); everything else that
            # reaches here is a genuine constraint on this surface.
            is_owner = e.kind == "OWNS" and other_atom.type == "owner"
            relevant = (
                (e.kind == "BLOCKS" and other_atom.type == "decision")
                or e.kind == "CONTRADICTS"
                or is_owner
                or _is_constraint_atom(other_atom)
            )
            if not relevant:
                continue
            ref = _constraint_ref(
                other_atom, other.summaries.get(50, other_atom.topic_key),
                trail_by_id.get(other_atom.id, "linked"),
            )
            # Owners get their own bucket (Plan.owners / Task.owners), not
            # `constraints` — SPEC §15's "Not yet done" caveat: an owner is
            # who to loop in, not a fact the approach must not violate, so it
            # shouldn't be indistinguishable from an invariant except by a
            # `type` string buried in the same list.
            if is_owner:
                attached_owners.setdefault(ref.atom_id, ref)
            else:
                attached.setdefault(ref.atom_id, ref)
            constraint_atoms.pop(other_atom.id, None)
            if e.kind == "CONTRADICTS" or (
                e.kind == "BLOCKS" and other_atom.decision_status == "CLOSED"
            ):
                task.blocked_by.append(ref)
        task.constraints = list(attached.values())
        task.owners = list(attached_owners.values())
        tasks.append(task)

    tasks.sort(key=_task_sort_key)

    unattached = [
        _constraint_ref(
            a, views[a.id].summaries.get(50, a.topic_key), trail_by_id.get(a.id, "seed")
        )
        for a in constraint_atoms.values()
    ]

    return Plan(
        goal=goal, tasks=tasks, unattached_constraints=unattached,
        top_relevance=top_relevance, matched_non_plannable=matched_non_plannable,
    )


def format_report(plan: Plan) -> str:
    lines = [f'Plan for "{plan.goal}":', ""]
    if not plan.tasks and not plan.all_constraints:
        if plan.matched_non_plannable:
            lines.append(
                f"  ({plan.matched_non_plannable} atom(s) cleared the relevance floor "
                f"(best cosine {plan.top_relevance:.3f}) but none were a code surface "
                f"or a constraint — likely just commit/owner history for this goal.)"
            )
        else:
            lines.append(
                f"  (nothing cleared the relevance floor — best cosine was "
                f"{plan.top_relevance:.3f}. The substrate has no recorded knowledge "
                f"for this goal; consider asserting invariants/decisions before proceeding.)"
            )
        return "\n".join(lines)

    for i, task in enumerate(plan.tasks, start=1):
        flag = "  [BLOCKED]" if task.blocked else ""
        lines.append(f"  {i}. {task.title}{flag}")
        for c in task.constraints:
            lines.append(f"     - {c.label()}")
        for c in task.owners:
            lines.append(f"     owner: {c.summary}  ({c.trail})")
        if task.blocked_by:
            ids = ", ".join(c.atom_id for c in task.blocked_by)
            lines.append(f"     ⚠ hits decision(s): {ids}")
        lines.append("")

    all_constraints = plan.all_constraints
    lines.append(f"Constraints: {len(all_constraints)} fact(s)")
    for c in all_constraints:
        lines.append(f"  • {c.label()}")
    lines.append("")

    owners = plan.owners
    if owners:
        lines.append(f"Owners: {len(owners)}")
        for c in owners:
            lines.append(f"  • {c.summary}  ({c.trail})")
        lines.append("")

    for c in plan.blocking:
        if c.type == "decision" and c.decision_status == "CLOSED":
            lines.append(
                f"  ⚠ approach hits CLOSED decision {c.atom_id}: {c.summary} "
                "— do not plan around it silently"
            )

    lines.append("")
    lines.append(
        "Next: feed these constraints to /meristem:patch or your GSD phase — do not "
        "edit around a BLOCKED task silently."
    )
    return "\n".join(lines)


def to_json(plan: Plan) -> str:
    def ref_dict(c: ConstraintRef) -> dict:
        return {
            "atom_id": c.atom_id, "type": c.type, "decision_class": c.decision_class,
            "decision_status": c.decision_status, "topic_key": c.topic_key,
            "summary": c.summary, "trail": c.trail,
        }

    payload = {
        "goal": plan.goal,
        "top_relevance": plan.top_relevance,
        "tasks": [
            {
                "atom_id": t.atom_id, "title": t.title, "tier": t.tier,
                "source_ref": t.source_ref, "trail": t.trail, "blocked": t.blocked,
                "constraints": [ref_dict(c) for c in t.constraints],
                "owners": [ref_dict(c) for c in t.owners],
                "blocked_by": [ref_dict(c) for c in t.blocked_by],
            }
            for t in plan.tasks
        ],
        "constraints": [ref_dict(c) for c in plan.all_constraints],
        "owners": [ref_dict(c) for c in plan.owners],
        "blocking": [ref_dict(c) for c in plan.blocking],
    }
    return json.dumps(payload, indent=2)


def persist_plan(conn: sqlite3.Connection, plan: Plan, *, slug: str) -> atoms_mod.Atom:
    """Record the plan as a CLOSED `change`-class decision atom — a plan is a
    record of what was considered, not itself a new binding constraint — so
    `meristem why plan:<slug>` answers "why did we scope it this way?"."""
    constraint_ids = ", ".join(c.atom_id for c in plan.all_constraints) or "(none)"
    task_titles = "; ".join(t.title for t in plan.tasks) or "(no surface atoms retrieved)"
    return atoms_mod.assert_fact(
        conn,
        type="decision",
        topic_key=f"plan:{slug}",
        summary_10w=plan.goal[:80],
        summary_50w=f"Plan for: {plan.goal}",
        summary_250w=(
            f"Plan for: {plan.goal}\n\nTasks: {task_titles}\n\n"
            f"Constraints consulted: {constraint_ids}"
        ),
        source_kind="manual",
        decision_status="CLOSED",
        decision_class="change",
        auto_supersede=False,
    )
