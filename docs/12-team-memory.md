# 12. Team memory

Memory that one person built is more useful when the rest of the team starts
with it. Meristem shares the *substrate* (atoms, summaries, edges, evidence)
through a text file that lives in git, and keeps everything per-machine out of it.
The mechanics are in [Multi-repo / sync protocol](07-sync-protocol.md); this page
is the workflow.

## The pieces

- **Export.** `meristem export` writes the shared atoms to
  `[sync] export_path` (default `.meristem/atoms.jsonl`), or, with
  `mode = "git-jsonl-sharded"`, one small file per shard under `export_dir`.
  Rows are sorted, so identical stores produce identical bytes and a diff shows
  real changes only.
- **Import.** `meristem import` merges that file into the local store. It is a
  union and never destructive: an atom you have locally that is not in the file is
  untouched.
- **Git-mergeable.** `meristem init` registers a git merge driver for the export
  path (a `.gitattributes` line is committed; the driver command is local git
  config, so it is re-registered on every clone by `init`). Two branches that both
  touched the export merge row by row instead of conflicting on text.
- **Never in the export:** the local SQLite file, the review queue (a captured
  fact is not memory until accepted), retrieval logs, embeddings ledger, session
  state and per-machine repo state. Atoms that hold secrets or personal data are
  withheld too; see [Trust](13-trust.md).

Nothing triggers `export` on its own. Run it, then commit the file with your code.
`meristem import --install-hook` adds `post-merge` and `post-checkout` hooks that
run `import` in the background after a pull or branch switch.

## Conflicts become CONTRADICTS edges

Merging identical rows is mechanical. The interesting case is two people who
recorded *different* answers to the same topic. Import does not pick a winner by
file order. It runs the same policy a local write would: one atom per topic stays
live (the later `valid_from`, ties broken by id), the other is closed with an end
date, and a `CONTRADICTS` edge is raised between them. The disagreement stays
visible in the graph (`meristem why <atom>` shows it) instead of being silently
decided.

## Onboarding in minutes

A new teammate, on a repo whose maintainers committed the export:

```bash
git clone <repo> && cd <repo>
uvx meristem setup          # or: meristem setup
meristem init --all         # imports the team's memory, then indexes the code
```

When `meristem init` creates a *fresh* store and finds the shared export
(single file or shard directory), it imports it immediately and reports
`imported N atoms from the shared export`. The import only happens into an empty
store; for a store that already has atoms, use `meristem import`.

`init --all` then ingests the code locally and embeds. Two honest caveats:

- **Imported atoms have no vectors yet.** Embeddings are per-machine and are never
  exported. Until `meristem embed` (or `meristem sync`) runs, imported atoms are
  present in the graph and the store but are not reachable by embedding-based
  retrieval. `init --all` runs the embed step for you; a bare `init` prints a
  reminder.
- **Imported memory is as stale as the export.** Liveness predicates are
  re-validated against the new clone's code, so facts that no longer hold will
  expire on their own, but run `meristem export` regularly so the file does not
  drift far behind the repo.

## Suggested team routine

1. Everyone: `meristem import --install-hook` once per clone.
2. Whoever records a decision or reviews captured facts: `meristem export`, commit
   the file in the same change as the code it concerns.
3. Review the diff of the export like any other file. It is plain JSON lines.
