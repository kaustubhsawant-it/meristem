# 1. Overview

## The problem

Coding agents forget. Every session starts cold: no memory of why a
constraint exists, which decisions are settled, or what broke last time
someone tried the obvious fix. The agent re-reads files to rebuild context,
which burns tokens and, on large or mature codebases, can hit the context
ceiling before any real work happens. Worse, decisions that were already
made get re-litigated, because nothing distinguishes "this was tried and
rejected" from "this was never considered." On a maintenance-phase system
the bottleneck usually isn't specification — it's orientation, and orientation
is exactly what gets thrown away at the end of every session.

## The answer

Meristem is a self-invalidating knowledge graph of typed **atoms** —
invariants, schema facts, decisions, owners, conventions, dependencies,
runtime facts, build recipes, glossary entries — connected by typed
**edges** (`MIRRORS`, `IMPLEMENTS`, `BLOCKS`, `SUPERSEDES`, `CO_CHANGED`, and
others). It is not a vector store or a RAG pipeline. Retrieval walks the
graph with Personalized PageRank, seeded from embedding hits and the atoms
located in the current file, so what surfaces is ranked by graph-flow
through typed relationships, not by raw embedding similarity. A `BLOCKS`
edge or a `MIRRORS` partner two hops away can outrank a closer but
disconnected embedding match — the kind of connection pure k-NN retrieval
misses.

The other half of the design is that facts expire. Atoms carry bi-temporal
validity and a liveness predicate — a grep, AST, or SQL check — that
re-validates the atom against the current repo. When the code a fact
describes changes underneath it, the fact goes stale instead of quietly
going wrong. An index nobody updates answers today's questions with
yesterday's confidence, which is what makes stale memory more dangerous
than no memory; liveness is what keeps Meristem from becoming that. Facts
learned from conversation rather than committed code carry a
reconfirmation horizon instead, since no predicate can verify a stated
policy — after the horizon passes, the fact still surfaces but is marked
unconfirmed.

## How it fits into a session

Meristem attaches to Claude Code through hooks rather than requiring the
agent to remember to ask for context:

- **SessionStart** injects a digest — atom count, branch, and the top few
  atoms most load-bearing for the workspace — so the agent starts oriented
  instead of blank. A separate resumption skill layers on top: if a prior
  session left a recent handoff, invoking it restores that session's tick,
  next action, and anything already tried and abandoned, rather than the
  hook branching on that itself.
- **Per turn**, a prompt hook classifies the incoming request and injects a
  capped set of relevant atoms — the PPR retrieval result for that query —
  without a model call in the hot path.
- **During work**, a tool-use hook watches edits against known invariants,
  and a end-of-turn hook scans the session's own conversation for the shape
  of a standing rule (a decision, an exception, a prohibition) and queues it
  for review rather than writing it straight into memory.
- **At session end** — approaching the context limit, or on request — a
  handoff is written: current tick, atoms consulted, open questions, and
  what didn't work, capped and stripped of anything recoverable from git
  history. The next session resumes from that instead of from nothing.

Each of these mechanisms — the atom taxonomy, the edge types and PPR
algorithm, the hook wiring, and the write-back path from conversation to
memory — gets its own treatment in the sections that follow. This section
is only the map.
