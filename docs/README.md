# Meristem docs

Fifteen sections, meant to be read in order — each one assumes the concepts
introduced before it. Start at 1 if you're new to the project; jump straight
to a section if you already have the shape of the system and need one part
of it.

1. [Overview](01-overview.md) — the problem (agents forget) and the
   elevator-pitch answer (a self-invalidating knowledge graph of atoms).
2. [Data model](02-data-model.md) — the atom: the single typed claim/row
   schema underlying the whole store.
3. [The edge graph](03-edge-graph.md) — how atoms connect to each other via
   typed relationships (`MIRRORS`, `IMPLEMENTS`, `BLOCKS`, `SUPERSEDES`,
   `CO_CHANGED`, and others) and how retrieval walks them.
4. [Liveness](04-liveness.md) — the self-invalidating part: the
   `liveness_kind` / `liveness_target` / `liveness_pattern` mechanism that
   re-validates atoms against the current repo and expires stale facts.
5. [The ambient layer (hooks)](05-ambient-layer.md) — the five Claude Code
   lifecycle hooks (SessionStart, UserPromptSubmit, PostToolUse, Stop,
   PreCompact) that attach Meristem to a session without the agent having to
   ask for context.
6. [The write path](06-write-path.md) — the three routes that write an
   atom, all converging on `atoms.assert_fact` (content-hashed id, idempotent
   re-assertion).
7. [Multi-repo / sync protocol](07-sync-protocol.md) — what gets exported
   from the local, gitignored `.meristem/atoms.sqlite` for sync across repos
   and workspaces.
8. [CLI + MCP surface](08-cli-mcp.md) — full reference map of the
   `meristem` CLI and the MCP server surface.
9. [Design principles](09-design-principles.md) — the recurring design
   principles evident across the data model, write path, and hooks.
10. [Related work / positioning](10-related-work.md) — where Meristem sits
    relative to its nearest neighbors (e.g. HippoRAG): what's borrowed, what's
    novel, and the competitive gap.

11. [Install and setup](11-install.md) — `uvx meristem setup`, what is wired
    for each agent (written vs. snippet), backups and uninstall.
12. [Team memory](12-team-memory.md) — export/import, git-mergeable merges,
    conflicts as `CONTRADICTS` edges, and onboarding a teammate in minutes.
13. [Trust](13-trust.md) — local-only operation and the guard that keeps
    secrets and personal data out of capture and the shared export.
14. [Comparison](14-comparison.md) — an honest comparison with instruction
    files, editor rules and vector-memory tools: where Meristem is better
    and where it is heavier or weaker.
15. [Performance](15-performance.md) — measured scale numbers (with the
    per-run cap caveat) and the statusline fast path.

## See also

- [SPEC.md](../SPEC.md) — design decisions, atom taxonomy, OODAR loop
- [../README.md](../README.md) — project overview and quickstart
